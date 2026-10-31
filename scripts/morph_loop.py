import argparse
import os
from pathlib import Path

from PIL import ImageDraw, ImageFont
from scipy.interpolate import CubicSpline

from nutmap import durable
from nutmap.motion import eased_alphas, steadied
from nutmap.video import BIG, FONT, HAIR, HEIGHT, INK, INK2, MAP_BOX, MUTED, PLAIN_MAP_BOX, SMALL, WIDTH, Sprites, encode, fit_to_screen
from nutmap.viewpoints import load_sweeps

REPO = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("NUTMAP_DATA") or REPO / "data")
TRACKED = ["1f330", "1f43f", "1f680", "1f1ef_1f1f5", "1f602", "1f355"]
PICTURES = "pictures"


class LoopRenderer:
    def __init__(self, plain, seconds, fps, easing, subpixel, sigma):
        self.plain, self.fps = plain, fps
        self.name = f"nutmap_morph_{seconds:g}s_{fps}fps" + ("_plain" if plain else "")
        folder = DATA / "viewpoints"
        if not durable.exists(folder / "sweeps.bin"):
            raise SystemExit("data/viewpoints is missing: run `uv run python -m nutmap run` first")
        sweeps, alphas, names = load_sweeps(folder)
        stored = steadied(sweeps[names.index(PICTURES)], sigma)
        self.fitted = len(stored)
        self.alphas = eased_alphas(round(seconds * fps / 2), easing)
        self.layouts = CubicSpline(alphas, stored, axis=0)(self.alphas)
        ids = [e["id"] for e in durable.read_jsonl(DATA / "inventory" / "emoji.jsonl")]
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
        draw.text((40, 72), f"The stored sweep from the shared blended map to pictures alone, {self.fitted} layouts, camera-steadied and interpolated; up to {self.pulled} far-out emoji pulled towards the centre per frame", font=self.note_font, fill=MUTED)
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
    parser = argparse.ArgumentParser(description="Render the emoji map morphing from text to image similarity and back as a seamless 1080p loop, from the pipeline's stored pictures sweep.")
    parser.add_argument("--out", type=Path, default=DATA / "morph")
    parser.add_argument("--seconds", type=float, default=16.0, help="loop length: one sweep from text to image and one back")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--easing", type=float, default=10.0, help="logistic steepness of the playback sweep; 0 is linear")
    parser.add_argument("--sigma", type=float, default=0.75, help="Gaussian smoothing along each emoji's path, in stored layouts; the shared map itself is never moved")
    parser.add_argument("--subpixel", type=int, default=4, help="sprite positions per pixel; 1 snaps sprites to whole pixels")
    parser.add_argument("--plain", action="store_true", help="no captions, slider or highlighted emoji")
    parser.add_argument("--stills", help="comma-separated frame indices to save as PNG instead of rendering the video")
    args = parser.parse_args()
    if args.subpixel < 1 or round(args.seconds * args.fps / 2) < 2:
        parser.error("--subpixel must be at least 1 and the loop at least 4 frames long")
    args.out.mkdir(parents=True, exist_ok=True)
    renderer = LoopRenderer(args.plain, args.seconds, args.fps, args.easing, args.subpixel, args.sigma)
    if args.stills:
        renderer.stills(args.out, [int(i) for i in args.stills.split(",")])
    else:
        renderer.video(args.out)


if __name__ == "__main__":
    main()
