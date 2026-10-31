import argparse
import json
import os
import time
import warnings
from pathlib import Path

import numpy as np
from PIL import ImageDraw, ImageFont
from scipy.interpolate import CubicSpline
from scipy.ndimage import gaussian_filter1d

from nutmap import durable
from nutmap.motion import eased_alphas, procrustes_chain
from nutmap.video import BIG, FONT, HAIR, HEIGHT, INK, INK2, MAP_BOX, MUTED, PLAIN_MAP_BOX, SMALL, WIDTH, Sprites, encode, fit_to_screen

REPO = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("NUTMAP_DATA") or REPO / "data")
TRACKED = ["1f330", "1f43f", "1f680", "1f1ef_1f1f5", "1f602", "1f355"]
NEIGHBOURS = 15


def unit(x):
    x = x.astype(np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)


def pooled(channel):
    vecs = unit(np.load(DATA / "emb" / f"{channel}.npy"))
    mask = np.load(DATA / "emb" / f"{channel}_mask.npy")
    return unit((vecs * mask[..., None]).sum(0) / np.maximum(mask.sum(0), 1)[:, None])


def fit(out, params):
    import umap

    text, image = pooled("text"), pooled("image")
    n = len(text)
    alphas = eased_alphas(params["layouts"], params["easing"])
    slices = [np.hstack([np.sqrt(1 - a) * text, np.sqrt(a) * image]).astype(np.float32) for a in alphas]
    print(f"fit: {len(alphas)} slices, smallest alpha step {np.diff(alphas).min():.2e}, largest {np.diff(alphas).max():.4f}", flush=True)
    started = time.time()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = umap.AlignedUMAP(
            n_neighbors=NEIGHBOURS,
            min_dist=0.1,
            metric="cosine",
            alignment_regularisation=0.02,
            alignment_window_size=3,
            random_state=params["seed"],
            verbose=True,
        ).fit(slices, relations=[{i: i for i in range(n)} for _ in range(len(alphas) - 1)])
    layouts = np.stack([np.asarray(e, dtype=np.float64) for e in model.embeddings_])
    ids = [e["id"] for e in durable.read_jsonl(DATA / "inventory" / "emoji.jsonl")]
    durable.atomic_save_npy(out / "layouts.npy", layouts)
    durable.atomic_write_json(out / "layouts.json", {"params": params, "alphas": alphas.tolist(), "ids": ids}, indent=None)
    print(f"fit: {len(alphas)} aligned layouts in {(time.time() - started) / 60:.1f} min", flush=True)


def nearest(similarity):
    s = similarity.copy()
    np.fill_diagonal(s, -np.inf)
    return np.argsort(-s, axis=1)[:, :NEIGHBOURS]


def neighbour_fidelity(xy, alpha, text, image):
    truth = nearest((1 - alpha) * (text @ text.T) + alpha * (image @ image.T))
    drawn = nearest(-((xy[:, None] - xy[None]) ** 2).sum(-1))
    return np.mean([len(set(p) & set(q)) / NEIGHBOURS for p, q in zip(truth, drawn)])


