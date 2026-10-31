# Development

This covers checking the project out, rebuilding the dataset, and changing it. For what the dataset is, see the [README](README.md).

## What you need

- **Git with Git LFS.** The stored data is kept in LFS, zstd-compressed.
- **[uv](https://docs.astral.sh/uv/).** It installs Python 3.12 and all dependencies, including PyTorch for ROCm.
- **For an exact rebuild, an AMD GPU with ROCm.** The reference machine is a Radeon RX 9070 on ROCm 7.2.
  - The embedding stage uses at most 8 GB of video memory, on the first GPU unless `HIP_VISIBLE_DEVICES` says otherwise.
  - Without a usable GPU it falls back to smaller CPU models (see [Rebuilding elsewhere](#rebuilding-elsewhere)).
- **[Bun](https://bun.sh)** for the site.
- **ffmpeg** for the videos.
- **An Anthropic API key**, only if you change something that makes the pipeline ask Claude new questions. It's read from `ANTHROPIC_API_KEY`; with direnv, a `.env` file in the project folder is loaded automatically. A plain rebuild never calls the API.

## Checking out

```bash
git lfs install
```
```bash
git clone https://github.com/dpwiz/nutmap
```
```bash
cd nutmap
```

Noto Emoji is a submodule pinned to its Unicode 18.0 release. It's marked shallow, so only that one commit is fetched, about 1.1 GB.

The submodule also stores 788 MB of 3D emoji fonts in LFS that nothing here uses. Skip them, and tell the submodule to keep skipping them:

```bash
GIT_LFS_SKIP_SMUDGE=1 git submodule update --init
```
```bash
git -C third_party/noto-emoji config lfs.fetchexclude '3D/fonts/**'
```

Then install the environment. The first time downloads a ROCm build of PyTorch, which is large:

```bash
uv sync
```

## Rebuilding

Git stores what can't be recomputed, plus the one result that's slow to recompute:
- the downloaded Unicode and CLDR sources and the emoji inventory built from them;
- the pilot and its approval;
- every Claude answer, and the records of the batches that produced them;
- the step notes;
- the map and flowmap images;
- the viewpoint maps, which take about 40 minutes of CPU to fit.

Everything else (emoji images, embeddings, similarity graph, races, site data, export and the Hugging Face release) is ignored by git. One command rebuilds it:

```bash
uv run python -m nutmap run
```

On the reference machine this takes about five minutes: about one minute to render images from the submodule and three to embed on the GPU. The embedding models download from Hugging Face on first use, at pinned revisions, a few GB in total. `viewpoints --force` refits the viewpoint maps: about 40 minutes, the race-map sweep first and then six sweeps at a time, at about 6½ minutes each.

The result is byte-identical to the original. That was checked by rebuilding from a fresh copy of the tracked files with no API key set.

### Compressed storage

Text data is stored zstd-compressed, as a `.zst` next to where the plain file would be: 66 MB of journals, notes, sources and inventory take 11 MB. The viewpoint maps have their own compact form, 6 MB:
- positions are rounded to 16-bit steps between bounds shared by all 24 sweeps, which loses under 0.001% of the map's width and keeps every sweep starting on a bit-identical layout;
- each layout is stored as its difference from the previous one;
- low and high bytes are stored separately, then the whole thing is compressed with zstd.

The pipeline reads `.zst` files directly, so a fresh clone needs no unpacking:
- **Working copies:** stages write plain working copies, which git ignores.
- **Packing:** `run` and every stage command finish by packing changed working copies into their `.zst`, deterministically, so a rebuild reproduces them byte for byte. `pack` does it by hand, and `status` warns about changes that haven't been packed.
- **Plain and packed together:** when both exist, the newer one is read. A working copy left over from an older run never shadows newer data that arrived through git, and packing never overwrites it. A journal that's about to be appended to first takes over the content of a newer packed copy.

### Commands

- **`run`** goes through every stage in order and skips stages that are up to date. `--until <stage>` stops after that stage.
- **`<stage>`** runs one stage. `--force` reruns it even when it's up to date.
- **`status`** shows every stage's state, money spent so far, batch progress and an ETA. It works while a run is in progress. `--remote` refreshes batch counts from the API.
- **`approve`** accepts the pilot report so the full panel may run. A run that is waiting for approval stops with exit code 3.
- **`pack`** packs any changed working copies of the stored data into their compressed form. `run` and the stage commands do this on their own.

Every command is `uv run python -m nutmap <command>`.

### Rebuilding elsewhere

The exact rebuild depends on the GPU models giving identical embeddings.

On a machine without ROCm, the embedding stage silently uses the CPU models instead (BGE base and SigLIP base). The map is still sensible, but the races change. Stages decide whether they're up to date from their inputs, not their outputs, so the stored step notes, map and flowmap will look current even though they no longer match.

After the first `run`, force the stages downstream of the races, in this order:
1. `explain --force` writes notes for the new steps. This calls Claude, but only for steps it hasn't seen, which costs cents.
2. `layout --force` redraws the map.
3. `flowmap --force` redraws the drainage map.
4. `web --force` refreshes the site's data.
5. `viewpoints --force` refits the viewpoint maps from the new embeddings, which takes about 40 minutes.
6. `export --force` rewrites the combined export.
7. `release --force` rebuilds the Hugging Face release.

NVIDIA GPUs would need PyTorch from a CUDA index instead of the ROCm one; that hasn't been tried.

## The pipeline

Stages run in this order. Each depends only on stages listed above it.

| stage | what it does | calls Claude |
|---|---|---|
| fetch | Downloads Unicode's emoji test data and CLDR's English annotations at pinned versions, and locks their checksums. | |
| inventory | Builds the emoji list. Folds skin-tone and gendered variants into their base emoji, links sequences to their parts, attaches keywords, and builds an alias table so any variant a model mentions resolves to an emoji in the set. | |
| render | Prepares the 3D and flat image of every emoji from the Noto submodule, plus contact sheets. | |
| pilot | Asks all 24 viewpoints about 40 emoji, including a fixed set of probe emoji, then writes a report with a cost projection for the full run and stops for approval. | yes, synchronously |
| panel | Submits one Message Batch per viewpoint for the remaining emoji, collects the answers and assembles them. Pilot answers are reused. | yes, batched |
| embed | Embeds every description, name and image. Work is cached in chunks, so an interrupted run resumes mid-way. | |
| graph | Mixes the similarity channels and builds the graph of neighbours, viewpoint links, part-of links and the spanning tree. | |
| race | Finds the cheapest routes between all pairs, plus a report of example races to the root. | |
| explain | Writes a one-line note for every step used by any race. A viewpoint's own reason is reused when it exists; Claude is asked only for steps without one. | yes, batched |
| layout | Lays the map out with UMAP. | |
| flowmap | Draws the drainage map into the root. | |
| viewpoints | Fits a sweep of layouts from the race map's similarity to the blended descriptions, starting from the race map itself, which the optimiser never moves. The sweep's blended end becomes the shared map, and sweeps from it follow, again with the shared map held still: one to each viewpoint alone, one to the pictures and one to the names and keywords. Several viewpoints are fitted at once, and an interrupted run resumes sweep by sweep. The sweeps are stored in a compact form (see [Compressed storage](#compressed-storage)). | |
| web | Prepares the site's data: a compact race tree for every possible finish line, the reason strings, the viewpoint answers per emoji, a sprite sheet, and every map sweep in the site's own coordinates, one small gzip file per sweep. | |
| export | Writes one combined file with everything, for anyone consuming the dataset, including each emoji's place on every viewpoint's map. | |
| release | Builds the Hugging Face release: Parquet tables of the emoji, viewpoints, descriptions, steps, graph edges, every race and every map, plus the dataset card, filled in with the table sizes and model names. | |

### Interruptions and failures

Any run can be stopped at any point, by Ctrl+C, a kill, a crash or a power cut. Running the same command again carries on where it left off.

- **Files** are replaced atomically, so a half-written output never replaces a good one.
- **Claude answers** are appended to journals one at a time. A line torn by a crash is dropped on the next start.
- **Batches** are recorded before and after submission. If a run dies between the two, the next run finds the batch on Anthropic's side and adopts it. If the batch never got there, it's resubmitted after a short settling time.
- **Failed requests** are retried up to three times, then marked dead rather than blocking the run.
- **Viewpoint maps** are fitted in worker processes and saved one viewpoint at a time. Ctrl+C stops the workers at once. If the main process is killed outright, workers may finish the viewpoint they're on (up to about 6½ minutes) before they exit; a re-run picks up the remaining viewpoints either way.
- **Two runs** can't touch the same data directory at once.

## Changing things

Settings live in `config.toml`, and the viewpoints, with their prompts, in `angles.toml`.

Each stage fingerprints its inputs: its own settings plus the fingerprints of the stages it depends on. Change a setting, and that stage and everything downstream reruns on the next `run`. Changes to a stage's code don't change its fingerprint; rerun that stage with `--force`.

### Free changes

- **The root emoji.** Notes already exist for every finish line, so nothing new needs explaining.
- **Map and drainage settings:** UMAP neighbours, spread and seed; how many basins get colours; how many landmark emoji are shown; the smallest river drawn.
- **The example races** listed in the race report.
- **Viewpoint map settings** (`[viewpoints]`): layouts per sweep and their easing, epochs, alignment strength, starting learning rate, how strongly layouts near the shared map are tied to it and how quickly that fades, and how many viewpoints are fitted at once. These cost CPU time rather than money: about 6½ minutes per viewpoint.

### Cheap changes

These cost a few cents and need an API key.

- **Graph and race settings:** the neighbour count, channel weights, cost exponent, viewpoint-vote discount and its cap, part-of discount and per-step cost.
  - They change the races, so some steps will be new.
  - The explain stage asks Claude only about those. For scale, all 5,744 Claude-written notes cost 15 cents.
- **Images or embedding models.** These also take a few minutes of GPU time, and change the races the same way.

### Paid changes

- **Adding a viewpoint.**
  1. The pilot asks it about the 40 pilot emoji and rewrites the report.
  2. The run stops for `approve`.
  3. The panel submits one batch for that viewpoint: 1,734 requests, about 23 cents.
  4. Everything downstream rebuilds, including notes for any new race steps.
- **Removing a viewpoint** is free. Its answers stay in the journals, unused.
- **Re-asking.** Answers are remembered per viewpoint and emoji, and notes per step, not per prompt or model. Rewording a viewpoint's prompt or switching models therefore reuses the old answers. To really ask again, give the viewpoint a new id, which makes it a new viewpoint, or start from an empty data directory.
- **Cost projection.** The pilot report projects the full panel's cost before you approve it. The original projection was $5.72, against $5.80 actually spent.

### Experimenting

- **A separate data directory.** Point `NUTMAP_DATA` at another directory to start a run from scratch there; nothing in the main data directory is touched. `NUTMAP_CONFIG` picks another configuration file.
- **A small subset.** The `[subset]` settings limit the run to an evenly spread sample of N emoji and to chosen viewpoints. The root and the probe emoji are always included. The test configuration (30 emoji, 3 viewpoints) is a ready-made example.
- **No API.** `NUTMAP_FAKE_LLM=1` answers every request locally with deterministic fake answers, including a fake batch service.
- **No GPU.** `NUTMAP_FAKE_EMBED=1` replaces the models with deterministic fake vectors.
- **Crash testing.** `NUTMAP_CHAOS=<probability>` makes runs crash at random points between writes. The chaos harness, a script in the scripts folder, combines this with random SIGKILLs, SIGINTs and torn journal lines on the small configuration, then checks that every rebuilt output matches a clean run byte for byte. `CHAOS_PASSES`, `CHAOS_ROUNDS` and `CHAOS_PROB` tune it.

## The site

The site is a static, framework-free TypeScript app built with Vite. It's served by an assets-only Cloudflare Worker named `nutmap`. Its data comes from the pipeline's web stage, so run the pipeline first.

Run these inside the site folder:

- **`bun install`** installs dependencies, once.
- **`bun run data`** refreshes the site's data from the pipeline.
- **`bun run dev`** serves the site locally with live reload.
- **`bun run build`** type-checks, builds, and assembles Cloudflare's prebuilt output.
- **`bun run deploy`** does all of the above and uploads the result to the Worker. You need to be signed in to Cloudflare with the `cf` CLI.

The project uses only Bun, no Node. Cloudflare's Vite plugin and `cf`'s own build can't load their TypeScript configuration under Bun, so the build writes Cloudflare's prebuilt output format itself, with Cloudflare's own libraries, and `cf` only uploads it. The Worker's settings (name, compatibility date, asset handling) live in the site's worker config.

Every view is a URL fragment: the finish line's emoji id (or `-` for no focus), optionally followed by a selected emoji's id and the chosen map, so links can be shared and back/forward works. Switching maps fetches the two sweeps involved (about 270 KB each), decompresses them with the browser's own gzip support, and slides every emoji along them through the blended map. Missing assets return a plain 404, because nothing is routed by path.

## Publishing on Hugging Face

The release stage writes a complete Hugging Face dataset repository into its own folder of the data directory: the dataset card and seven Parquet tables, about 34 MB. Each table is its own configuration, so the Hub's viewer and `load_dataset` show them separately. Nothing in it is uploaded by the pipeline.

The card's text, including the dataset's licence, is a template at the top of the project, with placeholders for the table sizes and model names. The Hub repository id it mentions is in the `[release]` settings. The release is rebuilt whenever the template or those settings change.

To publish, sign in once and upload the folder; the upload creates the dataset repository if it doesn't exist yet:

```bash
uv run hf auth login
```
```bash
uv run hf upload dpwiz/nutmap data/release/hub --repo-type dataset
```

## The videos

Two scripts in the scripts folder render 1080p H.264 videos, run with `uv run python`. Both need the site's data, for the sprite sheet, and ffmpeg. They read the data directory from `NUTMAP_DATA` like the pipeline, and write to an ignored folder under it. Both place sprites on a quarter-pixel grid by default (`--subpixel`), take `--plain` for a version without captions, and `--stills` to save chosen frames as images instead of a video.

### The morph

The morph script renders the text-to-image loop from the pipeline's stored sweep between the shared blended map and the pictures, so it needs no fitting of its own. Each stored layout is matched to the previous one by rotation, shift and uniform scale only, so the camera doesn't spin; each emoji's path is lightly smoothed; and the sweep plays forward and back so it loops seamlessly. Rendering takes seconds.

Options:
- **`--seconds`, `--fps` and `--easing`** retime the loop by following smooth curves through the stored layouts.
- **`--subpixel`** sets how finely sprites are placed. Slow motion glides instead of stepping a whole pixel at a time; at a quarter pixel a 64-second loop is as smooth as a 16-second one at whole pixels.
- **`--plain`** also drops the slider and the highlighted emoji.
- **`--sigma`** changes the smoothing; the shared map itself is never moved.

### The viewpoint tour

The tour script plays the pipeline's viewpoint maps: from the shared blended-text map out to each viewpoint and back, in the order of the viewpoint list. Every leg starts and ends on the same map, so the loop is seamless. It needs no fitting of its own and renders in a few minutes.

- **`--hold`** is how long each map stays perfectly still, 1 second by default. Holding the exact key layout avoids the small shuffle that interpolated frames near a key frame would show.
- **`--move`** and **`--steepness`** set how long each sweep takes and how gently it leaves and arrives: 3 seconds on a logistic curve of steepness 12 by default.
- **`--only`** visits just the listed ids, in the given order; besides viewpoint ids it takes `pictures`, `names` and `race`.
- **`--sigma`** lightly smooths each emoji's path; the shared map itself is never moved.

## Keeping the repository small

Only the data listed under [Rebuilding](#rebuilding) is committed, in its compressed form. Everything derived is ignored by git and comes back from `run`.

Before committing changed model output (new journals, batch records or notes), make sure it's packed (`status` says if not), and check that a rebuild from the tracked files alone still reproduces everything else. One way:
1. Copy the tracked files into an empty directory.
2. Run the pipeline there with `NUTMAP_DATA` pointing at it and no API key.
3. Compare the results with your working copy.
