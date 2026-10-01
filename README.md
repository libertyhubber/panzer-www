# Webseite für derrosarotepanzer.com

Statische Webseite für [derrosarotepanzer.com](https://derrosarotepanzer.com)

## Build

Install [uv](https://docs.astral.sh/uv/) and run `make html`.
Python scripts use `uv run --script` with inline dependency metadata; no manual
`.venv` setup is needed.

## Local development server

Run `make serve` and open <http://localhost:8082>. Uvicorn and Starlette are
installed automatically through `uvx`; no separate server installation is needed.
The server handles concurrent static-file requests and negotiates gzip compression
for responses of at least 1 KiB. Restart any existing server to apply this change.
For realistic loading tests, enable browser network throttling and disable caching.

## Local gallery thumbnails

```bash
# Convert existing local JPEG sheets to WebP quality 55 and remove the JPEGs.
# This does not download archive originals or change the monthly indexes.
uv run --script scripts/generate_thumbnails.py --convert-existing

# Initial generation if no local sheets exist: download archive originals.
make thumbnails

# Generate one month, or rebuild even unchanged sheets.
uv run --script scripts/generate_thumbnails.py --month 2026/09
uv run --script scripts/generate_thumbnails.py --force

# Ingest from an archive checkout instead of downloading originals.
uv run --script scripts/ingest_uploads.py ../panzer-archiv-02
```

Sprites and matching monthly indexes live in **`images/YYYY/MM/`** in this website
repository, alongside the global metadata in `images/`.
Each `thumbnails-00.webp`, `thumbnails-01.webp`, etc. contains at most **20 images**
in a **5-column grid**, with **220 × 220 px** tiles and **2 px** gutters. Images
are fitted without cropping/upscaling, centered on black, and saved as **lossy WebP
at quality 55**, with encoder method 6. The final sheet uses only the rows it needs.
The existing JPEG sheets were converted locally; future generated sheets use
archive originals. WebP has no progressive JPEG-style refinement; progressive
quality enhancement comes from subsequently loading the original images.

Sheet names and offsets are derived from the entry's position in the local
`entry_index.json`: entries 0–19 use sheet 00, 20–39 use sheet 01, etc. No sheet
filename or coordinates need to be stored per entry. The index preserves archive
order and contains only image names and original dimensions. The gallery displays
220 px tiles and loads **local sprites/indexes** as quick previews for unfiltered
browsing. After each visible sheet has loaded, decoded and had a chance to paint,
archive originals are loaded lazily for that bounded window, with low request
priority and at most **four concurrent upgrades**. Each original replaces its
preview only once loaded and decoded, fitted without cropping. Completed upgrades
survive metadata refreshes and scrolling; queued offscreen upgrades are discarded.
A failed original leaves its preview intact.

When any filter is active, tiles use archive originals directly instead of sparsely
used sprite sheets. Browsers that cannot decode lossy WebP also use originals
directly. Missing/corrupt sheets fall back to originals. The lightbox always opens
archive originals. Only viewport rows plus one extra row on each side are rendered
in either mode. Filtering loads all indexes, but images only for this bounded window. Monthly
indexes use six independent request slots: each completed or failed request
immediately frees its slot for the next month, without waiting for a neighbor.

The regeneration command reads `images/dir_index.json`, downloads each monthly
archive index, and downloads originals only for changed/missing sheets. Originals
are processed in memory and never retained in the website repository. Downloads
are bounded to a batch of 20 with `--workers` concurrent workers (default 6).
`--timeout` and `--retries` control network failures. Completed months are reusable
when rerunning after an interruption; unchanged sheets are skipped unless `--force`
is supplied. Publish the complete `images/` directory and matching directory index
with the new frontend, rather than deploying before regeneration finishes.

Normal Telegram sync generates these same local assets from the archive checkout
and includes `images/` in the website commit; it no longer generates archive sprites.
For an alternate website checkout use `ingest_uploads.py ARCHIVE --www-repo PATH`.

## Telegram links and engagement backfill

The normal sync only revisits the latest 50 message IDs. The existing cache starts
in July 2024, so older archive filenames need matching against Telegram photos;
message IDs cannot be inferred from their legacy filenames.

```bash
export PANZER_IMGSYNC_API_ID='your-api-id'
export PANZER_IMGSYNC_API_HASH='your-api-hash'
# Optional; defaults to @RosaroterPanzerBackup.
export PANZER_IMGSYNC_CHANNEL='@RosaroterPanzerBackup'

# Refresh views, comment counts, reactions and forwards for all cached posts.
# No image downloads. First run prompts for a Telegram user login.
uv run --script scripts/backfill_telegram.py --stats-only

# Pilot a full-history scan, then resume from its saved cursor.
uv run --script scripts/backfill_telegram.py --limit 100
uv run --script scripts/backfill_telegram.py

# Prefer local archive originals to HTTP downloads (repeat for other archives).
uv run --script scripts/backfill_telegram.py --archive-root ../panzer-archiv-00
```

The backfill updates `scripts/telegram_messages_cache.json` and exports
`images/telegram_metadata.json`; it does **not** rename/ingest images, commit or
push. Review and publish both JSON files yourself. Do not run it concurrently
with normal sync or another backfill. It uses the same `panzerimgsync.session`
as normal sync; keep that session and credentials private.

Full-history mode scans oldest-first, refreshes totals for existing mappings,
and downloads photos only for unmapped messages. Candidate archive originals
come from monthly indexes, within **±3 calendar days** of the Telegram post.
Originals are held in a bounded in-memory cache, not saved locally. Exact file
bytes or decoded pixels must match uniquely by default. If recompression/resizing
prevents exact matches, `--restart --allow-visual-matches` opts into conservative
128-pixel RGB comparisons with aspect-ratio checks; these are still heuristic
and resulting links should be reviewed. Ambiguous/unmatched photos retain no
link; `match_status` records the outcome in the message cache. Deleted posts,
photos absent from this channel, changed/cropped images or dates outside the
window cannot reliably be recovered. `--date-window N` changes the window.

Checkpoints are written every 50 history messages (100 posts for stats-only)
and on history interruption/error. The local, git-ignored
`scripts/telegram_backfill_state.json` stores separate cursors for history and
stats. Rerun the same command to resume; `--restart` restarts the selected mode
and retries unmatched photos. A completed stats-only scan resets its cursor so
its next run refreshes all cached posts again. `--limit N` bounds either scan.
Telegram rate limits can make a full-history scan take a long time.

Counts are **current cumulative totals**, not historical snapshots. Reactions
are summed across emoji; comments are Telegram's reply count, not downloaded
comment text. Unavailable view/comment counts remain null rather than guessed
zero. Missing/deleted messages during stats-only retain their last cached totals.
A filename linked to several posts displays the oldest mapped post's counts.

## Image OCR and classification

`scripts/classify_images.py` uses the OpenAI Responses API to transcribe visible
text in its original language and identify an established meme template, if any.
It needs only Python's standard library and `OPENAI_API_KEY`.
The `@RosarotePanzer` watermark is excluded from relevant content, including when
OCR splits it into `@Rosarote` and `Panzer` on separate lines.

```bash
export OPENAI_API_KEY='your-key'

# One image: print a JSON result to stdout.
uv run --script scripts/classify_images.py 'https://example.com/meme.jpg'

# Multiple images: concurrent requests, one JSON record per stdout line.
./scripts/classify_images.py 'https://example.com/one.jpg' 'https://example.com/two.jpg' \
    --concurrency 2 > results.jsonl

# Override the model and reasoning effort.
./scripts/classify_images.py 'https://example.com/meme.jpg' --model YOUR_MODEL_ID --reasoning-effort high

# Preview archive URLs without an API key or classification charges.
uv run --script scripts/classify_images.py --archive --month 2024/07 --limit 5 --dry-run

# Start with a small sample, then resume the whole archive.
uv run --script scripts/classify_images.py --archive --month 2024/07 --limit 5
uv run --script scripts/classify_images.py --archive --concurrency 4
```

`--concurrency N` limits simultaneous download/classification jobs (default: 4).
Use `--concurrency 1` for sequential processing or lower the value if you hit API
rate limits. Work submission is bounded rather than queueing the entire archive;
`--limit` caps attempts even when concurrency is higher than the limit.

A single explicit URL still produces one pretty-printed JSON object. Multiple
explicit URLs produce JSONL in completion order (not input order), with each
record identifying its source URL. Repeated URL arguments intentionally perform
repeated classifications, so they can be used to test concurrency. Per-image
failures go to stderr, do not create success records, and make the exit status
nonzero; other images continue. Fatal model/key errors stop new submissions,
cancel unstarted jobs, and drain running jobs so successful results can be saved.
Archive JSONL is written and flushed only by the coordinating main thread.

Single-URL runs keep the result JSON on stdout and always print debug diagnostics
on stderr: requested/returned model and reasoning effort,
image download/API/total wall-clock timing, image size, response ID, service tier,
and API token usage (including cached, cache-write and reasoning tokens).
Multiple-URL runs print the same per-image diagnostics with URL-tagged, atomic
stderr lines, plus total batch timing. `--pricing` applies to all explicit URLs.

Cost is an **estimate in USD** for the returned response, not a billing lookup;
it excludes charges from earlier retries, account discounts and tax. Built-in
`gpt-6-luna` standard-tier rates are $0.10 input, $0.01 cached input, $0.125 cache
writes and $0.50 output per million tokens, verified on 2026-09-30 from
[OpenAI's model pricing](https://developers.openai.com/api/docs/models/gpt-6-luna).
Above 272K input tokens, input/cache rates double and output is multiplied by 1.5.
Reasoning tokens are already included in output usage and aren't charged twice.
Unknown models/tiers report cost as unavailable rather than guessing. Override
with `--pricing INPUT,CACHED,CACHE_WRITE,OUTPUT` using effective USD/1M token rates
for your model/tier (custom rates are used as-is, with no long-context adjustment).
For example: `--pricing 0.10,0.01,0.125,0.50`. Redirect stderr to a separate file
with `2>debug.log` if needed; API keys and image base64 data are never logged.

The defaults are model `gpt-6-luna` and `--reasoning-effort low`, verified with a live
image classification request. Use `--model` with an identifier available to your
account if OpenAI rejects it. The model must support image input and structured
JSON output. Authentication/model configuration errors stop the run.

`--reasoning-effort` sets OpenAI's `reasoning.effort` directly. Choices are `none`,
`low`, `medium`, `high`, and `xhigh`; support varies by model. Use
`--no-reasoning` to omit the reasoning parameter entirely and use the model's
default (for example, with a model that doesn't support configurable reasoning).
This differs from `--reasoning-effort none`, which explicitly requests no reasoning.
The former `--thinking` option and `light` alias have been removed.

Archive mode reads `images/dir_index.json`, fetches monthly `entry_index.json`
files from the gallery's **`archivX.derrosarotepanzer.com`** hosts, and downloads
full-resolution images (not thumbnail sheets). `--month YYYY/MM` is repeatable;
`--dir-index PATH` selects another directory index. Supported inputs are JPEG,
PNG, WebP and non-animated GIF, up to 20 MiB each.

Results include OCR `text`, `languages`, `tags`, `image_type`, a concise factual German
`description` suitable for the image's `alt` attribute, and a `meme_template` with `status` (`recognized`, `unknown`, or `none`), nullable `name`,
confidence and visual evidence. OCR and template matches are model predictions
and can be wrong; `unknown` avoids forcing a guessed template name.
`tags` is a list of concise German search keywords or short phrases for supported
subjects, characters, objects, settings, formats and themes. Proper names are
preserved; whitespace, duplicates and the watermark are removed. For example:
`["Ernie", "Bert", "Sesamstraße", "konzertsaal", "meme"]`.

Archive results are appended to `images/classifications.jsonl` (override with
`--output PATH`). Each record includes its source URL, model, `reasoning_effort`,
schema version, timestamp and API token usage. Completed records are flushed
immediately and skipped on subsequent runs using the same model, reasoning effort
and schema; `--force` appends new classifications instead. Changing the model or
reasoning effort reclassifies images. Resume still reads legacy `thinking` fields
(`light` maps to `low`; `default` or a missing field maps to an omitted parameter),
so this terminology change alone does not trigger reclassification. New records
only use `reasoning_effort` (null when omitted). Results from before the
watermark-exclusion, German-alt-text or tags changes (schema versions 1, 2 and 3)
are reclassified on the next archive run, incurring new API requests. Avoid
concurrent runs writing the same output file. If interrupted during a write,
remove/repair the partial final JSONL line before resuming.

The full archive makes **one paid API request per pending image**, plus possible
retries. `--limit` bounds the number of images attempted, not the number of API
requests including retries. Image-specific failures are reported on stderr and
retried on the next run; a run with any failures exits nonzero. Use `--timeout`
and `--retries` to adjust network behavior. Downloaded image bytes are sent to
OpenAI, with response storage disabled (`store: false`); images are not saved
locally by this script.

### Classification metadata in the gallery

```bash
# Refresh the compact UI index after classifying more images.
make classification-index
# Also included in the normal HTML build.
make html
```

`scripts/export_classifications.py` reads `images/classifications.jsonl` and writes
two indexes, keyed by archive-relative image paths: `images/classification_index.json`
contains only tags and template names; `images/classification_text_index.json` contains
OCR text and descriptions. The last appended result for each image wins. Neither
index includes API diagnostics or classification history.
Generic tags `memes`, `meme`, `ausdruck`, `gesichtsausdruck` and `text-meme` are
excluded case-insensitively during export. Tags `zitatgrafik` and `zitat-meme`
are converted to `zitat` case-insensitively, with duplicates removed; raw results
remain unchanged. Exported template names are trimmed and case-folded so casing
variants share one filter value. Unknown templates do not display a template label.
Deploy both generated indexes alongside the website; the raw JSONL is not needed by
the browser. Without a local JSONL, existing split indexes are preserved; an old
combined index is automatically split on export.

The gallery renders first, then loads tags/templates and Telegram metadata in the
background. After both have finished loading and rendering (or failed), the larger
text/description index is requested last. Tag and template filtering work before
it arrives; OCR/description search and thumbnail descriptions become available
when it finishes. The search placeholder/status indicate loading or unavailable
full-text data, without preventing tag search or image browsing.
Tags and meme templates appear below the Telegram metadata; long tag lists can be
scrolled with a mouse or keyboard. Tag frequencies are counted once after loading,
case-insensitively and once per classified image across the entire catalog.
Thumbnail tags are ordered most-common-first (alphabetically on ties), and tags
used in only one image are hidden from thumbnails but remain searchable. Descriptions provide thumbnail tooltips and
accessible labels. Images without results are marked “Not classified”; missing
metadata does not prevent browsing production-hosted images. Search matches OCR,
descriptions, template names and tags case-insensitively; tag suggestions are not
implemented yet.

Offline tests:

```bash
uv run --no-project --python 3.12 --with 'pillow>=11.1.0' python -m unittest discover -s tests -v
node --test tests/test_gallery_classifications.js
```
