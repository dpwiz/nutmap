import gzip
import io
import json
import logging
import math
from collections import defaultdict

import numpy as np
from PIL import Image

from . import durable
from .context import PipelineError, Stage, register
from .flowmap import MUTED, SLOTS, pull_in, pull_in_params
from .inventory import load_entries, load_resolver
from .progress import make_progress
from .render import source_path
from .viewpoints import RACE, delta_planes, load_sweeps, quantised

log = logging.getLogger("nutmap")

TILE = 64
NONE = 0xFFFF


def compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def write_json(path, obj):
    durable.atomic_write_text(path, compact(obj))


def sprite_sheet(ctx, entries):
    cols = math.ceil(math.sqrt(len(entries)))
    rows = math.ceil(len(entries) / cols)
    sheet = Image.new("RGBA", (cols * TILE, rows * TILE), (0, 0, 0, 0))
    for k, e in enumerate(entries):
        src = source_path(ctx, "3d", e)
        if src is None:
            raise PipelineError(f"web: no 3d image for {e['id']}")
        im = Image.open(src).convert("RGBA")
        im.thumbnail((TILE, TILE), Image.LANCZOS)
        x = (k % cols) * TILE + (TILE - im.width) // 2
        y = (k // cols) * TILE + (TILE - im.height) // 2
        sheet.alpha_composite(im, (x, y))
    buf = io.BytesIO()
    sheet.save(buf, format="WEBP", quality=75, alpha_quality=70, method=6)
    return buf.getvalue(), cols, rows


def square_frame(pts):
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    span = (hi - lo).max()
    return lo, span, ((span - (hi - lo)) / span) / 2


def in_frame(pts, frame):
    lo, span, pad = frame
    out = (pts - lo) / span + pad
    out[..., 1] = 1 - out[..., 1]
    return out


def flow_points(flow_rows, ids):
    xy = {r["id"]: (r["x"], r["y"]) for r in flow_rows}
    return np.array([xy[i] for i in ids], dtype=np.float64)


def normalised(flow_rows, ids):
    pts = flow_points(flow_rows, ids)
    return in_frame(pts, square_frame(pts))


def screen_sweeps(ctx, ids):
    sweeps, alphas, names = load_sweeps(ctx.path("viewpoints"))
    race_end = sweeps[names.index(RACE), -1].astype(np.float64)
    coords = durable.read_json(ctx.path("map", "umap.json"))
    target = np.array([coords[i] for i in ids], dtype=np.float64)
    design = np.hstack([race_end, np.ones((len(race_end), 1))])
    affine, *_ = np.linalg.lstsq(design, target, rcond=None)
    flat = sweeps.reshape(-1, 2).astype(np.float64)
    mapped = np.hstack([flat, np.ones((len(flat), 1))]) @ affine
    center, r0 = pull_in_params(target)
    pulled, _ = pull_in(mapped, center, r0)
    flow = flow_points(durable.read_jsonl(ctx.path("flow", "flow.jsonl")), ids)
    screen = in_frame(pulled, square_frame(flow)).reshape(sweeps.shape)
    drift = np.abs(screen[names.index(RACE), -1] - in_frame(flow, square_frame(flow))).max()
    if drift > 1e-3:
        raise PipelineError(f"web: the race end of the bridge lands {drift:.2e} away from the site map")
    return screen, alphas, names, drift


def encode_view(layouts, lo, hi):
    return gzip.compress(delta_planes(quantised(layouts, lo, hi), 0), compresslevel=9, mtime=0)


@register
class Web(Stage):
    name = "web"
    dir = "web"
    needs = ("inventory", "panel", "race", "explain", "flowmap", "render", "viewpoints")

    def config_slice(self, ctx):
        return {"flowmap": ctx.cfg["flowmap"], "root": ctx.cfg["root"], "version": 3}

    def outputs(self, ctx):
        return [f"web/public/{f}" for f in ("meta.json", "nodes.json", "relations.json", "sprite.webp", "tree", "desc", "views")]

    def run(self, ctx):
        out = ctx.path("web", "public")
        entries = load_entries(ctx)
        ids = [e["id"] for e in entries]
        n = len(ids)
        root = load_resolver(ctx, entries)(ctx.cfg["root"])
        fc = ctx.cfg["flowmap"]

        pts = normalised(durable.read_jsonl(ctx.path("flow", "flow.jsonl")), ids)
        nodes = [
            {
                "id": e["id"],
                "c": e["char"],
                "n": e["name"],
                "g": e["group"],
                "s": e["subgroup"],
                "k": e["keywords"],
                "x": round(float(x), 5),
                "y": round(float(y), 5),
            }
            for e, (x, y) in zip(entries, pts)
        ]
        write_json(out / "nodes.json", nodes)

        notes = durable.read_jsonl(ctx.path("explain", "step_notes.jsonl"))
        strings = sorted({x["relation"] for x in notes if x["relation"]})
        index = {s: k for k, s in enumerate(strings)}
        if len(strings) >= NONE:
            raise PipelineError("web: too many relation strings for uint16 indices")
        step_rel = {(x["from"], x["to"]): index[x["relation"]] for x in notes if x["relation"]}
        write_json(out / "relations.json", strings)

        sheet, cols, rows = sprite_sheet(ctx, entries)
        durable.atomic_write_bytes(out / "sprite.webp", sheet)

        angles = [{"id": a["id"], "title": a["title"]} for a in ctx.angles()]
        screen, alphas, names, drift = screen_sweeps(ctx, ids)
        lo = screen.reshape(-1, 2).min(axis=0)
        hi = screen.reshape(-1, 2).max(axis=0)
        view_bytes = 0
        for k, name in enumerate(names):
            data = encode_view(screen[k], lo, hi)
            durable.atomic_write_bytes(out / "views" / f"{name}.bin", data)
            view_bytes += len(data)
        log.info("web: %d map sweeps, %.1f MB, race end within %.1e of the site map", len(names), view_bytes / 1e6, drift)
        views = {
            "layouts": int(screen.shape[1]),
            "alphas": [round(float(a), 6) for a in alphas],
            "lo": [float(v) for v in lo],
            "hi": [float(v) for v in hi],
            "race": RACE,
            "sweeps": [{"id": RACE, "title": "Race map"}] + [a for a in angles if a["id"] in names],
        }
        write_json(
            out / "meta.json",
            {
                "n": n,
                "root": root,
                "angles": angles,
                "sprite": {"file": "sprite.webp", "tile": TILE, "cols": cols, "rows": rows},
                "palette": {"slots": SLOTS, "other": MUTED},
                "riverMinFlow": fc["river_min_flow"],
                "coloredBasins": fc["colored_basins"],
                "views": views,
            },
        )

        pred = np.load(ctx.path("race", "pred.npy")).astype(np.int64)
        by_angle = defaultdict(dict)
        for r in durable.read_jsonl(ctx.path("panel", "panel.jsonl")):
            by_angle[r["emoji_id"]][r["angle"]] = r
        order = [a["id"] for a in angles]
        missing = 0
        with make_progress(ctx.console) as prog:
            task = prog.add_task("web trees", total=n, note="")
            for r in range(n):
                durable.check_stop()
                parent = np.full(n, NONE, dtype="<u2")
                rel = np.full(n, NONE, dtype="<u2")
                for j in range(n):
                    if j == r:
                        continue
                    p = int(pred[r, j])
                    parent[j] = p
                    k = step_rel.get((ids[j], ids[p]))
                    if k is None:
                        missing += 1
                    else:
                        rel[j] = k
                durable.atomic_write_bytes(out / "tree" / f"{ids[r]}.bin", parent.tobytes() + rel.tobytes())
                prog.advance(task)
            task = prog.add_task("web descriptions", total=n, note="")
            for i in ids:
                durable.check_stop()
                rows_out = []
                for a in order:
                    rec = by_angle[i].get(a)
                    if rec is None:
                        continue
                    rows_out.append(
                        {
                            "a": a,
                            "d": rec["description"],
                            "k": rec["keywords"],
                            "l": [{"e": lk["emoji"], "id": lk["id"], "r": lk["relation"]} for lk in rec["links"]],
                        }
                    )
                write_json(out / "desc" / f"{i}.json", rows_out)
                prog.advance(task)
        if missing:
            log.warning("web: %d tree steps across all focuses have no relation text (refused or failed notes)", missing)
        sizes = {f: (out / f).stat().st_size for f in ("meta.json", "nodes.json", "relations.json", "sprite.webp")}
        tree_bytes = sum(p.stat().st_size for p in (out / "tree").glob("*.bin"))
        desc_bytes = sum(p.stat().st_size for p in (out / "desc").glob("*.json"))
        log.info(
            "web: %s, tree/ %d files %.1f MB, desc/ %d files %.1f MB",
            ", ".join(f"{k} {v / 1024:.0f} KB" for k, v in sizes.items()),
            n,
            tree_bytes / 1e6,
            n,
            desc_bytes / 1e6,
        )
