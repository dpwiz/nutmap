import io
import logging
import random
import subprocess

from PIL import Image

from . import durable
from .context import Stage, register
from .inventory import load_entries
from .progress import make_progress

log = logging.getLogger("nutmap")

REGIONAL_A = 0x1F1E6
TAG_A = 0xE0061
TAG_END = 0xE007F


def noto_commit(ctx):
    noto = ctx.src(ctx.cfg["sources"]["noto_dir"])
    return subprocess.run(["git", "-C", str(noto), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def flag_code(key):
    cps = [ord(c) for c in key]
    if len(cps) == 2 and all(REGIONAL_A <= cp < REGIONAL_A + 26 for cp in cps):
        return "".join(chr(ord("A") + cp - REGIONAL_A) for cp in cps)
    if cps and cps[0] == 0x1F3F4 and cps[-1] == TAG_END and len(cps) > 3:
        letters = "".join(chr(ord("a") + cp - TAG_A) for cp in cps[1:-1]).upper()
        return f"{letters[:2]}-{letters[2:]}"
    return None


def noto_name(key):
    return "emoji_u" + "_".join(f"{ord(c):04x}" for c in key) + ".png"


def source_path(ctx, style, entry):
    noto = ctx.src(ctx.cfg["sources"]["noto_dir"])
    folder = {"3d": "3D", "2d": "2D"}[style]
    p = noto / folder / "png" / "512" / noto_name(entry["key"])
    if p.exists():
        return p
    if style == "2d":
        code = flag_code(entry["key"])
        if code:
            q = noto / "third_party" / "region-flags" / "png" / f"{code}.png"
            if q.exists():
                return q
    return None


def to_tile(path, size):
    im = Image.open(path).convert("RGBA")
    w, h = im.size
    side = max(w, h)
    canvas = Image.new("RGBA", (side, side), (255, 255, 255, 255))
    canvas.alpha_composite(im, ((side - w) // 2, (side - h) // 2))
    out = canvas.convert("RGB").resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def contact_sheet(ctx, entries, style, out_path, cols=16, tile=96):
    rng = random.Random(7)
    picks = rng.sample(entries, min(60, len(entries)))
    by_sub = {}
    for e in entries:
        if e["group"] == "Flags":
            by_sub.setdefault(e["subgroup"], []).append(e)
    for sub in sorted(by_sub):
        picks += by_sub[sub][:8]
    picks += [e for e in entries if "‍" in e["key"]][:: max(1, len([e for e in entries if "‍" in e["key"]]) // 20)][:20]
    seen, ordered = set(), []
    for e in picks:
        if e["id"] not in seen:
            seen.add(e["id"])
            ordered.append(e)
    rows = (len(ordered) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile, rows * tile), (255, 255, 255))
    for i, e in enumerate(ordered):
        p = ctx.path("img", style, f"{e['id']}.png")
        if p.exists():
            sheet.paste(Image.open(p).convert("RGB").resize((tile, tile)), ((i % cols) * tile, (i // cols) * tile))
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    durable.atomic_write_bytes(out_path, buf.getvalue())


@register
class Render(Stage):
    name = "render"
    dir = "img"
    needs = ("inventory",)

    def config_slice(self, ctx):
        return {"styles": ctx.cfg["render"]["styles"], "size": ctx.cfg["render"]["size"], "version": 2}

    def extra_inputs(self, ctx):
        return {"noto": noto_commit(ctx)}

    def outputs(self, ctx):
        return ["img/index.json", "img/missing.txt"] + [f"img/contact_{s}.png" for s in ctx.cfg["render"]["styles"]]

    def describe(self, ctx):
        parts = []
        for s in ctx.cfg["render"]["styles"]:
            d = ctx.path("img", s)
            parts.append(f"{s}: {sum(1 for _ in d.glob('*.png')) if d.exists() else 0}")
        return ", ".join(parts)

    def run(self, ctx):
        entries = load_entries(ctx)
        size = ctx.cfg["render"]["size"]
        styles = ctx.cfg["render"]["styles"]
        index = {s: [] for s in styles}
        missing = []
        with make_progress(ctx.console) as prog:
            for style in styles:
                task = prog.add_task(f"render {style}", total=len(entries), note="")
                for e in entries:
                    durable.check_stop()
                    out = ctx.path("img", style, f"{e['id']}.png")
                    if out.exists():
                        index[style].append(e["id"])
                        prog.advance(task)
                        continue
                    src = source_path(ctx, style, e)
                    if src is None:
                        missing.append(f"{style}\t{e['id']}\t{e['char']}\t{e['name']}")
                        prog.advance(task)
                        continue
                    durable.chaos("render item")
                    durable.atomic_write_bytes(out, to_tile(src, size))
                    index[style].append(e["id"])
                    prog.advance(task)
        durable.atomic_write_text(ctx.path("img", "missing.txt"), "".join(m + "\n" for m in missing))
        durable.atomic_write_json(ctx.path("img", "index.json"), index, indent=None)
        for style in styles:
            contact_sheet(ctx, entries, style, ctx.path("img", f"contact_{style}.png"))
        if missing:
            log.warning("render: %d missing images, see %s", len(missing), ctx.path("img", "missing.txt"))
        for style in styles:
            log.info("render: %s has %d/%d images", style, len(index[style]), len(entries))
