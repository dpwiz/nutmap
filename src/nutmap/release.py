import io
import logging
import string

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import durable
from .context import Stage, register
from .inventory import load_entries
from .motion import align
from .viewpoints import BLENDED, CHANNELS, RACE, viewpoint_maps

log = logging.getLogger("nutmap")

STRINGS = pa.list_(pa.string())
LINK = pa.struct([("emoji", pa.string()), ("emoji_id", pa.string()), ("relation", pa.string())])
VOICE = pa.struct([("viewpoint", pa.string()), ("from_id", pa.string()), ("text", pa.string())])
SCHEMAS = {
    "emoji": pa.schema(
        [
            ("id", pa.string()),
            ("emoji", pa.string()),
            ("name", pa.string()),
            ("group", pa.string()),
            ("subgroup", pa.string()),
            ("emoji_version", pa.string()),
            ("keywords", STRINGS),
            ("parts", STRINGS),
        ]
    ),
    "viewpoints": pa.schema([("id", pa.string()), ("title", pa.string()), ("instruction", pa.string())]),
    "descriptions": pa.schema(
        [
            ("emoji_id", pa.string()),
            ("viewpoint", pa.string()),
            ("description", pa.string()),
            ("keywords", STRINGS),
            ("links", pa.list_(LINK)),
        ]
    ),
    "steps": pa.schema([("from_id", pa.string()), ("to_id", pa.string()), ("relation", pa.string()), ("source", pa.string())]),
    "edges": pa.schema(
        [
            ("a", pa.string()),
            ("b", pa.string()),
            ("similarity", pa.float64()),
            ("cost", pa.float64()),
            ("sources", STRINGS),
            ("votes", pa.int8()),
            ("relation", pa.string()),
            ("relations", pa.list_(VOICE)),
        ]
    ),
    "races": pa.schema([("finish", pa.string()), ("start", pa.string()), ("next", pa.string()), ("hops", pa.int8()), ("cost", pa.float32())]),
    "maps": pa.schema([("map", pa.string()), ("kind", pa.string()), ("emoji_id", pa.string()), ("x", pa.float32()), ("y", pa.float32())]),
}
SIZES = ((1_000, "n<1K"), (10_000, "1K<n<10K"), (100_000, "10K<n<100K"), (1_000_000, "100K<n<1M"), (10_000_000, "1M<n<10M"))


def parquet(table):
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="zstd", compression_level=9)
    return sink.getvalue()


def size_category(rows):
    return next((label for limit, label in SIZES if rows < limit), "10M<n<100M")


def emoji_rows(entries):
    return [
        {
            "id": e["id"],
            "emoji": e["char"],
            "name": e["name"],
            "group": e["group"],
            "subgroup": e["subgroup"],
            "emoji_version": e["version"],
            "keywords": e["keywords"],
            "parts": e["parts"],
        }
        for e in entries
    ]


def description_rows(ctx, entries, angles):
    emoji_order = {e["id"]: e["index"] for e in entries}
    angle_order = {a: k for k, a in enumerate(angles)}
    rows = [r for r in durable.read_jsonl(ctx.path("panel", "panel.jsonl")) if r["emoji_id"] in emoji_order and r["angle"] in angle_order]
    rows.sort(key=lambda r: (emoji_order[r["emoji_id"]], angle_order[r["angle"]]))
    return [
        {
            "emoji_id": r["emoji_id"],
            "viewpoint": r["angle"],
            "description": r["description"],
            "keywords": r["keywords"],
            "links": [{"emoji": lk["emoji"], "emoji_id": lk["id"], "relation": lk["relation"]} for lk in r["links"]],
        }
        for r in rows
    ]


def step_rows(ctx):
    return [{"from_id": r["from"], "to_id": r["to"], "relation": r["relation"], "source": r["source"]} for r in durable.read_jsonl(ctx.path("explain", "step_notes.jsonl"))]


def edge_rows(ctx):
    return [
        {
            "a": r["a"],
            "b": r["b"],
            "similarity": r["s"],
            "cost": r["cost"],
            "sources": r["sources"],
            "votes": r["votes"],
            "relation": r["relation"],
            "relations": [{"viewpoint": v["angle"], "from_id": v["from"], "text": v["text"]} for v in r["relations"]],
        }
        for r in durable.read_jsonl(ctx.path("graph", "edges.jsonl"))
    ]


