import gc
import hashlib
import logging

import numpy as np

from . import durable
from .context import Stage, register
from .inventory import load_entries
from .progress import make_progress

log = logging.getLogger("nutmap")

FAKE_DIM = 64


def text_payload(rec):
    return f"{rec['description']}\nKeywords: {', '.join(rec['keywords'])}"


def cldr_payload(entry):
    return f"{entry['name']}: {', '.join(entry['keywords'])}" if entry["keywords"] else entry["name"]


def fake_vectors(payloads):
    out = np.empty((len(payloads), FAKE_DIM), dtype=np.float32)
    for i, p in enumerate(payloads):
        data = p if isinstance(p, bytes) else p.encode("utf-8")
        seed = int.from_bytes(hashlib.sha256(data).digest()[:8], "little")
        v = np.random.default_rng(seed).standard_normal(FAKE_DIM)
        out[i] = v / np.linalg.norm(v)
    return out


class Device:
    def __init__(self, ctx):
        self.ctx = ctx
        self.kind = "fake" if ctx.fake_embed else None
        if self.kind:
            return
        import torch

        ec = ctx.cfg["embed"]
        if ec["device"] == "cpu" or not torch.cuda.is_available():
            self.kind = "cpu"
            log.warning("embed: no ROCm GPU available, using CPU fallback models")
            return
        total = torch.cuda.get_device_properties(0).total_memory
        torch.cuda.set_per_process_memory_fraction(min(1.0, ec["vram_gb"] * 2**30 / total), 0)
        self.kind = "cuda:0"
        log.info("embed: using %s (%s), capped at %d GB", self.kind, torch.cuda.get_device_properties(0).gcnArchName, ec["vram_gb"])

    def gpu(self):
        return self.kind == "cuda:0"

    def free(self):
        gc.collect()
        if self.gpu():
            import torch

            torch.cuda.empty_cache()


