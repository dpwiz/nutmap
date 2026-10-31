import logging

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from . import durable
from .context import PipelineError, Stage, register
from .inventory import load_entries, load_resolver

log = logging.getLogger("nutmap")


def load_graph(ctx, entries):
    idx = {e["id"]: e["index"] for e in entries}
    rows = durable.read_jsonl(ctx.path("graph", "edges.jsonl"))
    n = len(entries)
    a = np.array([idx[r["a"]] for r in rows])
    b = np.array([idx[r["b"]] for r in rows])
    w = np.array([r["cost"] for r in rows], dtype=np.float64)
    G = csr_matrix((np.concatenate([w, w]), (np.concatenate([a, b]), np.concatenate([b, a]))), shape=(n, n))
    lookup = {}
    for r in rows:
        lookup[(r["a"], r["b"])] = r
        lookup[(r["b"], r["a"])] = r
    return G, lookup


def chain(pred_row, j, root):
    out = [j]
    while out[-1] != root:
        nxt = int(pred_row[out[-1]])
        if nxt < 0:
            return None
        out.append(nxt)
    return out


@register
class Race(Stage):
    name = "race"
    dir = "race"
    needs = ("inventory", "graph")

    def config_slice(self, ctx):
        return {"root": ctx.cfg["root"], "probes": ctx.cfg["race"]["probes"], "version": 1}

    def outputs(self, ctx):
        return ["race/dist.npy", "race/pred.npy", "race/root.jsonl", "race/probes.txt"]

    def run(self, ctx):
        entries = load_entries(ctx)
        resolver = load_resolver(ctx, entries)
        n = len(entries)
        G, lookup = load_graph(ctx, entries)
        durable.check_stop()
        dist, pred = dijkstra(G, directed=False, return_predecessors=True)
        if not np.isfinite(dist).all():
            raise PipelineError("race: graph is not connected")
        root_id = resolver(ctx.cfg["root"])
        root = next(e["index"] for e in entries if e["id"] == root_id)
        S = np.load(ctx.path("graph", "smix.npy"))
        rows = []
        for e in entries:
            j = e["index"]
            c = chain(pred[root], j, root)
            rows.append(
                {
                    "id": e["id"],
                    "sim_to_root": round(float(S[root, j]), 6),
                    "cost": float(f"{dist[root, j]:.6g}"),
                    "hops": len(c) - 1,
                    "parent": entries[c[1]]["id"] if len(c) > 1 else None,
                    "chain": [entries[x]["id"] for x in c],
                }
            )
        by_id = {e["id"]: e for e in entries}
        lines = []
        for p in ctx.cfg["race"]["probes"]:
            i = resolver(p)
            if not i:
                continue
            r = next(x for x in rows if x["id"] == i)
            steps = [by_id[r["chain"][0]]["char"]]
            for u, v in zip(r["chain"], r["chain"][1:]):
                rel = lookup[(u, v)]["relation"]
                steps.append(f" → {by_id[v]['char']}" + (f" ({rel})" if rel else ""))
            lines.append(f"{p}  hops={r['hops']} cost={r['cost']:.4g} sim={r['sim_to_root']:.3f}\n  " + "".join(steps))
        durable.atomic_save_npy(ctx.path("race", "dist.npy"), dist.astype(np.float32))
        durable.atomic_save_npy(ctx.path("race", "pred.npy"), pred.astype(np.int16 if n < 32767 else np.int32))
        durable.atomic_write_jsonl(ctx.path("race", "root.jsonl"), rows)
        durable.atomic_write_text(ctx.path("race", "probes.txt"), "\n\n".join(lines) + "\n")
        hops = [r["hops"] for r in rows]
        log.info("race: root %s, max hops %d, mean hops %.2f", ctx.cfg["root"], max(hops), sum(hops) / len(hops))
