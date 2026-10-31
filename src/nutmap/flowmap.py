import io
import logging
import textwrap
from collections import defaultdict

import numpy as np

from . import durable
from .context import Stage, register
from .inventory import load_entries

log = logging.getLogger("nutmap")

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
CONTEXT = "#c3c2b7"
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a"]


def drainage(entries, race):
    parent = {r["id"]: r["parent"] for r in race}
    root = next(i for i, p in parent.items() if p is None)
    kids = defaultdict(list)
    for i, p in parent.items():
        if p is not None:
            kids[p].append(i)
    flow = {}
    stack = [(root, False)]
    while stack:
        i, seen = stack.pop()
        if seen:
            flow[i] = 1 + sum(flow[k] for k in kids[i])
        else:
            stack.append((i, True))
            stack.extend((k, False) for k in kids[i])
    basin = {root: None}
    for head in kids[root]:
        todo = [head]
        while todo:
            i = todo.pop()
            basin[i] = head
            todo.extend(kids[i])
    heads = sorted(kids[root], key=lambda h: (-flow[h], h))
    return root, parent, kids, flow, basin, heads


def display_coords(coords):
    xy = np.array(coords, dtype=np.float64)
    center = np.median(xy, axis=0)
    d = xy - center
    r = np.linalg.norm(d, axis=1)
    r0 = np.percentile(r, 98)
    s = 0.15 * r0
    far = r > r0
    scale = np.ones_like(r)
    scale[far] = (r0 + s * np.log1p((r[far] - r0) / s)) / r[far]
    return center + d * scale[:, None], int(far.sum())


def unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v * 0