def stabilise(out, sigma):
    layouts = np.load(out / "layouts.npy")
    alphas = np.array(json.load(open(out / "layouts.json"))["alphas"])
    chained, turns = procrustes_chain(layouts)
    smooth = gaussian_filter1d(chained, sigma=sigma, axis=0, mode="reflect") if sigma > 0 else chained.copy()
    durable.atomic_save_npy(out / "stable.npy", smooth)

    extent = np.ptp(chained.reshape(-1, 2), axis=0).max()
    frames = len(chained)
    edge = max(1, frames // 12)
    middle = slice(frames // 2 - edge // 2, frames // 2 + edge - edge // 2)
    print(f"stabilise: camera turn per layout median {np.median(turns):.2f} deg, max {turns.max():.2f} deg")
    for name, xy in (("chained", chained), (f"smoothed sigma={sigma:g}", smooth)):
        moves = np.linalg.norm(np.diff(xy, axis=0), axis=2) / extent
        per_layout = np.median(moves, axis=1)
        ends = np.concatenate([per_layout[:edge], per_layout[-edge:]])
        print(f"stabilise: {name:20} median move per layout at the ends {np.median(ends):.4f}, in the middle {np.median(per_layout[middle]):.4f}; largest jump {moves.max():.4f}")
    shift = np.median(np.linalg.norm(smooth - chained, axis=2), axis=1) / extent
    print(f"stabilise: smoothing shifts emoji by a median {np.median(shift):.4f} of the frame (worst layout {shift.max():.4f})")
    text, image = pooled("text"), pooled("image")
    for k in np.linspace(0, frames - 1, 5).round().astype(int):
        print(f"stabilise: layout {k:3d} alpha {alphas[k]:.3f} neighbour fidelity chained {neighbour_fidelity(chained[k], alphas[k], text, image):.3f}, smoothed {neighbour_fidelity(smooth[k], alphas[k], text, image):.3f}")


class LoopRenderer:
    def __init__(self, out, plain, seconds, fps, easing, subpixel):
        self.plain, self.fps = plain, fps
        self.name = f"nutmap_morph_{seconds:g}s_{fps}fps" + ("_plain" if plain else "")
        stored = np.load(out / "stable.npy")
        meta = json.load(open(out / "layouts.json"))
        self.fitted = len(stored)
        self.alphas = eased_alphas(round(seconds * fps / 2), easing)
        self.layouts = CubicSpline(meta["alphas"], stored, axis=0)(self.alphas)
        ids = meta["ids"]
        self.n = len(ids)
        self.tracked = [] if plain else [ids.index(i) for i in TRACKED]
        if plain:
            self.screen, self.pulled = fit_to_screen(self.layouts, PLAIN_MAP_BOX, SMALL)
        else:
            self.screen, self.pulled = fit_to_screen(self.layouts, MAP_BOX, BIG)
        self.sprites = Sprites(DATA, self.n, self.tracked, subpixel)
        self.title_font = ImageFont.truetype(FONT, 30)
        self.note_font = ImageFont.truetype(FONT, 19)
        self.axis_font = ImageFont.truetype(FONT, 22)

    def frame(self, i):
        im = self.sprites.draw(self.screen[i])
        if not self.plain:
            self.draw_captions(ImageDraw.Draw(im), i)
        return im.convert("RGB")

    def draw_captions(self, draw, i):
        draw.text((40, 28), f"{self.n:,} emoji, from text similarity to image similarity", font=self.title_font, fill=INK)
        draw.text((40, 72), f"Aligned UMAP, {self.fitted} layouts per sweep, camera-stabilised, lightly smoothed and interpolated; up to {self.pulled} far-out emoji pulled towards the centre per frame", font=self.note_font, fill=MUTED)
        x0, x1, y = WIDTH / 2 - 300, WIDTH / 2 + 300, HEIGHT - 52
        draw.line((x0, y, x1, y), fill=HAIR, width=4)
        knob = x0 + self.alphas[i] * (x1 - x0)
        draw.ellipse((knob - 11, y - 11, knob + 11, y + 11), fill=INK)
        draw.text((x0 - 16, y), "text", font=self.axis_font, fill=INK2, anchor="rm")
        draw.text((x1 + 16, y), "image", font=self.axis_font, fill=INK2, anchor="lm")

    def stills(self, out, frames):
        for i in frames:
            self.frame(i).save(out / f"{self.name}_{i:04d}.png")
        print(f"render: {len(frames)} stills in {out}")

    def video(self, out):
        order = list(range(len(self.layouts))) + list(range(len(self.layouts) - 1, -1, -1))
        encode((self.frame(i) for i in order), out / f"{self.name}.mp4", self.fps)


def main():
    parser = argparse.ArgumentParser(description="Render the emoji map morphing from text to image similarity and back as a seamless 1080p loop.")
    parser.add_argument("--out", type=Path, default=DATA / "morph")
    parser.add_argument("--layouts", type=int, default=240, help="aligned UMAP layouts fitted per sweep, about 9.5 s of fitting each")
    parser.add_argument("--layout-easing", type=float, default=10.0, help="logistic steepness spacing the fitted layouts between the poles; 0 spaces them evenly")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sigma", type=float, default=3.0, help="Gaussian smoothing along each emoji's path, in layouts")
    parser.add_argument("--refit", action="store_true", help="refit the layouts even if they match the parameters")
    parser.add_argument("--seconds", type=float, default=16.0, help="loop length: one sweep from text to image and one back")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--easing", type=float, default=10.0, help="logistic steepness of the playback sweep; 0 is linear")
    parser.add_argument("--subpixel", type=int, default=4, help="sprite positions per pixel; 1 snaps sprites to whole pixels")
    parser.add_argument("--plain", action="store_true", help="no captions, slider or highlighted emoji")
    parser.add_argument("--stills", help="comma-separated frame indices to save as PNG instead of rendering the video")
    args = parser.parse_args()
    if args.subpixel < 1 or round(args.seconds * args.fps / 2) < 2:
        parser.error("--subpixel must be at least 1 and the loop at least 4 frames long")

    args.out.mkdir(parents=True, exist_ok=True)
    params = {"layouts": args.layouts, "easing": args.layout_easing, "seed": args.seed}
    saved = durable.read_json(args.out / "layouts.json", default={}).get("params")
    if args.refit or saved != params or not (args.out / "layouts.npy").exists():
        fit(args.out, params)
    else:
        print(f"fit: reusing {args.out / 'layouts.npy'}")
    stabilise(args.out, args.sigma)
    renderer = LoopRenderer(args.out, args.plain, args.seconds, args.fps, args.easing, args.subpixel)
    if args.stills:
        renderer.stills(args.out, [int(i) for i in args.stills.split(",")])
    else:
        renderer.video(args.out)


if __name__ == "__main__":
    main()
