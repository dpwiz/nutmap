import logging
import multiprocessing
import signal

import numpy as np

from . import durable
from .context import PipelineError, Stage, register
from .layout import race_distances, race_layout, standardised
from .motion import align, eased_alphas, normalise
from .progress import make_progress

log = logging.getLogger("nutmap")

NEGATIVE_SAMPLE_RATE = 5.0
RACE = "race"
BLENDED = "blended"
BRIDGE_VERSION = 3
CHANNELS = {"pictures": "Pictures", "names": "Names and keywords"}
RESERVED = {RACE, BLENDED, *CHANNELS}


def unit(x):
    x = x.astype(np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)


def pooled(vectors, mask):
    return unit((vectors * mask[..., None]).sum(0) / np.maximum(mask.sum(0), 1)[:, None])


def viewpoint_vectors(ctx):
    mask = np.load(ctx.path("emb", "text_mask.npy"))
    views = unit(np.load(ctx.path("emb", "text.npy")))
    blend = pooled(views, mask)
    for i in range(len(views)):
        views[i][~mask[i]] = blend[~mask[i]]
    return blend, views


def channel_vectors(ctx):
    pictures = pooled(unit(np.load(ctx.path("emb", "image.npy"))), np.load(ctx.path("emb", "image_mask.npy")))
    return {"pictures": pictures, "names": unit(np.load(ctx.path("emb", "cldr.npy")))}


def mix(blend, view, alpha):
    return np.hstack([np.sqrt(1 - alpha) * blend, np.sqrt(alpha) * view]).astype(np.float32)


def cosine_distances(vectors):
    from sklearn.metrics import pairwise_distances

    distances = pairwise_distances(vectors, metric="cosine")
    distances[distances >= 2] = np.inf
    return distances


def distance_graph(distances, n_neighbors, seed):
    from umap.umap_ import fuzzy_simplicial_set

    return fuzzy_simplicial_set(distances, n_neighbors, np.random.RandomState(seed), "precomputed")[0]


def neighbour_graph(vectors, n_neighbors, seed):
    return distance_graph(cosine_distances(vectors), n_neighbors, seed)


def neighbour_scale(distances, k):
    return float(np.median(np.sort(distances, axis=1)[:, k]))


def typed_list(kind, arrays):
    import numba

    out = numba.typed.List.empty_list(kind)
    for a in arrays:
        out.append(a)
    return out


def aligned_sweep(graphs, alphas, vc, start):
    import numba
    from umap.aligned_umap import build_neighborhood_similarities, expand_relations
    from umap.layouts import _optimize_layout_aligned_euclidean_single_epoch
    from umap.umap_ import find_ab_params, make_epochs_per_sample

    int32s, float32s, float64s = numba.types.int32[::1], numba.types.float32[::1], numba.types.float64[::1]
    epochs = vc["epochs"]
    edges = [None if m == 0 else g.tocoo() for m, g in enumerate(graphs)]
    none = np.empty(0, dtype=np.int32)
    heads = typed_list(int32s, [none if e is None else e.row.astype(np.int32) for e in edges])
    tails = typed_list(int32s, [none if e is None else e.col.astype(np.int32) for e in edges])
    per_sample = typed_list(float64s, [np.empty(0) if e is None else make_epochs_per_sample(e.data, epochs) for e in edges])
    relations = expand_relations([{i: i for i in range(graphs[0].shape[0])} for _ in alphas[1:]], vc["window"])
    weights = build_neighborhood_similarities(
        typed_list(int32s, [g.indptr.astype(np.int32) for g in graphs]),
        typed_list(int32s, [g.indices.astype(np.int32) for g in graphs]),
        relations,
    )
    stiffness = 1 + vc["anchor_stiffness"] * np.exp(-alphas / vc["stiffness_fade"])
    weights[1:] *= stiffness[1:, None, None].astype(np.float32)
    first = np.ascontiguousarray(start, dtype=np.float32)
    layouts = typed_list(numba.types.float32[:, ::1], [first.copy() for _ in alphas])
    with np.errstate(over="ignore"):
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


