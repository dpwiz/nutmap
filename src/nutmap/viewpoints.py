import logging
import multiprocessing
import signal
import warnings

import numpy as np

from . import durable
from .context import Stage, register
from .motion import align, eased_alphas, normalise
from .progress import make_progress

log = logging.getLogger("nutmap")

NEGATIVE_SAMPLE_RATE = 5.0


def unit(x):
    x = x.astype(np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)


def viewpoint_vectors(ctx):
    mask = np.load(ctx.path("emb", "text_mask.npy"))
    views = unit(np.load(ctx.path("emb", "text.npy")))
    blend = unit((views * mask[..., None]).sum(0) / np.maximum(mask.sum(0), 1)[:, None])
    for i in range(len(views)):
        views[i][~mask[i]] = blend[~mask[i]]
    return blend, views


def mix(blend, view, alpha):
    return np.hstack([np.sqrt(1 - alpha) * blend, np.sqrt(alpha) * view]).astype(np.float32)


def neighbour_graph(vectors, n_neighbors, seed):
    from sklearn.metrics import pairwise_distances
    from umap.umap_ import fuzzy_simplicial_set

    distances = pairwise_distances(vectors, metric="cosine")
    distances[distances >= 2] = np.inf
    return fuzzy_simplicial_set(distances, n_neighbors, np.random.RandomState(seed), "precomputed")[0]


def anchor_layout(blend, vc):
    import umap

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        reducer = umap.UMAP(n_neighbors=vc["n_neighbors"], min_dist=vc["min_dist"], metric="cosine", random_state=vc["seed"])
        return reducer.fit_transform(mix(blend, np.zeros_like(blend), 0.0)).astype(np.float32)


def typed_list(kind, arrays):
    import numba

    out = numba.typed.List.empty_list(kind)
    for a in arrays:
        out.append(a)
    return out


def sweep(blend, view, anchor, vc):
    import numba
    from umap.aligned_umap import build_neighborhood_similarities, expand_relations
    from umap.layouts import _optimize_layout_aligned_euclidean_single_epoch
    from umap.umap_ import find_ab_params, make_epochs_per_sample

    int32s, float32s, float64s = numba.types.int32[::1], numba.types.float32[::1], numba.types.float64[::1]
    alphas = eased_alphas(vc["layouts"], vc["easing"])
    epochs = vc["epochs"]
    graphs = [neighbour_graph(mix(blend, view, a), vc["n_neighbors"], vc["seed"]) for a in alphas]
    edges = [g.tocoo() for g in graphs[1:]]
    none = np.empty(0, dtype=np.int32)
    heads = typed_list(int32s, [none] + [e.row.astype(np.int32) for e in edges])
    tails = typed_list(int32s, [none] + [e.col.astype(np.int32) for e in edges])
    per_sample = typed_list(float64s, [np.empty(0)] + [make_epochs_per_sample(e.data, epochs) for e in edges])
    relations = expand_relations([{i: i for i in range(len(blend))} for _ in alphas[1:]], vc["window"])
    weights = build_neighborhood_similarities(
        typed_list(int32s, [g.indptr.astype(np.int32) for g in graphs]),
        typed_list(int32s, [g.indices.astype(np.int32) for g in graphs]),
        relations,
    )
    stiffness = 1 + vc["anchor_stiffness"] * np.exp(-alphas / vc["stiffness_fade"])
    weights[1:] *= stiffness[1:, None, None].astype(np.float32)
    start = np.ascontiguousarray(anchor, dtype=np.float32)
    layouts = typed_list(numba.types.float32[:, ::1], [start.copy() for _ in alphas])
    per_negative = typed_list(float32s, [(p / NEGATIVE_SAMPLE_RATE).astype(np.float32) for p in per_sample])
    next_negative = typed_list(float32s, [(p / NEGATIVE_SAMPLE_RATE).astype(np.float32) for p in per_sample])
    next_sample = typed_list(float32s, [p.astype(np.float32) for p in per_sample])
    rng_state = np.random.RandomState(vc["seed"]).randint(np.iinfo(np.int32).min, np.iinfo(np.int32).max, 3).astype(np.int64)
    a, b = find_ab_params(1.0, vc["min_dist"])
    epoch = numba.njit(_optimize_layout_aligned_euclidean_single_epoch, fastmath=True, parallel=True)
    for n in range(epochs):
        learning_rate = vc["learning_rate"] * (1 - n / epochs)
        epoch(layouts, layouts, heads, tails, per_sample, a, b, weights, relations, rng_state, 1.0, vc["alignment"], 2, True, learning_rate, per_negative, next_negative, next_sample, n)
    return np.stack([np.asarray(x) for x in layouts])


def ignore_interrupts():
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def sweep_job(job):
    angle, blend, view, anchor, vc = job
    return angle, sweep(blend, view, anchor, vc)


