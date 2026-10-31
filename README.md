# nutmap 🌰

A semantic map of 1,734 emoji, and a race from every one of them to the chestnut.

Think of it as Wikipedia racing for emoji. Each step moves to an emoji that is close in meaning or looks, and every step comes with a one-line reason:

> 💻 → 🖱️ *computer mouse is a common accessory used with laptops*
>
> 🖱️ → 🐁 *Mouse doubles as computer hardware for clicking*
>
> 🐁 → 🐿️ *small rounded brown body with bushy tail shape*
>
> 🐿️ → 🌰 *chipmunks stuff acorns and nuts into their cheeks*

**[Explore the map →](https://nutmap.ic-rbow.workers.dev)** Any emoji can be the finish line, not just the chestnut.

![How 1,734 emoji drain into the chestnut](data/flow/flowmap.png)

## What's in it

- **The emoji.** All 1,734 fully-qualified emoji of Unicode 18.0, with their Unicode names, CLDR keywords, and their 3D and flat images from Google's Noto Emoji.
  - Skin-tone variants and gendered variants that have a gender-neutral form are folded into that base emoji.
  - Sequences such as 🐻‍❄️ or 🧑‍🌾 are emoji of their own, linked to their parts (🐻 and ❄️, 🧑 and 🌾). The four hair components (🦰 🦱 🦳 🦲) are included as parts.
- **24 viewpoints on every emoji.** 41,616 short descriptions written by Claude Haiku, one per emoji per viewpoint, each with keywords. Each also links to other emoji it relates to, with a reason for every link: 248,469 links, 99.9% of which resolve to an emoji in the set.
- **Similarity.** Every pair of emoji is scored on three things: how similarly the viewpoints describe them, how alike their images look, and how close their names and keywords are.
- **A similarity graph** of 96,167 connections: nearest neighbours, every link a viewpoint proposed, and part-of links.
- **Races.** For every pair of emoji, the cheapest route between them. Together these routes use 29,698 distinct steps, and every step has a one-line note explaining it.
- **A map.** A 2D layout of all emoji by similarity. For any chosen finish line, the races into it form a drainage network: emoji flow along rivers, and every tributary belongs to a basin.
- **A map per viewpoint.** 24 more layouts, each placing the emoji by one viewpoint's descriptions alone. All of them grow out of one shared map of the blended descriptions, which itself grows out of the race map, so an emoji can be followed from the race map to any viewpoint's map and back. Two more show the other parts of the race similarity on their own, the pictures and the names and keywords, next to the blended descriptions.

### The race to the chestnut

- **Distance:** the average emoji is 3.9 steps from 🌰. 18 are one step away; the farthest are six steps away. One of the long ones:
  > 🤨 → 🧐 → 🔍 → 🔬 → 🦠 → 🍄 → 🌰
- **Basins:** the races arrive through 18 basins. The biggest are 🍁 maple leaf (302 emoji), 🎄 Christmas tree (159), 🍂 fallen leaf (151), 🌲 evergreen tree (146), 🐿️ chipmunk (141) and 🥜 peanuts (132).
- **Flags:** almost all flags (262 of 270) cross over through Canada: 🇯🇵 → 🇨🇳 → 🇨🇦 → 🍁 → 🌰.
- **A few more:**
  - 🚀 → 🪁 → 🍃 → 🍂 → 🌰
  - 🐉 → 🐍 → 🪱 → 🍄 → 🌰
  - 😂 → 😹 → 🐱 → 🐭 → 🐿️ → 🌰
  - 🏴‍☠️ → ☠️ → 👻 → 🎃 → 🌰
  - 🍕 → 🍟 → 🥔 → 🌰

### How the viewpoints differ

The viewpoints really do see the emoji differently. Comparing each emoji's 15 nearest neighbours:
- **Viewpoint against viewpoint:** two viewpoints agree on only 29–43% of them.
- **Closest and farthest from the blend:** Taxonomist shares 62% with the blended descriptions; Colour and shape, the most distinct, shares 38%.
- **Text against pictures:** every viewpoint is closer to the blended descriptions than to the images or the names. Even Colour and shape agrees with the image map on only 34%, less than Literal depiction (43%) or Taxonomist (44%). Describing colours in words doesn't make neighbourhoods look like the pictures.

## How it was made

1. **Inventory.** The emoji list comes from Unicode's emoji test data (Unicode 18.0), and the keywords from CLDR 49 English annotations.
2. **Images.** Every emoji has a 3D and a flat image from Noto Emoji's release for Unicode 18.0. Flat flags Noto doesn't cover come from the public-domain region-flags set that ships with it.
3. **The panel.** Claude Haiku 5.5 described every emoji once from each of 24 viewpoints. Every answer is a description, keywords, and three to eight linked emoji with the reason for each link.
   - A 40-emoji pilot (960 requests) was read and approved before the full run.
   - The full run went through Anthropic's Message Batches API at half price.
   - There were no refusals and no failed requests.
4. **Embeddings.**
   - Each description, and each emoji's name and keywords, was embedded with Qwen3-Embedding-0.6B.
   - Both images were embedded with SigLIP 2 (so400m).
5. **Similarity.** For each pair of emoji, the descriptions are compared viewpoint by viewpoint, and the images style by style.
   - Each of the three channels is normalised, then they are mixed 60% descriptions, 30% images and 10% names and keywords.
   - The mixed score becomes a percentile rank within each emoji's own neighbourhood, so crowded regions such as faces or flags don't drown out the rest.
6. **The graph.** Each emoji connects to:
   - its 12 most similar emoji;
   - every emoji a viewpoint linked it to;
   - its parts.

   A spanning tree keeps everything reachable.
7. **The race.** A step costs more the less alike its two ends are. It gets cheaper the more viewpoints proposed that link, and half price for a part-of link. A tiny cost per step makes equal routes prefer fewer steps. The cheapest routes between all pairs were found with Dijkstra's algorithm.
8. **Step notes.** Every step used by any race gets a one-line reason:
   - the viewpoint's own reason, when a viewpoint proposed that link (23,774 steps);
   - "contains" for part-of steps (180);
   - otherwise a reason Claude Haiku wrote for that specific step (5,744).
9. **Maps.** A UMAP layout of the mixed similarity places every emoji. The races into each finish line are drawn as rivers whose width grows with the number of emoji flowing through.
10. **Viewpoint maps.** The race map stays exactly where it is. A sweep of 60 layouts moves from the race map's similarity to the blended descriptions, fitted together with aligned UMAP so neighbouring layouts stay consistent; its far end becomes the shared blended map. From there, 60 more layouts sweep to each viewpoint alone, and to the pictures and to the names and keywords, the other two parts of the race similarity.
    - Unlike standard aligned UMAP, the first layout of each sweep is fixed (the race map, or the shared blended map), and the optimiser never moves it. The layouts next to it are tied to it more strongly, fading out as the sweep moves away. Only the start is fixed: pinning both ends of a sweep forces a tear wherever the two arrangements disagree.
    - So every viewpoint sweep starts from exactly the same picture and leaves it gently: within the first five layouts emoji move a median 0.5–0.6% of the map's width, and the race-map sweep leaves the race map just as gently. Sweeps fitted separately and lined up afterwards start 5–12% apart.
    - The far ends are full maps of their viewpoint, keeping 37–56% of each emoji's nearest neighbours. The blended map keeps 51%.

All of it took 47,360 Claude Haiku requests and cost $5.95: $0.26 for the pilot, $5.54 for the panel and $0.15 for the step notes.

### The 24 viewpoints

Literal depiction · Colour and shape · Taxonomist · Chef · Naturalist · Five-year-old · Texting and internet slang · Emotional vibe · Seasons and holidays · Japanese origin · Cross-cultural meanings · Storyteller · Punster · Historian · Material and senses · Scale and physics · Trade and economy · Game designer · Poet · Jobs and workplaces · Sports and play · Body and health · Where you would find it · Free word association

## Visualisations

- **[The interactive map](https://nutmap.ic-rbow.workers.dev).**
  - Pan and zoom across all 1,734 emoji, close enough to pull apart even the tightest clusters. Click any emoji to make it the finish line, and the map redraws as rivers draining into it; clear the focus to see just the emoji.
  - Hover over another emoji to see its race to the finish line, with the reason for every step. On a touch screen, tap once to see the race and again to make that emoji the finish line.
  - Swap a pinned race to run it the other way. The route comes back reversed, apart from a handful of equally short alternatives, and steps whose reason was written for one direction get the one written for the other.
  - A side panel lists the basins, searches names and keywords, and shows what the 24 viewpoints said about the focused emoji. Their links lead on to other emoji.
  - A map selector switches between the race map, each part of its similarity on its own (blended descriptions, pictures, names and keywords) and any single viewpoint. Every emoji slides along the fitted sweeps, always through the blended map, and the rivers fade on maps they weren't worked out for.
  - Every view, including the chosen map, has its own URL, so a race can be shared.
- **The drainage map** above: the same view for the chestnut, as a still image.
- **[The morph](https://www.youtube.com/watch?v=oN4GZm7bwsc).** A video loop of the map shifting from description similarity to image similarity and back.
  - It plays the stored sweep from the shared blended map to the pictures alone, so the video ends on the same pictures map as the site.
  - You can watch neighbourhoods regroup as looks take over from meaning; the island of flags, for example, drifts from one side of the map to the other.

[![The emoji map morphing between text and image similarity, on YouTube](https://img.youtube.com/vi/oN4GZm7bwsc/maxresdefault.jpg)](https://www.youtube.com/watch?v=oN4GZm7bwsc)

- **The viewpoint tour.** A 3-minute video loop that starts on the shared blended map, sweeps out to each viewpoint's map in turn, and comes back. Every map is held perfectly still for a second, so each viewpoint has a moment to be looked at.

## Caveats

- **Model-written text.** The descriptions, links and reasons were written by a language model, in English. Some reasons are loose. Others describe the link from the far end: a viewpoint's reason for linking A to B is reused when a race steps from B to A.
- **Folded variants.** Skin tones and gendered variants are not separate emoji; mentions of them count as their base emoji.
- **Hubs.** Very general emoji attract many links: 💰 money bag, 🏆 trophy and 🌙 crescent moon have the most connections.
- **Looks are literal.** The image channel compares pictures, not meanings, so lookalike flags and faces sit very close together.
- **Flat maps.** Every 2D map loses some neighbourhoods: the mixed map, the blended map and the viewpoint maps keep roughly 37–56% of each emoji's 15 nearest neighbours. The races are worked out on the full similarities, not on the maps.

## Sources and licences

- Unicode emoji test data and CLDR annotations: © Unicode, Inc., under the [Unicode terms of use](https://www.unicode.org/terms_of_use.html).
- Emoji images: [Noto Emoji](https://github.com/googlefonts/noto-emoji) by Google, Apache License 2.0. Flag images from region-flags are in the public domain.
- Descriptions, links and step notes: generated with Claude Haiku 5.5 by Anthropic.
- Embedding models: [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) and [SigLIP 2 so400m](https://huggingface.co/google/siglip2-so400m-patch14-384).

Rebuilding, extending or modding the dataset is covered in [development.md](development.md).