def river_segments(pos, parent, samples=20):
    t = np.linspace(0, 1, samples)[:, None]
    segs = {}
    for u, p in parent.items():
        if p is None:
            continue
        a, b = pos[u], pos[p]
        length = np.linalg.norm(b - a)
        straight = unit(b - a)
        downstream = unit(pos[parent[p]] - b) if parent[p] is not None else straight
        arrive = unit(straight + downstream)
        c1 = a + straight * length * 0.33
        c2 = b - arrive * length * 0.33
        segs[u] = (1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * c1 + 3 * (1 - t) * t**2 * c2 + t**3 * b
    return segs


def spaced(candidates, pos, keep_clear, min_sep, limit):
    placed = list(keep_clear)
    out = []
    for u in candidates:
        if len(out) >= limit:
            break
        if all(np.linalg.norm(pos[u] - q) >= min_sep for q in placed):
            out.append(u)
            placed.append(pos[u])
    return out


def thumb(ctx, entry_id, px):
    from PIL import Image

    p = ctx.path("img", ctx.cfg["render"]["styles"][0], f"{entry_id}.png")
    im = Image.open(p).convert("RGBA").resize((px, px), Image.LANCZOS)
    arr = np.asarray(im).copy()
    white = (arr[..., :3] > 245).all(axis=-1)
    arr[white, 3] = 0
    return arr


def add_emoji(ax, ctx, entry_id, xy, px, zoom=1.0, ring=False):
    from matplotlib.offsetbox import AnnotationBbox, OffsetImage

    box = AnnotationBbox(
        OffsetImage(thumb(ctx, entry_id, px), zoom=zoom),
        xy,
        frameon=ring,
        pad=0.25,
        bboxprops={"edgecolor": INK_2, "facecolor": SURFACE, "boxstyle": "circle,pad=0.3", "linewidth": 2} if ring else None,
        zorder=6,
    )
    ax.add_artist(box)


def draw_rivers(ax, segs, flow, color_of, alpha_of, max_flow, order):
    from matplotlib.collections import LineCollection

    lines = [segs[u] for u in order]
    widths = [0.35 + 9.0 * np.sqrt(flow[u] / max_flow) for u in order]
    colors = [color_of(u) for u in order]
    alphas = [alpha_of(u) for u in order]
    from matplotlib.colors import to_rgba

    rgba = [to_rgba(c, a) for c, a in zip(colors, alphas)]
    ax.add_collection(LineCollection(lines, linewidths=widths, colors=rgba, capstyle="round", joinstyle="round", zorder=2))


def frame(ax, pos):
    lo, hi = pos.min(axis=0), pos.max(axis=0)
    pad = 0.04 * (hi - lo).max()
    ax.set_xlim(lo[0] - pad, hi[0] + pad)
    ax.set_ylim(lo[1] - pad, hi[1] + pad)
    ax.set_aspect("equal")
    ax.set_axis_off()


def png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    return buf.getvalue()


@register
class Flowmap(Stage):
    name = "flowmap"
    dir = "flow"
    needs = ("inventory", "race", "layout")

    def config_slice(self, ctx):
        return {"flowmap": ctx.cfg["flowmap"], "version": 1}

    def outputs(self, ctx):
        return ["flow/flow.jsonl", "flow/flowmap.png", "flow/basins.png"]

    def run(self, ctx):
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D

        fc = ctx.cfg["flowmap"]
        entries = load_entries(ctx)
        by_id = {e["id"]: e for e in entries}
        race = durable.read_jsonl(ctx.path("race", "root.jsonl"))
        root, parent, kids, flow, basin, heads = drainage(entries, race)
        coords = durable.read_json(ctx.path("map", "umap.json"))
        ids = [e["id"] for e in entries]
        disp, pulled = display_coords([coords[i] for i in ids])
        pos = {i: disp[k] for k, i in enumerate(ids)}
        segs = river_segments(pos, parent)
        colored = heads[: min(fc["colored_basins"], len(SLOTS))]
        color = {h: SLOTS[k] for k, h in enumerate(colored)}
        max_flow = max(flow[h] for h in heads)
        order = sorted(segs, key=lambda u: (flow[u], u))
        rivers = [u for u in order if flow[u] >= fc["river_min_flow"]]
        durable.check_stop()

        fig = plt.figure(figsize=(36, 30), dpi=100)
        fig.patch.set_facecolor(SURFACE)
        ax = fig.add_axes((0.01, 0.04, 0.74, 0.88))
        key = fig.add_axes((0.77, 0.04, 0.22, 0.88))
        draw_rivers(ax, segs, flow, lambda u: color.get(basin[u], MUTED), lambda u: 0.85 if basin[u] in color else 0.5, max_flow, rivers)
        pts = np.array([pos[i] for i in ids])
        ax.scatter(pts[:, 0], pts[:, 1], s=14, c=[color.get(basin.get(i), MUTED) for i in ids], alpha=0.75, linewidths=0, zorder=3)
        frame(ax, disp)
        extent = (disp.max(axis=0) - disp.min(axis=0)).max()
        candidates = [u for u in sorted(flow, key=lambda u: (-flow[u], u)) if u != root]
        landmarks = spaced(candidates, pos, [pos[root]], 0.035 * extent, fc["landmarks"])
        for u in landmarks:
            add_emoji(ax, ctx, u, pos[u], 48, 0.8)
        add_emoji(ax, ctx, root, pos[root], 96, 1.1, ring=True)

        key.set_xlim(0, 1)
        key.set_ylim(len(heads) + 1.5, -1.5)
        key.set_axis_off()
        key.text(0, -1, "Basins, by the emoji that drain through them", fontsize=20, color=INK, va="center")
        for k, h in enumerate(heads):
            key.add_line(Line2D([0.0, 0.08], [k, k], color=color.get(h, MUTED), lw=7, alpha=0.9 if h in color else 0.6, solid_capstyle="round"))
            add_emoji(key, ctx, h, (0.15, k), 48, 0.75)
            key.text(0.22, k, by_id[h]["name"], fontsize=18, color=INK_2, va="center")
            key.text(1.0, k, f"{flow[h]}", fontsize=18, color=INK_2, va="center", ha="right")
        ax.set_title(f"How {len(ids):,} emoji drain into the {by_id[root]['name']}", fontsize=36, color=INK, loc="left", pad=24)
        note = (
            f"Lines are race steps towards the chestnut that carry at least {fc['river_min_flow']} emoji, curved to merge downstream; "
            "width grows with the emoji flowing through. Single-emoji steps are left out; every emoji is a dot coloured by its basin. "
            f"Only the {len(colored)} largest basins get a colour, the rest are grey."
        )
        if pulled:
            note += f" {pulled} far-out emoji are pulled towards the centre for display."
        fig.text(0.01, 0.008, textwrap.fill(note, 190), fontsize=16, color=MUTED, ha="left", va="bottom", linespacing=1.5)
        durable.atomic_write_bytes(ctx.path("flow", "flowmap.png"), png(fig))
        plt.close(fig)
        durable.check_stop()

        facets = heads[: fc["facets"]]
        cols = 4
        rows = max(1, (len(facets) + cols - 1) // cols)
        fig, axes = plt.subplots(rows, cols, figsize=(8 * cols, 8 * rows), dpi=100, squeeze=False)
        fig.patch.set_facecolor(SURFACE)
        for ax, h in zip(axes.flat, facets):
            draw_rivers(ax, segs, flow, lambda u, h=h: SLOTS[0] if basin[u] == h else CONTEXT, lambda u, h=h: 0.9 if basin[u] == h else 0.35, max_flow, sorted(order, key=lambda u, h=h: basin[u] == h))
            add_emoji(ax, ctx, h, pos[h], 56, 0.9)
            add_emoji(ax, ctx, root, pos[root], 56, 0.9, ring=True)
            frame(ax, disp)
            ax.set_title(f"{by_id[h]['name']} · {flow[h]} emoji", fontsize=20, color=INK, loc="left")
        for ax in list(axes.flat)[len(facets):]:
            ax.set_axis_off()
        fig.suptitle(f"The {len(facets)} largest basins draining into the {by_id[root]['name']}", fontsize=28, color=INK, x=0.01, ha="left")
        fig.tight_layout(rect=(0, 0, 1, 0.96))
        durable.atomic_write_bytes(ctx.path("flow", "basins.png"), png(fig))
        plt.close(fig)

        rows_out = [{"id": i, "parent": parent[i], "flow": flow[i], "basin": basin.get(i), "x": round(float(pos[i][0]), 5), "y": round(float(pos[i][1]), 5)} for i in ids]
        durable.atomic_write_jsonl(ctx.path("flow", "flow.jsonl"), rows_out)
        log.info("flowmap: %d basins, largest %s", len(heads), ", ".join(f"{by_id[h]['char']} {flow[h]}" for h in heads[:5]))