def encode_sweeps(sweeps):
    lo = sweeps.min(axis=(0, 1, 2)).astype(np.float64)
    hi = sweeps.max(axis=(0, 1, 2)).astype(np.float64)
    levels = np.round((sweeps - lo) / (hi - lo) * 65535).astype("<u2")
    steps = np.concatenate([levels[:, :1], levels[:, 1:] - levels[:, :-1]], axis=1)
    planes = np.ascontiguousarray(steps).view(np.uint8).reshape(-1, 2).T
    return lo, hi, planes.tobytes()


def decode_sweeps(payload, shape, lo, hi):
    planes = np.frombuffer(payload, dtype=np.uint8).reshape(2, -1)
    steps = np.ascontiguousarray(planes.T).view("<u2").reshape(shape)
    levels = np.cumsum(steps, axis=1, dtype="<u2")
    return (lo + levels / 65535 * (hi - lo)).astype(np.float32)


def load_sweeps(folder):
    meta = durable.read_json(folder / "sweeps.json")
    sweeps = decode_sweeps(durable.read_bytes(folder / "sweeps.bin"), meta["shape"], np.array(meta["lo"]), np.array(meta["hi"]))
    return sweeps, np.array(meta["alphas"]), meta["angles"]


def viewpoint_maps(ctx):
    sweeps, _, angles = load_sweeps(ctx.path("viewpoints"))
    anchor = normalise(sweeps[0, 0].astype(np.float64))
    return anchor, {a: align(sweeps[i, -1].astype(np.float64), anchor) for i, a in enumerate(angles)}


@register
class Viewpoints(Stage):
    name = "viewpoints"
    dir = "viewpoints"
    needs = ("inventory", "embed")

    def fit_settings(self, ctx):
        vc = dict(ctx.cfg["viewpoints"])
        vc.pop("workers", None)
        return vc

    def config_slice(self, ctx):
        return {"viewpoints": self.fit_settings(ctx), "version": 2}

    def outputs(self, ctx):
        return ["viewpoints/sweeps.json", "viewpoints/sweeps.bin"]

    def describe(self, ctx):
        meta = durable.read_json(ctx.path("viewpoints", "sweeps.json"))
        if meta:
            return f"{len(meta['angles'])} sweeps of {meta['shape'][1]} layouts stored"
        d = ctx.path("viewpoints", "chunks")
        n = sum(1 for f in d.glob("*.npy") if not f.name.startswith("anchor_")) if d.exists() else 0
        return f"{n} sweeps fitted so far"

    def run(self, ctx):
        vc = self.fit_settings(ctx)
        angles = durable.read_json(ctx.path("emb", "angles.json"))
        blend, views = viewpoint_vectors(ctx)
        cache = ctx.path("viewpoints", "chunks")
        cache.mkdir(parents=True, exist_ok=True)
        anchor_file = cache / f"anchor_{durable.digest([vc, durable.sha256_bytes(blend.tobytes())])[:16]}.npy"
        if not anchor_file.exists():
            durable.check_stop()
            durable.atomic_save_npy(anchor_file, anchor_layout(blend, vc))
        anchor = np.load(anchor_file)
        files = {a: cache / f"{a}_{durable.digest([vc, anchor_file.name, durable.sha256_bytes(views[i].tobytes())])[:16]}.npy" for i, a in enumerate(angles)}
        todo = [(a, blend, views[i], anchor, vc) for i, a in enumerate(angles) if not files[a].exists()]
        with make_progress(ctx.console) as prog:
            task = prog.add_task("viewpoints", total=len(angles), completed=len(angles) - len(todo), note="")
            if todo:
                workers = max(1, min(ctx.cfg["viewpoints"]["workers"], len(todo)))
                prog.update(task, note=f"{workers} workers")
                with multiprocessing.get_context("spawn").Pool(workers, initializer=ignore_interrupts) as pool:
                    results = pool.imap_unordered(sweep_job, todo)
                    for _ in todo:
                        while True:
                            durable.check_stop()
                            try:
                                angle, layouts = results.next(timeout=1)
                                break
                            except multiprocessing.TimeoutError:
                                continue
                        durable.chaos("viewpoints sweep")
                        durable.atomic_save_npy(files[angle], layouts)
                        prog.update(task, advance=1, note=angle)
        expected = set(files.values()) | {anchor_file}
        for stale in cache.glob("*.npy"):
            if stale not in expected:
                stale.unlink()
        sweeps = np.stack([np.load(files[a]) for a in angles])
        lo, hi, payload = encode_sweeps(sweeps)
        durable.atomic_write_packed(ctx.path("viewpoints", "sweeps.bin"), payload)
        meta = {
            "encoding": "uint16 levels between lo and hi, stored as differences from the previous layout, low bytes then high bytes, zstd",
            "shape": list(sweeps.shape),
            "lo": lo.tolist(),
            "hi": hi.tolist(),
            "angles": angles,
            "alphas": eased_alphas(vc["layouts"], vc["easing"]).tolist(),
        }
        durable.atomic_write_json(ctx.path("viewpoints", "sweeps.json"), meta)
        log.info("viewpoints: %d sweeps of %d layouts from a shared blended-text anchor", len(angles), vc["layouts"])
