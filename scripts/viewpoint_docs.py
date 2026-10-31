import argparse
import io
from pathlib import Path

import numpy as np

from nutmap import durable
from nutmap.context import Ctx
from nutmap.inventory import load_entries
from nutmap.panel import SYSTEM, params_for
from nutmap.video import PLAIN_MAP_BOX, SMALL, Sprites, fit_to_screen
from nutmap.viewpoints import BLENDED, viewpoint_maps, viewpoint_vectors

REPO = Path(__file__).resolve().parent.parent
SITE = "https://nutmap.ic-rbow.workers.dev"
TOUR = "https://www.youtube.com/watch?v=_otlOBEtCgg"
NEIGHBOURS = 15
CLOSEST = 8
STRAGGLERS = 0.02
TUCK = 0.03
IMAGE_QUALITY = 80
IMAGES = "viewpoints"


def nearest_by_meaning(vectors, k):
    similarity = vectors @ vectors.T
    np.fill_diagonal(similarity, -np.inf)
    return np.argsort(-similarity, axis=1, kind="stable")[:, :k]


def nearest_on_map(xy, k):
    gaps = ((xy[:, None] - xy[None]) ** 2).sum(-1)
    np.fill_diagonal(gaps, np.inf)
    return np.argsort(gaps, axis=1, kind="stable")[:, :k]


def shared(a, b):
    return sum(len(set(x) & set(y)) for x, y in zip(a, b)) / a.size


def slug(title):
    return "".join(c for c in title.lower() if c.isalnum() or c in " -_").replace(" ", "-")


def webp(image):
    sink = io.BytesIO()
    image.convert("RGB").save(sink, "WEBP", quality=IMAGE_QUALITY, method=6)
    return sink.getvalue()


def stragglers_tucked_in(xy):
    lo, hi = np.quantile(xy, [STRAGGLERS, 1 - STRAGGLERS], axis=0)
    room = TUCK * (hi - lo)
    below = lo - room * np.log1p(np.maximum(lo - xy, 0) / room)
    above = hi + room * np.log1p(np.maximum(xy - hi, 0) / room)
    return np.where(xy < lo, below, np.where(xy > hi, above, xy))


def render_maps(ctx, layouts, landmarks, folder):
    sprites = Sprites(ctx.data, len(next(iter(layouts.values()))), landmarks, 1)
    sizes = []
    for name, xy in layouts.items():
        screen, _ = fit_to_screen(stragglers_tucked_in(xy)[None], PLAIN_MAP_BOX, SMALL)
        payload = webp(sprites.draw(screen[0]))
        durable.atomic_write_bytes(folder / f"{name}.webp", payload)
        sizes.append(len(payload))
    for stale in folder.glob("*.webp"):
        if stale.stem not in layouts:
            stale.unlink()
    print(f"docs: {len(sizes)} maps, {sum(sizes) / 1e6:.1f} MB in {folder}")


