---
pretty_name: "nutmap: emoji races and viewpoint maps"
license: cc-by-4.0
language:
- en
tags:
- emoji
- synthetic
- llm-generated
- semantic-similarity
- graph
- umap
size_categories:
- $size_category
configs:
- config_name: emoji
  data_files: data/emoji.parquet
  default: true
- config_name: viewpoints
  data_files: data/viewpoints.parquet
- config_name: descriptions
  data_files: data/descriptions.parquet
- config_name: steps
  data_files: data/steps.parquet
- config_name: edges
  data_files: data/edges.parquet
- config_name: races
  data_files: data/races.parquet
- config_name: maps
  data_files: data/maps.parquet
---

# nutmap 🌰

Every Unicode emoji, described from $viewpoints_rows viewpoints, linked into a similarity graph, with the cheapest route between every pair of emoji and a one-line reason for every step those routes take.

Think of it as Wikipedia racing for emoji:

> 💻 → 🖱️ *computer mouse is a common accessory used with laptops*
>
> 🖱️ → 🐁 *Mouse doubles as computer hardware for clicking*
>
> 🐁 → 🐿️ *small rounded brown body with bushy tail shape*
>
> 🐿️ → 🌰 *chipmunks stuff acorns and nuts into their cheeks*

- **[Interactive map](https://nutmap.ic-rbow.workers.dev):** pick any emoji as the finish line and watch every other emoji drain into it; switch between the race map, the component maps and each viewpoint's map.
- **[Viewpoints](https://github.com/dpwiz/nutmap/blob/main/docs/viewpoints.md):** every viewpoint's instruction, its map, and what it said about 🌰 and 🐉.
- **Videos:** [the morph](https://www.youtube.com/watch?v=oN4GZm7bwsc), from description similarity to picture similarity and back, and [the viewpoint tour](https://www.youtube.com/watch?v=_otlOBEtCgg), from the blended map out to each viewpoint's map and back.
- **[Source](https://github.com/dpwiz/nutmap):** the pipeline that builds all of this, rebuildable byte for byte.

## Configurations

| Config | Rows | One row per |
| --- | ---: | --- |
| `emoji` | $emoji_rows | emoji: Unicode name, group, CLDR keywords, the emoji it is made of |
| `viewpoints` | $viewpoints_rows | viewpoint: its title and the instruction the model was given |
| `descriptions` | $descriptions_rows | emoji and viewpoint: a description, keywords, and linked emoji with a reason for each link |
| `edges` | $edges_rows | connection in the similarity graph, with its similarity, race cost and why it exists |
| `steps` | $steps_rows | directed step used by at least one race, with a one-line reason |
| `races` | $races_rows | ordered pair of emoji: the next step on the cheapest route from start to finish, hops left and total cost |
| `maps` | $maps_rows | emoji on one of $maps_count 2D maps |

Emoji are identified everywhere by `id`, their code points in lowercase hex joined by underscores (`1f330` is 🌰, `1f43b_200d_2744` is 🐻‍❄️).

### Columns

- **emoji:** `id`, `emoji` (the character), `name`, `group`, `subgroup`, `emoji_version` (the Emoji version that introduced it), `keywords`, `parts` (ids of the emoji a sequence is made of).
- **viewpoints:** `id`, `title`, `instruction`.
- **descriptions:** `emoji_id`, `viewpoint`, `description`, `keywords`, `links` (a list of `emoji`, `emoji_id` and `relation`; `emoji_id` is null for the few links that don't resolve to an emoji in the set).
- **edges:** `a`, `b`, `similarity` (0 to 1), `cost` (what a race pays to cross it), `sources` (`knn`, `panel`, `parts`, `mst`), `votes` (how many viewpoints proposed the link), `relation` and `relations` (up to three viewpoint reasons, each with the viewpoint and the emoji it was written from).
- **steps:** `from_id`, `to_id`, `relation`, `source` (`panel` when a viewpoint's own reason is reused, `parts` for part-of steps, `explain` when the model wrote a reason for that specific step).
- **races:** `finish`, `start`, `next`, `hops`, `cost`. Follow `next` from any start until you reach the finish to get the whole route; join each step on `steps` for its reason.
- **maps:** `map`, `kind` (`race`, `part` or `viewpoint`), `emoji_id`, `x`, `y`.
  - `race` is the map the races are drawn on. `blended`, `pictures` and `names` show the parts of the race similarity on their own; every other map shows one viewpoint's descriptions alone.
  - Every map is centred and scaled to unit size, and rotated to line up with the blended map. The component and viewpoint maps were fitted outward from the blended map, so the same emoji tends to sit in a similar place on each.

## Quick start

```python
from datasets import load_dataset

emoji = load_dataset("$repo", "emoji", split="train").to_pandas().set_index("id")
races = load_dataset("$repo", "races", split="train").to_pandas()
steps = load_dataset("$repo", "steps", split="train").to_pandas()

next_hop = races[races.finish == "1f330"].set_index("start")["next"]
reason = steps.set_index(["from_id", "to_id"])["relation"]

at = "1f4bb"
while at != "1f330":
    step = next_hop[at]
    print(emoji.emoji[at], "→", emoji.emoji[step], reason[at, step])
    at = step
```

## How it was made

1. **Emoji.** All fully-qualified emoji of Unicode 18.0, with names from Unicode's emoji test data and keywords from CLDR 49. Skin-tone variants, and gendered variants that have a gender-neutral form, are folded into the base emoji.
2. **Descriptions.** $panel_model described every emoji once from each viewpoint: a short description, keywords, and up to eight related emoji with a reason for each link.
3. **Similarity.** For each pair of emoji, three channels are compared: the descriptions viewpoint by viewpoint (embedded with $text_model), the Noto Emoji 3D and flat images (embedded with $image_model), and the names and keywords. They are mixed 60/30/10 and turned into a rank within each emoji's own neighbourhood.
4. **Graph.** Each emoji connects to its 12 most similar emoji, to every emoji a viewpoint linked it to, and to its parts; a spanning tree keeps everything reachable.
5. **Races.** A step costs more the less alike its ends are, less the more viewpoints proposed the link, and half for a part-of link. The cheapest routes between all pairs come from Dijkstra's algorithm.
6. **Step reasons.** A step reuses a viewpoint's reason when one exists, says "contains" for part-of steps, and otherwise has a reason written by $explain_model for that step.
7. **Maps.** UMAP lays out the race map. A sweep of aligned UMAP layouts grows the blended-descriptions map out of it, and further sweeps grow each component and viewpoint map out of the blended one.

The whole dataset cost about six dollars of API calls. The repository has the full pipeline, including the batch requests and every answer received.

## Limitations

- **Model-written text.** Descriptions, links and reasons were written by one language model, in English, and may be wrong or loose. Treat them as associations, not facts.
- **Reasons can point the other way.** A viewpoint's reason for linking A to B is reused when a race steps from B to A, so some reasons are written from the far end.
- **Cultural slant.** The viewpoints include internet slang and cross-cultural meanings, so a few descriptions mention innuendo or culture-specific readings.
- **One art style.** The picture similarity comes from Google's Noto Emoji; other vendors draw some emoji very differently. The images themselves are not included.
- **Hubs.** Very general emoji such as 💰, 🏆 and 🌙 attract many links and many races.
- **Flat maps.** Every 2D map keeps only about 37–56% of each emoji's 15 nearest neighbours. The races are worked out on the full similarities, not on the maps.

## Licences

- This dataset is released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The [pipeline](https://github.com/dpwiz/nutmap) that builds it is under the Apache License 2.0.
- Emoji names, groups and CLDR keywords: © Unicode, Inc., under the [Unicode License v3](https://www.unicode.org/license.txt) (SPDX: Unicode-3.0). See the [Unicode terms of use](https://www.unicode.org/terms_of_use.html).
- Descriptions, links and step reasons were generated with $panel_model by Anthropic and have not been checked by hand.
- No images or embeddings are included. The images come from [Noto Emoji](https://github.com/googlefonts/noto-emoji) (Apache License 2.0), and the embeddings can be rebuilt with [$text_model](https://huggingface.co/$text_model) and [$image_model](https://huggingface.co/$image_model).