def with_oom_backoff(fn, batch_size):
    import torch

    while True:
        try:
            return fn(batch_size)
        except torch.OutOfMemoryError:
            torch.cuda.empty_cache()
            if batch_size == 1:
                raise
            batch_size = max(1, batch_size // 2)
            log.warning("embed: out of GPU memory, retrying chunk at batch size %d", batch_size)


class TextEncoder:
    def __init__(self, ctx, device):
        self.device = device
        ec = ctx.cfg["embed"]
        if device.kind == "fake":
            self.name = "fake"
            return
        from sentence_transformers import SentenceTransformer
        import torch

        if device.gpu():
            self.name, rev = ec["text_model"], ec["text_revision"]
            self.model = SentenceTransformer(self.name, revision=rev, device=device.kind, model_kwargs={"dtype": torch.float16})
        else:
            self.name, rev = ec["text_model_cpu"], ec["text_revision_cpu"]
            self.model = SentenceTransformer(self.name, revision=rev, device="cpu")
        self.name = f"{self.name}@{rev}"
        self.batch_size = ec["batch_size"]

    def __call__(self, texts):
        if self.device.kind == "fake":
            return fake_vectors(texts)
        run = lambda bs: self.model.encode(texts, batch_size=bs, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
        return with_oom_backoff(run, self.batch_size).astype(np.float32)


class ImageEncoder:
    def __init__(self, ctx, device):
        self.device = device
        ec = ctx.cfg["embed"]
        if device.kind == "fake":
            self.name = "fake"
            return
        import torch
        from transformers import AutoModel, AutoProcessor

        name, rev = (ec["image_model"], ec["image_revision"]) if device.gpu() else (ec["image_model_cpu"], ec["image_revision_cpu"])
        dtype = torch.float16 if device.gpu() else torch.float32
        self.processor = AutoProcessor.from_pretrained(name, revision=rev)
        self.model = AutoModel.from_pretrained(name, revision=rev, dtype=dtype).to(device.kind if device.gpu() else "cpu").eval()
        self.dtype = dtype
        self.name = f"{name}@{rev}"
        self.batch_size = ec["batch_size"]

    def __call__(self, paths):
        if self.device.kind == "fake":
            return fake_vectors([p.read_bytes() for p in paths])
        import torch
        from PIL import Image

        def run(bs):
            outs = []
            for i in range(0, len(paths), bs):
                ims = [Image.open(p).convert("RGB") for p in paths[i : i + bs]]
                inputs = self.processor(images=ims, return_tensors="pt")
                pv = inputs["pixel_values"].to(self.model.device, dtype=self.dtype)
                with torch.inference_mode():
                    out = self.model.get_image_features(pixel_values=pv)
                feats = getattr(out, "pooler_output", out)
                feats = torch.nn.functional.normalize(feats.float(), dim=-1)
                outs.append(feats.cpu().numpy())
            return np.concatenate(outs, axis=0)

        return with_oom_backoff(run, self.batch_size).astype(np.float32)


def item_digest(payload):
    data = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def embed_channel(ctx, prog, channel, items, encoder, digest_of):
    chunk = ctx.cfg["embed"]["chunk"]
    cdir = ctx.path("emb", "chunks", channel)
    cdir.mkdir(parents=True, exist_ok=True)
    expected = []
    task = prog.add_task(channel, total=len(items), note="")
    for start in range(0, len(items), chunk):
        part = items[start : start + chunk]
        h = durable.digest([encoder.name] + [digest_of(p) for _, p in part])[:16]
        f = cdir / f"{start:06d}_{h}.npy"
        expected.append(f)
        if not f.exists():
            durable.check_stop()
            durable.chaos("embed chunk")
            vecs = encoder([p for _, p in part])
            durable.atomic_save_npy(f, vecs)
        prog.advance(task, len(part))
    for stale in cdir.glob("*.npy"):
        if stale not in expected:
            stale.unlink()
    if not items:
        return None
    return np.concatenate([np.load(f) for f in expected], axis=0)


def scatter(n, items, vecs):
    dim = vecs.shape[1]
    out = np.zeros((n, dim), dtype=np.float32)
    mask = np.zeros(n, dtype=bool)
    for (row, _), v in zip(items, vecs):
        out[row] = v
        mask[row] = True
    return out, mask


@register
class Embed(Stage):
    name = "embed"
    dir = "emb"
    needs = ("inventory", "render", "panel")

    def config_slice(self, ctx):
        ec = dict(ctx.cfg["embed"])
        ec.pop("chunk", None)
        ec.pop("batch_size", None)
        return {"embed": ec, "fake": ctx.fake_embed, "styles": ctx.cfg["render"]["styles"], "version": 1}

    def outputs(self, ctx):
        return [f"emb/{n}" for n in ("ids.json", "text.npy", "text_mask.npy", "angles.json", "cldr.npy", "image.npy", "image_mask.npy", "styles.json", "models.json")]

    def describe(self, ctx):
        d = ctx.path("emb", "chunks")
        return f"{sum(1 for _ in d.rglob('*.npy'))} chunks cached" if d.exists() else ""

    def run(self, ctx):
        entries = load_entries(ctx)
        n = len(entries)
        row = {e["id"]: e["index"] for e in entries}
        panel = durable.read_jsonl(ctx.path("panel", "panel.jsonl"))
        angles = [a["id"] for a in ctx.angles()]
        styles = ctx.cfg["render"]["styles"]
        device = Device(ctx)
        models = {"device": device.kind}
        with make_progress(ctx.console) as prog:
            enc = TextEncoder(ctx, device)
            models["text"] = enc.name
            per_angle = []
            for a in angles:
                items = sorted(((row[r["emoji_id"]], text_payload(r)) for r in panel if r["angle"] == a and r["emoji_id"] in row), key=lambda t: t[0])
                per_angle.append((items, embed_channel(ctx, prog, f"text-{a}", items, enc, item_digest)))
            cldr_items = [(e["index"], cldr_payload(e)) for e in entries]
            cldr_vecs = embed_channel(ctx, prog, "cldr", cldr_items, enc, item_digest)
            dim = cldr_vecs.shape[1]
            text = np.zeros((len(angles), n, dim), dtype=np.float16)
            text_mask = np.zeros((len(angles), n), dtype=bool)
            for i, (items, vecs) in enumerate(per_angle):
                if items:
                    full, m = scatter(n, items, vecs)
                    text[i] = full.astype(np.float16)
                    text_mask[i] = m
            cldr = cldr_vecs.astype(np.float16)
            del enc
            device.free()
            ienc = ImageEncoder(ctx, device)
            models["image"] = ienc.name
            index = durable.read_json(ctx.path("img", "index.json"))
            img_list = []
            for s in styles:
                have = set(index.get(s, []))
                items = [(e["index"], ctx.path("img", s, f"{e['id']}.png")) for e in entries if e["id"] in have]
                vecs = embed_channel(ctx, prog, f"image-{s}", items, ienc, lambda p: item_digest(p.read_bytes()))
                img_list.append(scatter(n, items, vecs) if items else (None, np.zeros(n, dtype=bool)))
            idim = next(v.shape[1] for v, _ in img_list if v is not None)
            image = np.zeros((len(styles), n, idim), dtype=np.float16)
            image_mask = np.zeros((len(styles), n), dtype=bool)
            for i, (v, m) in enumerate(img_list):
                if v is not None:
                    image[i] = v.astype(np.float16)
                    image_mask[i] = m
            del ienc
            device.free()
        durable.atomic_save_npy(ctx.path("emb", "text.npy"), text)
        durable.atomic_save_npy(ctx.path("emb", "text_mask.npy"), text_mask)
        durable.atomic_save_npy(ctx.path("emb", "cldr.npy"), cldr)
        durable.atomic_save_npy(ctx.path("emb", "image.npy"), image)
        durable.atomic_save_npy(ctx.path("emb", "image_mask.npy"), image_mask)
        durable.atomic_write_json(ctx.path("emb", "angles.json"), angles)
        durable.atomic_write_json(ctx.path("emb", "styles.json"), styles)
        durable.atomic_write_json(ctx.path("emb", "ids.json"), [e["id"] for e in entries])
        durable.atomic_write_json(ctx.path("emb", "models.json"), models)
        log.info("embed: text %s (coverage %.3f), image %s (coverage %.3f)", text.shape, text_mask.mean(), image.shape, image_mask.mean())