def sweep(blend, view, anchor, vc):
    alphas = eased_alphas(vc["layouts"], vc["easing"])
    graphs = [neighbour_graph(mix(blend, view, a), vc["n_neighbors"], vc["seed"]) for a in alphas]
    return aligned_sweep(graphs, alphas, vc, anchor)


def bridge(blend, similarity, vc, lc):
    k = vc["n_neighbors"]
    near = np.minimum(cosine_distances(mix(blend, np.zeros_like(blend), 0.0)), 2.0)
    near /= neighbour_scale(near, k)
    raced = race_distances(similarity)
    race = race_layout(raced, lc)
    raced /= neighbour_scale(raced, k)
    alphas = eased_alphas(vc["layouts"], vc["easing"])
    graphs = [distance_graph((1 - a) * raced + a * near, k, vc["seed"]) for a in alphas]
    return race, aligned_sweep(graphs, alphas, vc, race)


def ignore_interrupts():
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def sweep_job(job):
    name, blend, other, anchor, vc, lc = job
    if name == RACE:
        return name, bridge(blend, other, vc, lc)
    return name, (None, sweep(blend, other, anchor, vc))


def quantised(xy, lo, hi):
    return np.round((xy - lo) / (hi - lo) * 65535).astype("<u2")


def delta_planes(levels, axis):
    steps = np.concatenate([np.take(levels, [0], axis=axis), np.diff(levels, axis=axis)], axis=axis)
    return np.ascontiguousarray(steps).view(np.uint8).reshape(-1, 2).T.tobytes()


def encode_sweeps(sweeps):
    lo = sweeps.min(axis=(0, 1, 2)).astype(np.float64)
    hi = sweeps.max(axis=(0, 1, 2)).astype(np.float64)
    return lo, hi, delta_planes(quantised(sweeps, lo, hi), 1)


def decode_sweeps(payload, shape, lo, hi):
    planes = np.frombuffer(payload, dtype=np.uint8).reshape(2, -1)
    steps = np.ascontiguousarray(planes.T).view("<u2").reshape(shape)
    levels = np.cumsum(steps, axis=1, dtype="<u2")
    return (lo + levels / 65535 * (hi - lo)).astype(np.float32)


def load_sweeps(folder):
    meta = durable.read_json(folder / "sweeps.json")
    sweeps = decode_sweeps(durable.read_bytes(folder / "sweeps.bin"), meta["shape"], np.array(meta["lo"]), np.array(meta["hi"]))
    return sweeps, np.array(meta["alphas"]), meta["angles"] + meta["channels"] + [meta["bridge"]]


def viewpoint_maps(ctx):
    sweeps, _, names = load_sweeps(ctx.path("viewpoints"))
    anchor = normalise(sweeps[0, 0].astype(np.float64))
    return anchor, {a: align(sweeps[i, -1].astype(np.float64), anchor) for i, a in enumerate(names) if a != RACE}


