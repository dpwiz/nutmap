import logging

from . import durable
from .context import Stage, register
from .inventory import load_entries
from .viewpoints import viewpoint_maps

log = logging.getLogger("nutmap")


def rounded(xy):
    return [round(float(xy[0]), 5), round(float(xy[1]), 5)]


@register
class Export(Stage):
    name = "export"
    dir = "out"
    needs = ("inventory", "embed", "graph", "race", "explain", "layout", "viewpoints")

    def config_slice(self, ctx):
        return {"version": 2}

    def outputs(self, ctx):
        return ["out/nutmap.json"]

    def run(self, ctx):
        entries = load_entries(ctx)
        coords = durable.read_json(ctx.path("map", "umap.json"))
        race = {r["id"]: r for r in durable.read_jsonl(ctx.path("race", "root.jsonl"))}
        notes = {(n["from"], n["to"]): n["relation"] for n in durable.read_jsonl(ctx.path("explain", "edge_notes.jsonl"))}
        blended, views = viewpoint_maps(ctx)
        nodes = []
        for e in entries:
            r = race[e["id"]]
            nodes.append(
                {
                    "id": e["id"],
                    "char": e["char"],
                    "name": e["name"],
                    "group": e["group"],
                    "subgroup": e["subgroup"],
                    "keywords": e["keywords"],
                    "parts": e["parts"],
                    "x": coords[e["id"]][0],
                    "y": coords[e["id"]][1],
                    "sim_to_root": r["sim_to_root"],
                    "hops": r["hops"],
                    "cost": r["cost"],
                    "parent": r["parent"],
                    "parent_relation": notes.get((e["id"], r["parent"])),
                    "viewpoint_maps": {"blended": rounded(blended[e["index"]]), **{a: rounded(xy[e["index"]]) for a, xy in views.items()}},
                }
            )
        edges = [
            {k: r[k] for k in ("a", "b", "s", "cost", "sources", "votes", "relation")}
            for r in durable.read_jsonl(ctx.path("graph", "edges.jsonl"))
        ]
        meta = {
            "root": ctx.cfg["root"],
            "angles": [a["id"] for a in ctx.angles()],
            "models": durable.read_json(ctx.path("emb", "models.json")),
            "panel_model": ctx.cfg["panel"]["model"],
            "graph": ctx.cfg["graph"],
            "viewpoint_maps": {"anchor": "blended", "angles": list(views), "aligned_to": "blended", "layouts_per_sweep": ctx.cfg["viewpoints"]["layouts"]},
            "counts": {"nodes": len(nodes), "edges": len(edges)},
        }
        durable.atomic_write_json(ctx.path("out", "nutmap.json"), {"meta": meta, "nodes": nodes, "edges": edges}, indent=None)
        log.info("export: %d nodes, %d edges -> %s", len(nodes), len(edges), ctx.path("out", "nutmap.json"))
