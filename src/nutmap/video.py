import json
import math
import subprocess
import time
from pathlib import Path

import matplotlib
import numpy as np
from PIL import Image, ImageDraw

WIDTH, HEIGHT = 1920, 1080
SMALL, BIG = 22, 42
RING_RADIUS, RING_WIDTH, RING_SUPERSAMPLE = BIG * 0.62, 2, 8
MAP_BOX = (60, 90, WIDTH - 60, HEIGHT - 110)
PLAIN_MAP_BOX = (60, 60, WIDTH - 60, HEIGHT - 60)
SURFACE = (252, 252, 251, 255)
INK = (11, 11, 11, 255)
INK2 = (82, 81, 78, 255)
MUTED = (137, 135, 129, 255)
HAIR = (195, 194, 183, 255)
FONT = str(Path(matplotlib.get_data_path()) / "fonts/ttf/DejaVuSans.ttf")


def fit_to_screen(layouts, box, sprite_size):
    points = layouts.reshape(-1, 2)
    center = np.median(points, axis=0)
    reach = np.percentile(np.linalg.norm(points - center, axis=1), 98)
    softness = 0.12 * reach

    def pull_in(xy):
        d = xy - center
        r = np.linalg.norm(d, axis=1)
        k = np.ones_like(r)
        far = r > reach
        k[far] = (reach + softness * np.log1p((r[far] - reach) / softness)) / r[far]
        return d * k[:, None]

    shown = np.stack([pull_in(xy) for xy in layouts])
    shown[..., 1] *= -1
    lo = shown.reshape(-1, 2).min(axis=0)
    hi = shown.reshape(-1, 2).max(axis=0)
    x0, y0, x1, y1 = box
    scale = min((x1 - x0 - sprite_size) / (hi[0] - lo[0]), (y1 - y0 - sprite_size) / (hi[1] - lo[1]))
    offset = np.array([(x0 + x1) / 2, (y0 + y1) / 2]) - (lo + hi) / 2 * scale
    pulled = int(max((np.linalg.norm(xy - center, axis=1) > reach).sum() for xy in layouts))
    return shown * scale + offset, pulled


def shifted_variants(image, size, steps):
    pad = image.width
    padded = Image.new("RGBA", (image.width + 2 * pad, image.height + 2 * pad))
    padded.paste(image, (pad, pad))
    source_per_pixel = image.width / size
    side = math.ceil(size) + 2
    variants = []
    for row in range(steps):
        variants.append([])
        for col in range(steps):
            left = pad - (1 + col / steps) * source_per_pixel
            top = pad - (1 + row / steps) * source_per_pixel
            box = (left, top, left + side * source_per_pixel, top + side * source_per_pixel)
            variants[-1].append(padded.resize((side, side), Image.LANCZOS, box=box))
    return variants


def ring_image():
    side = round(2 * RING_RADIUS * RING_SUPERSAMPLE)
    ring = Image.new("RGBA", (side, side))
    ImageDraw.Draw(ring).ellipse((0, 0, side - 1, side - 1), fill=SURFACE, outline=INK, width=RING_WIDTH * RING_SUPERSAMPLE)
    return ring


class Sprites:
    def __init__(self, data, n, tracked, steps):
        self.n, self.tracked, self.steps = n, tracked, steps
        web = data / "web" / "public"
        if not (web / "sprite.webp").exists():
            raise SystemExit("data/web/public/sprite.webp is missing: run `uv run python -m nutmap web` first")
        started = time.time()
        sprite = json.load(open(web / "meta.json"))["sprite"]
        tile, cols = sprite["tile"], sprite["cols"]
        sheet = Image.open(web / "sprite.webp").convert("RGBA")
        tiles = [sheet.crop(((k % cols) * tile, (k // cols) * tile, (k % cols + 1) * tile, (k // cols + 1) * tile)) for k in range(n)]
        self.small = [shifted_variants(t, SMALL, steps) for t in tiles]
        self.big = {k: shifted_variants(tiles[k], BIG, steps) for k in tracked}
        ring = ring_image()
        self.ring_size = ring.width / RING_SUPERSAMPLE
        self.ring = shifted_variants(ring, self.ring_size, steps)
        print(f"render: sprites placed on a 1/{steps} px grid, {(n + len(tracked)) * steps**2} variants in {time.time() - started:.1f} s")

    def place(self, im, variants, size, point):
        x, col = divmod(math.floor((point[0] - size / 2) * self.steps), self.steps)
        y, row = divmod(math.floor((point[1] - size / 2) * self.steps), self.steps)
        im.alpha_composite(variants[row][col], (x - 1, y - 1))

    def draw(self, points):
        im = Image.new("RGBA", (WIDTH, HEIGHT), SURFACE)
        tracked = set(self.tracked)
        for k in range(self.n):
            if k not in tracked:
                self.place(im, self.small[k], SMALL, points[k])
        for k in self.tracked:
            self.place(im, self.ring, self.ring_size, points[k])
            self.place(im, self.big[k], BIG, points[k])
        return im


def encode(frames, target, fps):
    partial = target.with_name(target.name + ".part")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(fps), "-i", "-",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-f", "mp4", str(partial),
    ]
    started = time.time()
    encoder = subprocess.Popen(command, stdin=subprocess.PIPE)
    count = 0
    for frame in frames:
        encoder.stdin.write(frame.tobytes())
        count += 1
    encoder.stdin.close()
    if encoder.wait() != 0:
        raise SystemExit(f"ffmpeg failed with exit code {encoder.returncode}")
    partial.replace(target)
    print(f"render: {count} frames ({count / fps:.1f} s at {fps} fps) in {time.time() - started:.1f} s -> {target} ({target.stat().st_size / 1e6:.1f} MB)")