@register
class Viewpoints(Stage):
    name = "viewpoints"
    dir = "viewpoints"
    needs = ("inventory", "embed", "graph", "layout")

    def fit_settings(self, ctx):
        vc = dict(ctx.cfg["viewpoints"])
        vc.pop("workers", None)
        return vc

    def config_slice(self, ctx):
        return {"viewpoints": self.fit_settings(ctx), "layout": self.layout_settings(ctx), "bridge": BRIDGE_VERSION, "channels": list(CHANNELS), "version": 5}

    def layout_settings(self, ctx):
        return {k: ctx.cfg["layout"][k] for k in ("n_neighbors", "min_dist", "random_state")}

    def outputs(self, ctx):
        return ["viewpoints/sweeps.json", "viewpoints/sweeps.bin"]

    def describe(self, ctx):
        meta = durable.read_json(ctx.path("viewpoints", "sweeps.json"))
        if meta:
            return f"the race-map bridge, {len(meta['channels'])} component and {len(meta['angles'])} viewpoint sweeps, {meta['shape'][1]} layouts each"
        d = ctx.path("viewpoints", "chunks")
        n = sum(1 for _ in d.glob("*.npy")) if d.exists() else 0
        return f"{n} sweeps fitted so far"

    def run(self, ctx):
        vc = self.fit_settings(ctx)
        lc = self.layout_settings(ctx)
        if (lc["n_neighbors"], lc["min_dist"]) != (vc["n_neighbors"], vc["min_dist"]):
            raise PipelineError("viewpoints: n_neighbors and min_dist must match [layout], so the race-map sweep starts from a layout of its own graph")
        angles = durable.read_json(ctx.path("emb", "angles.json"))
        clashes = sorted(RESERVED.intersection(angles))
        if clashes:
            raise PipelineError(f"viewpoints: viewpoint ids {', '.join(clashes)} are reserved for other maps")
        blend, views = viewpoint_vectors(ctx)
        similarity = np.load(ctx.path("graph", "smix.npy"))
        cache = ctx.path("viewpoints", "chunks")
        cache.mkdir(parents=True, exist_ok=True)
        bridge_file = cache / f"{RACE}_{durable.digest([vc, lc, BRIDGE_VERSION, durable.sha256_bytes(blend.tobytes()), durable.sha256_bytes(similarity.tobytes())])[:16]}.npy"
        with make_progress(ctx.console) as prog:
            task = prog.add_task("viewpoints", total=len(angles) + len(CHANNELS) + 1, completed=0, note="")
            self.fit(ctx, prog, task, [(RACE, blend, similarity, None, vc, lc)], {RACE: bridge_file})
            anchor = np.load(bridge_file)[-1]
            targets = dict(zip(angles, views)) | channel_vectors(ctx)
            files = {name: cache / f"{name}_{durable.digest([vc, bridge_file.name, durable.sha256_bytes(v.tobytes())])[:16]}.npy" for name, v in targets.items()}
            self.fit(ctx, prog, task, [(name, blend, v, anchor, vc, lc) for name, v in targets.items()], files)
        expected = set(files.values()) | {bridge_file}
        for stale in cache.glob("*.npy"):
            if stale not in expected:
                stale.unlink()
        sweeps = np.stack([np.load(files[name]) for name in targets] + [np.load(bridge_file)[::-1]])
        lo, hi, payload = encode_sweeps(sweeps)
        durable.atomic_write_packed(ctx.path("viewpoints", "sweeps.bin"), payload)
        meta = {
            "encoding": "uint16 levels between lo and hi, stored as differences from the previous layout, low bytes then high bytes, zstd",
            "shape": list(sweeps.shape),
            "lo": lo.tolist(),
            "hi": hi.tolist(),
            "angles": angles,
            "channels": list(CHANNELS),
            "bridge": RACE,
            "alphas": eased_alphas(vc["layouts"], vc["easing"]).tolist(),
        }
        durable.atomic_write_json(ctx.path("viewpoints", "sweeps.json"), meta)
        log.info("viewpoints: the race-map bridge, then %d component and %d viewpoint sweeps from its blended end, %d layouts each", len(CHANNELS), len(angles), vc["layouts"])

    def fit(self, ctx, prog, task, jobs, files):
        todo = [job for job in jobs if not files[job[0]].exists()]
        prog.update(task, advance=len(jobs) - len(todo))
        if not todo:
            return
        workers = max(1, min(ctx.cfg["viewpoints"]["workers"], len(todo)))
        prog.update(task, note=f"{workers} workers")
        with multiprocessing.get_context("spawn").Pool(workers, initializer=ignore_interrupts) as pool:
            results = pool.imap_unordered(sweep_job, todo)
            for _ in todo:
                while True:
                    durable.check_stop()
                    try:
                        name, (race, layouts) = results.next(timeout=1)
                        break
                    except multiprocessing.TimeoutError:
                        continue
                if race is not None:
                    self.check_race_layout(ctx, race)
                durable.chaos("viewpoints sweep")
                durable.atomic_save_npy(files[name], layouts)
                prog.update(task, advance=1, note=name)

    def check_race_layout(self, ctx, race):
        stored = durable.read_json(ctx.path("map", "umap.json"))
        ids = [e["id"] for e in durable.read_jsonl(ctx.path("inventory", "emoji.jsonl"))]
        expected = np.array([stored[i] for i in ids])
        drift = np.abs(standardised(race) - expected).max()
        if drift > 1e-4:
            raise PipelineError(f"viewpoints: the race map refitted for the bridge differs from map/umap.json by {drift:.2e}")
