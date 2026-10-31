import io
import logging
import warnings

import numpy as np

from . import durable
from .context import Stage, register
from .inventory import load_entries

log = logging.getLogger("nutmap")


def draw(ctx, entries, xy, out_path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.offsetbox import AnnotationBbox, OffsetImage
    from PIL import Image

    px = ctx.cfg["layout"]["thumb_px"]
    style = ctx.cfg["render"]["styles"][0]
    fig, ax = plt.subplots(figsize=(40, 40), dpi=100)
    ax.scatter(xy[:, 0], xy[:, 1], s=1, alpha=0.2)
    root = ctx.cfg["root"]
    for e, (x, y) in zip(entries, xy):
        p = ctx.path("img", style, f"{e['id']}.png")
        if not p.exists():
            continue
        im = np.asarray(Image.open(p).convert("RGB").resize((px * 2, px * 2)))
        zoom = 1.6 if e["char"] == root else 0.5
        ax.add_artist(AnnotationBbox(OffsetImage(im, zoom=zoom), (x, y), frameon=e["char"] == root, pad=0.1))
    ax.set_axis_off()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    durable.atomic_write_bytes(out_path, buf.getvalue())


@register
class Layout(Stage):
    name = "layout"
    dir = "map"
    needs = ("inventory", "graph")

    def config_slice(self, ctx):
        return {"layout": ctx.cfg["layout"], "version": 1}

    def outputs(self, ctx):
        return ["map/umap.json", "map/umap.png"]

    def run(self, ctx):
        import umap

        lc = ctx.cfg["layout"]
        entries = load_entries(ctx)
        S = np.load(ctx.path("graph", "smix.npy")).astype(np.float64)
        D = 1.0 - S
        np.fill_diagonal(D, 0.0)
        durable.check_stop()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            reducer = umap.UMAP(
                metric="precomputed",
                n_neighbors=min(lc["n_neighbors"], len(entries) - 1),
                min_dist=lc["min_dist"],
                random_state=lc["random_state"],
                init="spectral" if len(entries) > 50 else "random",
            )
            xy = reducer.fit_transform(D)
        xy = (xy - xy.mean(0)) / np.maximum(xy.std(0), 1e-9)
        coords = {e["id"]: [round(float(x), 5), round(float(y), 5)] for e, (x, y) in zip(entries, xy)}
        durable.atomic_write_json(ctx.path("map", "umap.json"), coords, indent=None)
        draw(ctx, entries, xy, ctx.path("map", "umap.png"))
        log.info("layout: %d points", len(coords))
