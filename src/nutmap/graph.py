import logging
from collections import Counter

import numpy as np
from scipy.sparse.csgraph import connected_components, minimum_spanning_tree
from scipy.sparse import coo_matrix

from . import durable
from .context import PipelineError, Stage, register
from .inventory import load_entries, load_resolver

log = logging.getLogger("nutmap")


def unit(x):
    x = x.astype(np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-12)


def mean_cosine(X, M):
    n = X.shape[1]
    num = np.zeros((n, n), dtype=np.float64)
    cnt = np.zeros((n, n), dtype=np.float64)
    for c in range(X.shape[0]):
        x = unit(X[c])
        m = M[c].astype(np.float64)
        v = np.outer(m, m)
        num += (x @ x.T) * v
        cnt += v
    return num / np.maximum(cnt, 1), cnt > 0


def row_z(S, valid):
    v = valid.copy()
    np.fill_diagonal(v, False)
    cnt = np.maximum(v.sum(1, keepdims=True), 1)
    mean = np.where(v, S, 0).sum(1, keepdims=True) / cnt
    var = np.where(v, (S - mean) ** 2, 0).sum(1, keepdims=True) / cnt
    z = (S - mean) / np.sqrt(np.maximum(var, 1e-12))
    return np.where(v, z, 0.0)


def row_percentile(M):
    n = M.shape[0]
    A = M.copy()
    np.fill_diagonal(A, -np.inf)
    ranks = np.argsort(np.argsort(A, axis=1, kind="stable"), axis=1, kind="stable").astype(np.float64)
    R = (ranks - 1) / max(1, n - 2)
    np.fill_diagonal(R, 1.0)
    return np.clip(R, 0.0, 1.0)


def similarity(ctx):
    gc = ctx.cfg["graph"]
    text = np.load(ctx.path("emb", "text.npy"))
    tmask = np.load(ctx.path("emb", "text_mask.npy"))
    image = np.load(ctx.path("emb", "image.npy"))
    imask = np.load(ctx.path("emb", "image_mask.npy"))
    cldr = np.load(ctx.path("emb", "cldr.npy"))
    n = cldr.shape[0]
    St, Vt = mean_cosine(text, tmask)
    Si, Vi = mean_cosine(image, imask)
    Sc, Vc = mean_cosine(cldr[None], np.ones((1, n), dtype=bool))
    M = gc["w_text"] * row_z(St, Vt) + gc["w_image"] * row_z(Si, Vi) + gc["w_cldr"] * row_z(Sc, Vc)
    R = row_percentile(M)
    S = (R + R.T) / 2
    np.fill_diagonal(S, 1.0)
    S = S.astype(np.float32)
    if not np.array_equal(S, S.T) or S.min() < 0 or S.max() > 1 or not np.all(np.diag(S) == 1):
        raise PipelineError("graph: s_mix failed symmetric/[0,1]/unit-diagonal check")
    return S, {"text": St, "image": Si, "cldr": Sc}


def panel_links(ctx, entries, angle_order):
    idx = {e["id"]: e["index"] for e in entries}
    votes, rels = {}, {}
    for r in durable.read_jsonl(ctx.path("panel", "panel.jsonl")):
        src = r["emoji_id"]
        if src not in idx:
            continue
        for lk in r["links"]:
            dst = lk["id"]
            if not dst or dst == src or dst not in idx:
                continue
            a, b = sorted((idx[src], idx[dst]))
            votes.setdefault((a, b), set()).add(r["angle"])
            rels.setdefault((a, b), []).append((angle_order.get(r["angle"], 99), src, r["angle"], lk["relation"]))
    return votes, rels