class Page:
    def __init__(self, ctx, examples):
        self.ctx, self.examples = ctx, examples
        self.entries = load_entries(ctx)
        self.index = {e["id"]: k for k, e in enumerate(self.entries)}
        self.angles = ctx.angles()
        self.said = {(r["angle"], r["emoji_id"]): r for r in durable.read_jsonl(ctx.path("panel", "panel.jsonl"))}
        missing = [f"{a['id']}:{e['id']}" for a in self.angles for e in examples if (a["id"], e["id"]) not in self.said]
        if missing:
            raise SystemExit(f"no description for {', '.join(missing)}")
        blend, views = viewpoint_vectors(ctx)
        vectors = dict(zip(durable.read_json(ctx.path("emb", "angles.json")), views))
        self.anchor, self.maps = viewpoint_maps(ctx)
        near_blend = nearest_by_meaning(blend, NEIGHBOURS)
        self.blended_kept = shared(nearest_on_map(self.anchor, NEIGHBOURS), near_blend)
        self.near, self.shared, self.kept = {}, {}, {}
        for a in self.angles:
            near = nearest_by_meaning(vectors[a["id"]], NEIGHBOURS)
            self.near[a["id"]] = near
            self.shared[a["id"]] = shared(near, near_blend)
            self.kept[a["id"]] = shared(nearest_on_map(self.maps[a["id"]], NEIGHBOURS), near)

    def layouts(self):
        return {BLENDED: self.anchor} | {a["id"]: self.maps[a["id"]] for a in self.angles}

    def landmarks(self):
        return [self.index[e["id"]] for e in self.examples]

    def named(self, entry):
        return f"{entry['char']} {entry['name']}"

    def examples_named(self):
        return " and ".join(self.named(e) for e in self.examples)

    def image(self, name, alt):
        return f"![{alt}]({IMAGES}/{name}.webp)"

    def text(self):
        return "\n".join(self.intro() + self.prompt() + self.blended() + self.glance() + [line for a in self.angles for line in self.viewpoint(a)] + self.footer())

    def intro(self):
        n = len(self.angles)
        return [
            f"# The {n} viewpoints",
            "",
            f"Every emoji in nutmap was described {n} times, once from each viewpoint below, by `{self.ctx.cfg['panel']['model']}`. Each description comes with keywords and up to eight linked emoji, each with a reason for the link. Together the descriptions make up most of the race similarity, and each viewpoint's descriptions alone also get a map of their own.",
            "",
            f"For every viewpoint, this page shows the instruction it was given, its map, and what it said about {self.examples_named()}. [The viewpoint tour]({TOUR}) plays the maps in motion, and [the interactive map]({SITE}) switches between them.",
            "",
        ]

    def prompt(self):
        first = self.examples[0]
        message = params_for(self.ctx, self.angles[0], first)["messages"][0]["content"]
        return [
            "## The prompt",
            "",
            "Every request had the same system prompt, with the viewpoint's title and instruction filled in:",
            "",
            "```text",
            SYSTEM,
            "```",
            "",
            f"The emoji came in the user message. For {first['char']} it read:",
            "",
            "```text",
            message,
            "```",
            "",
            "The answer had to be JSON with exactly those three fields.",
            "",
        ]

    def blended(self):
        return [
            "## The blended map",
            "",
            self.image(BLENDED, f"All {len(self.entries):,} emoji placed by their pooled descriptions"),
            "",
            f"The blended map places every emoji by all {len(self.angles)} of its descriptions pooled together. Each viewpoint's map grew out of it through a sweep of aligned UMAP layouts, so an emoji tends to sit in a similar place on every map. {' and '.join(e['char'] for e in self.examples)} are circled on all of them. So that the rest of each map fills its picture, a few far-off emoji are drawn nearer the edges than they really are.",
            "",
            f"No flat map keeps every neighbourhood: the blended map keeps {self.blended_kept:.0%} of each emoji's {NEIGHBOURS} nearest neighbours.",
            "",
        ]

    def glance(self):
        rows = [f"| [{a['title']}](#{slug(a['title'])}) | {self.shared[a['id']]:.0%} | {self.kept[a['id']]:.0%} |" for a in self.angles]
        return [
            "## At a glance",
            "",
            f"Take each emoji's {NEIGHBOURS} nearest neighbours by one viewpoint's descriptions. *Shared* is how many of them the blended descriptions also have; *kept* is how many the viewpoint's own map keeps.",
            "",
            "| Viewpoint | Shared | Kept |",
            "| --- | ---: | ---: |",
            *rows,
            "",
        ]

    def viewpoint(self, angle):
        lines = [
            f"## {angle['title']}",
            "",
            f"> {angle['instruction']}",
            "",
            self.image(angle["id"], f"The {angle['title']} map"),
            "",
            f"Shares {self.shared[angle['id']]:.0%} of each emoji's nearest neighbours with the blended descriptions; its map keeps {self.kept[angle['id']]:.0%}.",
            "",
        ]
        for entry in self.examples:
            said = self.said[(angle["id"], entry["id"])]
            closest = self.near[angle["id"]][self.index[entry["id"]], :CLOSEST]
            lines += [
                f"**{self.named(entry)}.** {said['description']}",
                "",
                f"Keywords: {', '.join(said['keywords'])}.",
                "",
                *(f"- {link['emoji']} {link['relation']}" for link in said["links"]),
                "",
                f"Nearest by these descriptions: {' '.join(self.entries[k]['char'] for k in closest)}",
                "",
            ]
        return lines

    def footer(self):
        return ["---", "", "This page and its maps are generated from the pipeline's data by a script; see [the viewpoints page](../development.md#the-viewpoints-page) in the development notes.", ""]


def main():
    parser = argparse.ArgumentParser(description="Write the viewpoints page: each viewpoint's instruction, its map, and what it said about a few example emoji.")
    parser.add_argument("--out", type=Path, default=REPO / "docs")
    parser.add_argument("--examples", default="1f330,1f409", help="comma-separated emoji ids described in every section; they are also circled on every map")
    args = parser.parse_args()
    ctx = Ctx()
    by_id = {e["id"]: e for e in load_entries(ctx)}
    wanted = [i.strip() for i in args.examples.split(",")]
    unknown = [i for i in wanted if i not in by_id]
    if unknown:
        parser.error(f"unknown emoji ids: {', '.join(unknown)}")
    page = Page(ctx, [by_id[i] for i in wanted])
    render_maps(ctx, page.layouts(), page.landmarks(), args.out / IMAGES)
    durable.atomic_write_text(args.out / "viewpoints.md", page.text())
    print(f"docs: wrote {args.out / 'viewpoints.md'}")


if __name__ == "__main__":
    main()