def race_hops(pred):
    n = len(pred)
    finish = np.arange(n)[:, None]
    at = np.broadcast_to(np.arange(n), (n, n)).copy()
    hops = np.zeros((n, n), dtype=np.int8)
    while (moving := at != finish).any():
        hops += moving
        rows, cols = np.nonzero(moving)
        at[rows, cols] = pred[rows, at[rows, cols]]
    return hops


def races_table(ctx, ids):
    pred = np.load(ctx.path("race", "pred.npy")).astype(np.int32)
    dist = np.load(ctx.path("race", "dist.npy"))
    finish, start = np.nonzero(~np.eye(len(ids), dtype=bool))
    names = pa.array(ids, pa.string())

    def named(index):
        return pa.DictionaryArray.from_arrays(pa.array(index.astype(np.int32)), names).dictionary_decode()

    columns = [named(finish), named(start), named(pred[finish, start]), pa.array(race_hops(pred)[finish, start]), pa.array(dist[finish, start])]
    return pa.Table.from_arrays(columns, schema=SCHEMAS["races"])


def map_rows(ctx, ids):
    anchor, views = viewpoint_maps(ctx)
    coords = durable.read_json(ctx.path("map", "umap.json"))
    race = align(np.array([coords[i] for i in ids], dtype=np.float64), anchor)
    layouts = [(RACE, "race", race), (BLENDED, "part", anchor)]
    layouts += [(c, "part", views[c]) for c in CHANNELS if c in views]
    layouts += [(name, "viewpoint", xy) for name, xy in views.items() if name not in CHANNELS]
    return [{"map": name, "kind": kind, "emoji_id": i, "x": float(xy[k, 0]), "y": float(xy[k, 1])} for name, kind, xy in layouts for k, i in enumerate(ids)]


@register
class Release(Stage):
    name = "release"
    dir = "release"
    needs = ("inventory", "panel", "embed", "graph", "race", "explain", "layout", "viewpoints")

    def config_slice(self, ctx):
        return {"release": ctx.cfg["release"], "version": 1}

    def extra_inputs(self, ctx):
        return {"card": durable.sha256_file(self.card_path(ctx))}

    def card_path(self, ctx):
        return ctx.src(ctx.cfg["release"]["card"])

    def outputs(self, ctx):
        return ["release/hub/README.md"] + [f"release/hub/data/{name}.parquet" for name in SCHEMAS]

    def run(self, ctx):
        entries = load_entries(ctx)
        ids = [e["id"] for e in entries]
        angles = ctx.angles()
        rows = {
            "emoji": emoji_rows(entries),
            "viewpoints": [{"id": a["id"], "title": a["title"], "instruction": a["instruction"]} for a in angles],
            "descriptions": description_rows(ctx, entries, [a["id"] for a in angles]),
            "steps": step_rows(ctx),
            "edges": edge_rows(ctx),
            "maps": map_rows(ctx, ids),
        }
        tables = {name: pa.Table.from_pylist(r, schema=SCHEMAS[name]) for name, r in rows.items()} | {"races": races_table(ctx, ids)}
        sizes = {}
        for name in SCHEMAS:
            durable.check_stop()
            payload = parquet(tables[name])
            durable.atomic_write_bytes(ctx.path("release", "hub", "data", f"{name}.parquet"), payload)
            sizes[name] = len(payload)
        models = durable.read_json(ctx.path("emb", "models.json"))
        counts = {name: f"{t.num_rows:,}" for name, t in tables.items()}
        card = string.Template(self.card_path(ctx).read_text("utf-8")).substitute(
            repo=ctx.cfg["release"]["repo"],
            size_category=size_category(max(t.num_rows for t in tables.values())),
            maps_count=len(tables["maps"]) // len(ids),
            panel_model=ctx.cfg["panel"]["model"],
            explain_model=ctx.cfg["explain"]["model"],
            text_model=models["text"].split("@")[0],
            image_model=models["image"].split("@")[0],
            **{f"{name}_rows": count for name, count in counts.items()},
        )
        durable.atomic_write_text(ctx.path("release", "hub", "README.md"), card)
        log.info("release: %s; %.1f MB of parquet", ", ".join(f"{name} {counts[name]}" for name in SCHEMAS), sum(sizes.values()) / 1e6)