@register
class Graph(Stage):
    name = "graph"
    dir = "graph"
    needs = ("inventory", "panel", "embed")

    def config_slice(self, ctx):
        return {"graph": ctx.cfg["graph"], "version": 1}

    def outputs(self, ctx):
        return ["graph/smix.npy", "graph/edges.jsonl", "graph/report.txt"]

    def run(self, ctx):
        gc = ctx.cfg["graph"]
        entries = load_entries(ctx)
        n = len(entries)
        S, channels = similarity(ctx)
        D = 1.0 - S.astype(np.float64) + gc["eps"]
        np.fill_diagonal(D, 0.0)
        edges = {}

        def edge(a, b):
            a, b = (a, b) if a < b else (b, a)
            return edges.setdefault((a, b), {"sources": set(), "votes": 0, "relations": []})

        k = min(gc["k"], n - 1)
        A = S.copy()
        np.fill_diagonal(A, -1)
        knn = np.argsort(-A, axis=1, kind="stable")[:, :k]
        indeg = Counter()
        for i in range(n):
            for j in knn[i]:
                edge(i, int(j))["sources"].add("knn")
                indeg[int(j)] += 1
        angle_order = {a["id"]: i for i, a in enumerate(ctx.angles())}
        votes, rels = panel_links(ctx, entries, angle_order)
        for (a, b), vs in votes.items():
            e = edge(a, b)
            e["sources"].add("panel")
            e["votes"] = len(vs)
            e["relations"] = [{"from": src, "angle": ang, "text": txt} for _, src, ang, txt in sorted(rels[(a, b)])[:3]]
        by_id = {x["id"]: x for x in entries}
        for x in entries:
            for p in x["parts"]:
                e = edge(x["index"], by_id[p]["index"])
                e["sources"].add("parts")
                e["parts_text"] = f"{x['char']} contains {by_id[p]['char']}"
        mst = minimum_spanning_tree(D).tocoo()
        for a, b in zip(mst.row, mst.col):
            edge(int(a), int(b))["sources"].add("mst")
        rows = []
        for (a, b), e in sorted(edges.items()):
            s = float(S[a, b])
            cost = (1.0 - s + gc["eps"]) ** gc["p"]
            cost *= gc["gamma_panel"] ** min(e["votes"], gc["votes_cap"])
            if "parts" in e["sources"]:
                cost *= gc["gamma_parts"]
            cost += gc["hop_cost"]
            relation = e.get("parts_text") or (e["relations"][0]["text"] if e["relations"] else None)
            rows.append(
                {
                    "a": entries[a]["id"],
                    "b": entries[b]["id"],
                    "s": round(s, 6),
                    "cost": float(f"{cost:.6g}"),
                    "sources": sorted(e["sources"]),
                    "votes": e["votes"],
                    "relation": relation,
                    "relations": e["relations"],
                }
            )
        ia = np.array([by_id[r["a"]]["index"] for r in rows])
        ib = np.array([by_id[r["b"]]["index"] for r in rows])
        G = coo_matrix((np.ones(len(rows)), (ia, ib)), shape=(n, n))
        ncomp, _ = connected_components(G, directed=False)
        durable.atomic_save_npy(ctx.path("graph", "smix.npy"), S)
        durable.atomic_write_jsonl(ctx.path("graph", "edges.jsonl"), rows)
        durable.atomic_write_text(ctx.path("graph", "report.txt"), self.report(ctx, entries, rows, ncomp, indeg, channels))
        if ncomp != 1:
            raise PipelineError(f"graph: {ncomp} connected components, expected 1")
        log.info("graph: %d nodes, %d edges, 1 component", n, len(rows))

    def report(self, ctx, entries, rows, ncomp, indeg, channels):
        n = len(entries)
        deg = Counter()
        src_counts = Counter()
        for r in rows:
            deg[r["a"]] += 1
            deg[r["b"]] += 1
            for s in r["sources"]:
                src_counts[s] += 1
        by_index = {e["index"]: e for e in entries}
        by_id = {e["id"]: e for e in entries}
        hist = Counter(min(d // 5 * 5, 100) for d in deg.values())
        out = [f"nodes {n}", f"edges {len(rows)}", f"components {ncomp}", ""]
        out.append("edges by source: " + ", ".join(f"{k}={v}" for k, v in sorted(src_counts.items())))
        out.append("")
        out.append("degree histogram (bucket of 5):")
        out += [f"  {b:>3}-{b + 4:<3} {hist[b]}" for b in sorted(hist)]
        out.append("")
        out.append("top 20 kNN in-degree (hubness):")
        for i, d in indeg.most_common(20):
            e = by_index[i]
            out.append(f"  {e['char']}\t{d}\t{e['name']}")
        out.append("")
        out.append("top 20 total degree:")
        for i, d in deg.most_common(20):
            out.append(f"  {by_id[i]['char']}\t{d}\t{by_id[i]['name']}")
        out.append("")
        out.append("parts checks:")
        resolver = load_resolver(ctx, entries)
        for c in ("🐻‍❄️", "🧑‍🦰", "🧑‍🌾", "🏴‍☠️"):
            i = resolver(c)
            if not i:
                continue
            nbrs = sorted((r for r in rows if i in (r["a"], r["b"]) and "parts" in r["sources"]), key=lambda r: r["cost"])
            out.append(f"  {c}: " + " ".join(by_id[r["b"] if r["a"] == i else r["a"]]["char"] for r in nbrs))
        out.append("")
        out.append("channel similarity means: " + ", ".join(f"{k}={float(v.mean()):.4f}" for k, v in channels.items()))
        return "\n".join(out) + "\n"
