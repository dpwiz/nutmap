import argparse
import os
import tomllib
from pathlib import Path

import numpy as np
from PIL import ImageDraw, ImageFont
from scipy.interpolate import CubicSpline

from nutmap import durable
from nutmap.motion import eased_alphas, steadied
from nutmap.video import FONT, INK, MAP_BOX, MUTED, PLAIN_MAP_BOX, SMALL, Sprites, encode, fit_to_screen
from nutmap.viewpoints import CHANNELS, RACE, load_sweeps

REPO = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("NUTMAP_DATA") or REPO / "data")
BLENDED = "Blended text"


class Tour:
    def __init__(self, only, hold, move, steepness, fps, sigma):
        folder = DATA / "viewpoints"
        if not durable.exists(folder / "sweeps.bin"):
            raise SystemExit("data/viewpoints is missing: run `uv run python -m nutmap run` first")
        sweeps, alphas, angles = load_sweeps(folder)
        titles = {a["id"]: a["title"] for a in tomllib.loads((REPO / "angles.toml").read_text("utf-8"))["angle"]} | CHANNELS | {RACE: "Race map"}
        chosen = only or [a for a in angles if a != RACE and a not in CHANNELS]
        unknown = sorted(set(chosen) - set(angles))
        if unknown:
            raise SystemExit(f"unknown viewpoints: {', '.join(unknown)}")
        held = round(hold * fps)
        path = eased_alphas(round(move * fps) + 1, steepness)[1:]
        frames, captions = [], []
        for number, angle in enumerate(chosen, 1):
            layouts = steadied(sweeps[angles.index(angle)], sigma)
            spline = CubicSpline(alphas, layouts, axis=0)
            title = titles.get(angle, angle)
            legs = [
                ([layouts[0]] * held, BLENDED),
                (list(spline(path)), f"{BLENDED} → {title}"),
                ([layouts[-1]] * held, title),
                (list(spline(1 - path)), f"{title} → {BLENDED}"),
            ]
            for leg, caption in legs:
                frames += leg
                captions += [(caption, number)] * len(leg)
        self.frames = np.stack(frames)
        self.captions = captions
        self.count = len(chosen)
        self.fps = fps

    def seconds(self):
        return len(self.frames) / self.fps


class TourRenderer:
    def __init__(self, tour, plain, subpixel):
        self.tour, self.plain = tour, plain
        n = tour.frames.shape[1]
        self.screen, _ = fit_to_screen(tour.frames, PLAIN_MAP_BOX if plain else MAP_BOX, SMALL)
        self.sprites = Sprites(DATA, n, [], subpixel)
        self.title_font = ImageFont.truetype(FONT, 30)
        self.note_font = ImageFont.truetype(FONT, 19)
        self.n = n
        self.name = f"nutmap_viewpoints_{tour.count}_{tour.seconds():g}s" + ("_plain" if plain else "")

    def frame(self, i):
        im = self.sprites.draw(self.screen[i])
        if not self.plain:
            caption, number = self.tour.captions[i]
            draw = ImageDraw.Draw(im)
            draw.text((40, 28), caption, font=self.title_font, fill=INK)
            draw.text((40, 72), f"Viewpoint {number} of {self.tour.count} · {self.n:,} emoji laid out by one viewpoint's descriptions, starting from the shared blended-text map", font=self.note_font, fill=MUTED)
        return im.convert("RGB")

    def stills(self, out, frames):
        for i in frames:
            self.frame(i).save(out / f"{self.name}_{i:05d}.png")
        print(f"render: {len(frames)} stills in {out}")

    def video(self, out):
        encode((self.frame(i) for i in range(len(self.tour.frames))), out / f"{self.name}.mp4", self.tour.fps)


def main():
    parser = argparse.ArgumentParser(description="Render a tour of the viewpoint maps: from the shared blended-text map out to each viewpoint and back, as a seamless 1080p loop.")
    parser.add_argument("--out", type=Path, default=DATA / "morph")
    parser.add_argument("--only", help="comma-separated viewpoint ids to visit, in order; all of them by default")
    parser.add_argument("--hold", type=float, default=1.0, help="seconds each map is held still")
    parser.add_argument("--move", type=float, default=3.0, help="seconds each sweep between maps takes")
    parser.add_argument("--steepness", type=float, default=12.0, help="logistic steepness of each sweep; higher leaves and arrives more gently")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--sigma", type=float, default=0.75, help="Gaussian smoothing along each emoji's path, in fitted layouts; the shared map is never moved")
    parser.add_argument("--subpixel", type=int, default=4, help="sprite positions per pixel; 1 snaps sprites to whole pixels")
    parser.add_argument("--plain", action="store_true", help="no captions")
    parser.add_argument("--stills", help="comma-separated frame indices to save as PNG instead of rendering the video")
    args = parser.parse_args()
    if args.subpixel < 1 or args.hold < 0 or args.move <= 0:
        parser.error("--subpixel must be at least 1, --hold not negative and --move positive")
    args.out.mkdir(parents=True, exist_ok=True)
    only = [a.strip() for a in args.only.split(",")] if args.only else None
    tour = Tour(only, args.hold, args.move, args.steepness, args.fps, args.sigma)
    print(f"tour: {tour.count} viewpoints, {len(tour.frames)} frames, {tour.seconds():.1f} s")
    renderer = TourRenderer(tour, args.plain, args.subpixel)
    if args.stills:
        renderer.stills(args.out, [int(i) for i in args.stills.split(",")])
    else:
        renderer.video(args.out)


if __name__ == "__main__":
    main()
