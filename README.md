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
OPENAI_API_KEY='your-key' uv run --script scripts/ingest_uploads.py ../panzer-archiv-02
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

Normal Telegram sync (`make sync_and_ingest`) generates these same local assets
from the archive checkout and includes `images/` in the website commit; it no longer
generates archive sprites. All pending months are ingested in one run. The matching
sibling `panzer-archiv-*` checkouts must already exist. Git failures stop the run;
archive staging originals are retained until archive publishing succeeds. Rerun
after fixing the failure; unchanged repositories still retry their push. Commits
include only generated paths, leaving unrelated staged changes out of the commit.
Ingest also classifies pending originals using OpenAI before either repository is
pushed, then exports both gallery classification indexes. Originals are read from
the local archive, so newly ingested images do not need to be published first.
Results are appended to the website's `images/classifications.jsonl`; images already
classified with the current model, reasoning effort and schema are skipped. This
covers all missing/outdated classifications in the archive being ingested, not just
newly copied files, and incurs API charges for pending images. `OPENAI_API_KEY` is
required when work is pending. A classification failure aborts publishing and
retains successful records for the next attempt; normal sync also retains staging
originals. Standalone ingest performs this same classification/export step but does
not run Git itself.
For an alternate website checkout use `ingest_uploads.py ARCHIVE --www-repo PATH`.

To sync/ingest without running Git, use `make sync_and_ingest SYNC_ARGS=--no-git`
(or `uv run --script scripts/panzer_imgsync.py --no-git`). This still downloads
photos, updates archive indexes and local assets, classifies pending images
(incurring API charges), and removes ingested staging originals. Without new
photos, sync still ingests the latest populated archive to retry pending
classifications and publishing.
Telegram scripts require Telethon 1.44 or newer to read version-8 session databases;
older cached clients fail with `ValueError: too many values to unpack (expected 5)`.

## Import Telegram photos missing from the archive

The archive → Telegram matching/review pass is complete as far as it can go;
remaining unknown archive associations do not block the reverse backfill.
`scripts/import_telegram.py` scans **the full channel oldest-first**, including
photos posted before archiving began. It is not restricted to the archive-gap
search windows or normal sync's recent-message lookback.

```bash
# Uses the same PANZER_IMGSYNC_API_ID/API_HASH/CHANNEL credentials as sync.
# Preview the first 100 history messages: no photo downloads or application writes.
uv run --script scripts/import_telegram.py --dry-run --limit 100

# Pilot: import missing originals locally, without classification charges or git.
uv run --script scripts/import_telegram.py --limit 100

# Resume through the entire channel.
uv run --script scripts/import_telegram.py

# Explicitly opt into website sprites + classification (requires OPENAI_API_KEY).
uv run --script scripts/import_telegram.py --ingest

# Explicitly opt into classification AND archive/website commits and pushes.
uv run --script scripts/import_telegram.py --ingest --publish
```

Existing cached associations are preserved without downloading their photos.
Unmapped photo attachments are compared against **all nearby originals**, including
already-linked ones, using the existing conservative visual matcher and byte-pinned
approvals. Reposts reuse an existing filename; genuinely missing photos are saved
as `date_messageID_fingerprint.jpg` under the appropriate sibling archive checkout.
The fingerprint is only a filename component, never proof of duplicate identity.
Ambiguous matches stay unmapped for review rather than creating duplicate originals.
Webpage previews, documents and videos are excluded; downloads select the largest
still photo and preserve its bytes without recompression. Missing archive checkouts
or unsupported years fail rather than silently skipping images.

By default, imports update archive monthly/directory indexes,
`scripts/telegram_messages_cache.json` and `images/telegram_metadata.json` **locally**.
Website monthly indexes/sprites/classifications are not regenerated until `--ingest`;
the gallery will not show new entries before that step, and original URLs will not
be public until the archive is published. Ingest processes the affected archives'
pending classifications through the normal pipeline and may incur API charges.
Neither mode runs git unless `--publish` is explicitly supplied; that option uses
normal sync's generated-file publication and can include other pending changes
under `images/`. Review the working trees before publishing.

The separate, git-ignored `scripts/telegram_import_state.json` stores the last
completed Telegram ID and archives awaiting ingest. Imports checkpoint every 50
history messages and on exit; rerun to resume after transfer/classification failures
or interruption. `--limit` counts **all scanned messages**, not only new photos.
`--restart` rescans from the oldest post without clearing reviewed mappings, and
can revisit ambiguous posts once their associations have been resolved. A dry run
uses the current cursor but does not advance it or determine how many unmapped
photos will prove to be reposts. Normal Telethon session activity still occurs.
The archive-gap backfill state is not changed. `--photo-cache-dir`,
`--no-photo-cache` and `--download-timeout` work as in the linking backfill.

**Do not run import concurrently with sync, link backfill or review:** they share
the Telegram session/cache and archive files. No exhaustive import has been run
as part of implementing this command.

## Telegram links and engagement backfill

The normal sync only revisits the latest 50 message IDs. Backfill first compares
archive indexes with cached Telegram associations to find **images still lacking
a message**, then searches Telegram only around those images' dates. Message IDs
cannot be inferred from legacy filenames.

```bash
export PANZER_IMGSYNC_API_ID='your-api-id'
export PANZER_IMGSYNC_API_HASH='your-api-hash'
# Optional; defaults to @RosaroterPanzerBackup.
export PANZER_IMGSYNC_CHANNEL='@RosaroterPanzerBackup'

# Refresh counts only for mapped posts from the last 30 calendar days.
# No image downloads. First run prompts for a Telegram user login.
uv run --script scripts/backfill_telegram.py --stats-only
uv run --script scripts/backfill_telegram.py --stats-only --recent-days 7

# Fill missing archive associations; pilot with at most 100 history messages.
uv run --script scripts/backfill_telegram.py --allow-visual-matches --limit 100
uv run --script scripts/backfill_telegram.py --allow-visual-matches

# Retry remaining gaps after tweaking thresholds; reuse downloaded photo bytes.
uv run --script scripts/backfill_telegram.py --restart --allow-visual-matches

# Optional: choose a photo cache directory, or bypass it for fresh downloads.
uv run --script scripts/backfill_telegram.py --photo-cache-dir /tmp/panzer-photos
uv run --script scripts/backfill_telegram.py --no-photo-cache

# Diagnose specific downloads without changing the backfill cursor or metadata.
# Telethon INFO logs are automatically enabled for probes.
uv run --script scripts/backfill_telegram.py --probe-message 14611 14609

# Enable Telethon flood-wait/connection/retry logs during a normal scan.
uv run --script scripts/backfill_telegram.py --telegram-log
```

The backfill updates `scripts/telegram_messages_cache.json` and exports
`images/telegram_metadata.json`; it does **not** rename/ingest images, commit or
push. Review and publish both JSON files yourself. Do not run it concurrently
with normal sync or another backfill. It uses the same `panzerimgsync.session`
as normal sync; keep that session and credentials private.

Default mode is **archive-gap driven**, not an exhaustive history/count refresh.
It finds unlinked image filenames, merges their ±`--date-window` day ranges and
searches those periods **newest to oldest**, jumping over covered periods using
Telegram date offsets. Within a period it downloads only unmapped photos whose
dates could match a still-unlinked image. Already mapped posts are skipped without
changing their engagement counts; newly established links receive initial counts.
The scan stops when all gaps are filled or their date windows have been exhausted.
Remaining unmatched images are reported rather than endlessly rescanned.
Matching still compares against *all* nearby archive originals, including linked
ones, so excluding an already-linked duplicate cannot create a false unique match.

Downloaded Telegram still-image bytes are cached on disk by default in
`panzer-telegram-photos/` under Python's system temporary directory (usually
`/tmp/panzer-telegram-photos`; respects `TMPDIR`). The path is printed at startup.
The cache survives reruns and `--restart`, including unmatched/ambiguous photos,
so threshold changes can retry matching without downloading those photos again.
Only photo bytes are cached, not features or match results: each retry uses the
current matching settings and archive candidates. Cache keys identify the Telegram
photo, DC and selected still-image variant, not just the message ID; edited photos
or changed variants are downloaded again. Expiring Telegram file references do not
invalidate cached bytes. Complete, decodable downloads are saved atomically;
empty/corrupt entries are downloaded again and cache I/O failures do not block backfill.
Use `--photo-cache-dir PATH` to choose another directory or `--no-photo-cache` to
bypass both cache reads and writes. Remove the cache directory whenever you want
to reclaim disk space; there is no automatic size/age eviction, and the OS may
clear temporary files at any time. No downloaded photos are stored in the checkout
by default. Stats-only runs and diagnostic probes do not use this disk cache.

**Only photo attachments are matched** (`MessageMediaPhoto`): video/document posts
and link previews are skipped, even when Telethon exposes a preview thumbnail via
`msg.photo`. Only still images are downloaded for matching, and an explicit
still-image size is selected when a Telegram photo also has video variants. Still-image selection prioritizes pixel
area, with byte count only breaking equal-resolution ties: a smaller recompressed
thumbnail can have more bytes than the original. Telethon's default media selection
is not used because it can prefer a video variant. The photo object itself is downloaded,
not the whole message, so Telethon cannot select a different media object instead.
Already mapped photos and non-photo media with preview thumbnails are skipped
silently and counted in the final summary. Other messages print their date,
Telegram ID and photo outcome: not yet mapped (loading/matching), new exact match,
visual match (if enabled), unmatched, ambiguous, or no photo.
Checkpoints and the final summary also show progress and the latest processed date.
Telegram photo downloads and archive matching are reported separately, with elapsed
time and a "still working" message every 10 seconds during slow operations. Before
downloading, the script prints the still-image sizes, selected variant and Telegram
DC; transfer progress includes bytes received and percentages. `--telegram-log`
enables Telethon INFO logs (on stderr) for flood waits, connections and retries. Telegram
photo cache hits are reported explicitly and skip the transfer. Uncached photo
downloads time out after 120 seconds by default; `--download-timeout N` changes
that limit. A failed/timed-out download leaves the cursor before that message so the
next run retries it rather than skipping it. Archive matching has no overall time
limit: it may need to download many candidates, each with its own HTTP timeout/retries.

`--probe-message ID [ID ...]` fetches and downloads each specified post's still image,
even if already mapped, but does not read/write backfill state, message caches,
gallery metadata or archive files. Diagnostic probes can also download webpage
preview photos, but those previews are never candidates for archive matching.
Photo bytes are not saved. Probes automatically
enable Telethon INFO logging and use the same download timeout; after an error,
remaining IDs are still tested and the command exits nonzero. Do not combine probes
with `--stats-only`, `--restart` or `--limit`, or run them alongside normal sync/backfill.

### Manual top-five visual review

When automatic matching leaves gaps, use `scripts/review_telegram_matches.py`
to rank **the five closest unmatched Telegram photo posts for each archive image**,
without requiring the strict automatic-match thresholds to pass:

```bash
# Paste an archive image URL, including image URLs copied from localhost.
uv run --script scripts/review_telegram_matches.py \
  'http://localhost:8082/images/2021/12/2021-12-07_806ABF24.jpg'
uv run --script scripts/review_telegram_matches.py \
  'https://archiv0.derrosarotepanzer.com/images/2021/12/2021-12-07_806ABF24.jpg'

# With no image arguments, review archive images still lacking an association.
# First three unmatched archive images, newest first; NOT a Telegram-post limit.
uv run --script scripts/review_telegram_matches.py --limit 3

# Review all remaining unmatched archive images.
uv run --script scripts/review_telegram_matches.py

# Default scope is ±90 calendar days around EACH archive filename date.
# Widen it, or explicitly search the entire channel regardless of dates.
uv run --script scripts/review_telegram_matches.py --limit 3 --date-window 180
uv run --script scripts/review_telegram_matches.py \
  2021-12-07_806ABF24.jpg --all-history
```

The CLI prints each archive image URL followed by up to five ranked Telegram
URLs, with dates and distances. Click the archive and Telegram links in your
terminal to compare the full images and captions in the browser. **No HTML or JSON reports are generated.**
Progress and completion/partial-scan warnings go to stderr; results go to stdout.
Default batch mode selects only archive images without cached associations, in
deterministic newest-first filename order. `--limit N` is applied **before** loading
archive originals; `--limit 3` loads and ranks only the first three pending images.
`--unmatched` remains an optional explicit alias for this default mode.
Archive filenames and multiple image URLs are also accepted. Localhost,
`127.0.0.1` and `[::1]` URLs are resolved to the indexed archive original by their
`/images/YYYY/MM/filename.jpg` path; the local server need not be running.

**Review only:** this script never changes `telegram_messages_cache.json`, gallery
metadata, archive originals or the backfill cursor. It only saves downloaded photo
bytes to the same temporary cache used by backfill. **Already mapped Telegram
posts are excluded**, before reading cached photos, decoding, downloading or
scoring. A post is mapped when its message-cache record has an archive `name`;
unmatched and ambiguous records without a name remain candidates. This exclusion
is review-only: automatic backfill still checks linked originals for ambiguity.
Repeated/identical photos in distinct unmatched posts retain separate links.
After manual verification,
record the confirmed archive/post pairs separately; suggestions are not applied
automatically.

Ranking uses 128px YCbCr pixel differences, weighted toward luminance, plus an
aspect-ratio penalty. Lower distance means closer pixels, **not match confidence**.
No automatic RMS, aspect-ratio or outlier rejection gates are applied. It is not
crop-, border- or layout-invariant, and similar templates can have different text.
The top five are closest **within the selected search scope**, not necessarily in
the entire channel; use `--all-history` if date offsets are unknown. If fewer than
five unmatched photo posts exist in scope, all available ones are shown.

The scan merges overlapping date windows, visits each candidate post once, and
reuses each photo's features for all nearby archive images. Reruns retrieve fresh
message/media metadata but reuse cached full-size still-image bytes; no persistent
feature index or review cursor is used. Telegram history metadata is still fetched,
but progress reports how many mapped posts were skipped without photo work.
Non-photo attachments and webpage previews are excluded. `--photo-cache-dir` and `--download-timeout` work as in backfill.
A full-channel or large unmatched batch can require many downloads and trigger
Telegram rate limits; start with `--limit`. Progress is printed every 100 posts,
and uncached transfers show backfill's download progress. Failed photo downloads
are printed to stderr and cause a nonzero exit. Interrupted/failed scans still
print the suggestions collected so far, with an explicit **incomplete** warning;
rerun to retry with cached bytes. Do not run review concurrently with sync/backfill: it uses
the same private `panzerimgsync.session` and credentials.

### Automatic matching details

Candidate archive originals come from monthly indexes, within **±7 calendar days**
of the Telegram post by default. Archive filename dates can lag the actual Telegram
post by several days, so a full week covers cases that the former ±3-day window
excluded. `--date-window N` selects a narrower or wider search.
Sibling `panzer-archiv-*` checkouts are used automatically, using the year-to-repo
mapping in `scripts/panzer_imgsync.py` (`IMG_REPOS`). Archive indexes are preferred
over website copies. Missing originals are downloaded from the matching
`https://archivN.derrosarotepanzer.com/images/YYYY/MM/` URL; missing indexes are
also downloaded if neither local copy exists. No archive-directory arguments are
needed. Original-image features are held in a bounded in-memory cache, not saved locally.
Compact byte/pixel identities and **4×4 luminance fingerprints** are indexed lazily
as new date windows are visited and retained for the rest of the invocation. This
avoids repeatedly decoding every candidate when a wide window exceeds the 512-image
feature cache. Visual lookup probes neighboring fingerprint buckets and uses
conservative RMS bounds to shortlist candidates, then runs the unchanged strict
128-pixel comparison; a coarse fingerprint never establishes a link. Exact-match
precedence, date-window restrictions and duplicate/ambiguity checks still apply to
all nearby originals, including already-linked images. The index is in-memory only:
first visits still need to decode/download candidates, and reruns rebuild it.
Exact file bytes or decoded pixels must match uniquely by default. If recompression/resizing
prevents exact matches, `--restart --allow-visual-matches` opts into conservative
128-pixel comparisons with aspect-ratio checks. The normal RGB path requires maximum
channel RMS ≤ **3.0** and at most **0.2%** of RGB channel samples differing by more
than 12. A JPEG/color-edge fallback uses YCbCr: luminance RMS ≤ **3.0**, at most
**0.225%** of luminance samples differing by more than 8, and raw chroma RMS ≤ **6.5**.
Absolute signed mean shifts must stay ≤ **2.0** for luminance and ≤ **1.875** for
chroma, allowing small JPEG rounding bias while bounding broad brightness/hue changes.
The ordinary chroma path requires RMS ≤ **3.25** and at most **1%** outliers (>12)
per channel. Stronger chroma errors require an additional shared-color-edge check:
raw outliers ≤ **8%**, chroma RMS ≤ **4.5** and outliers ≤ **4%** after a **1px
Gaussian blur**, and at most **0.8%** raw outliers outside color structure present
in **both** originals. If blurred RMS exceeds **3.25** or blurred outliers exceed
**1%**, at least **1%** of all pixels must be raw outliers on shared color edges.
Structure means a channel differs from its own blurred version by more than two
levels. This extra evidence permits dense JPEG artifacts around colored text,
without admitting broad balanced color changes that lack shared edge outliers. **Luminance is never blurred**, so the
text/content outlier gate remains in force. Fallback matches include `Y` and
`chroma` scores in the output. These remain heuristics and resulting
links should be reviewed; multiple passing candidates remain ambiguous. Ambiguous/unmatched photos retain no
link; `match_status` records the outcome in the message cache. Deleted posts,
photos absent from this channel, changed/cropped images or dates outside the
window cannot reliably be recovered. `--date-window N` changes the window.

Some manually verified pairs are rendering variants rather than JPEG copies:
text placement, spacing, colors or rasterization can differ while the owner has
confirmed the archive/post association. Broadly increasing pixel tolerances to
accept these would also accept altered captions. Such exceptions are recorded
explicitly in **`scripts/telegram_verified_matches.json`**, not inferred by the
visual comparator. With `--allow-visual-matches`, backfill can apply an approval
only for its configured channel, exact Telegram message ID and post date, and
when **both the downloaded photo and archive original have the approved SHA-256
hashes**. The original must also be in the selected date window. Expired or
mismatched approvals do not apply; normal matching may still run. Byte/pixel
identity precedence and exact-match ambiguity checks remain first; a human
approval explicitly chooses the named archive image rather than requiring a
unique approximate match. Output labels these as
`visual (manually verified; byte-pinned)` so they are distinguishable from
heuristic matches. Updating the approval registry changes the plan signature,
automatically retrying remaining gaps on the next invocation. The registry
contains no Telegram access hashes, file references or credentials and should
be published with the script.

Every newly filled gap is exported immediately, with additional checkpoints every
50 history messages (100 posts for stats-only) and on history interruption/error.
Reload the local gallery to see saved metadata; no rebuild/server restart is needed.
The git-ignored `scripts/telegram_backfill_state.json` stores the gap search plan,
current date-window index and exclusive message cursor, separately from stats.
Rerun the same command to resume **before the last processed message in that window**.
Old cursor-only state is automatically replaced by a gap plan; mappings are retained.
New archive images, newly removed associations, changed matching settings or changed
visual thresholds or photo-selection algorithm versions automatically rebuild the
plan so earlier unresolved gaps aren't
skipped by an old cursor. `--restart` explicitly retries remaining gaps, never
already-linked images. Exhausted plans make no more history requests unless the
plan changes or is restarted. `--limit N` bounds history messages across all windows.
Telegram rate limits can still make searches take a long time.

Engagement refresh is separate: `--stats-only` fetches **mapped posts from the last
30 UTC calendar days** by default; `--recent-days N` changes that window and 0
disables refresh. Actual cached post dates are preferred; legacy records fall back
to the archive filename date, with the actual Telegram date checked before updating
counts. Undated/unmapped posts and older posts are not refreshed. The recent stats
scan has its own resumable cursor; changing its day window resets that cursor, and
completion resets it so the next run refreshes recent posts again. `--limit N`
bounds the number of selected recent posts.

Counts are **current cumulative totals**, not historical snapshots. Reactions
are summed across emoji; comments are Telegram's reply count, not downloaded
comment text. Unavailable view/comment counts remain null rather than guessed
zero. Missing/deleted messages during stats-only retain their last cached totals.
A filename linked to several posts displays the oldest mapped post's counts.

## Image OCR and classification

`scripts/classify_images.py` uses the OpenAI Responses API to transcribe visible
text in its original language and identify an established meme template, if any.
Each image uses up to three separate structured-output calls: `gpt-6-luna` handles
OCR, languages, German description, image type and meme template; `gpt-6.1-sol`
handles only tags with medium reasoning effort; a further `gpt-6-luna` call with
low reasoning effort translates the tags from German to English and vice versa.
The first two calls receive the original image, downloaded/read only once. The
translation call receives only the normalized tag list as text, not the image,
and is skipped if the list is empty. It needs only Python's standard library and
`OPENAI_API_KEY`.
The `@RosarotePanzer` watermark is excluded from relevant content, including when
OCR splits it into `@Rosarote` and `Panzer` on separate lines.

```bash
export OPENAI_API_KEY='your-key'

# One image: print a JSON result to stdout.
uv run --script scripts/classify_images.py 'https://example.com/meme.jpg'

# Multiple images: concurrent requests, one JSON record per stdout line.
./scripts/classify_images.py 'https://example.com/one.jpg' 'https://example.com/two.jpg' \
    --concurrency 2 > results.jsonl

# Override the OCR/description/template model and reasoning effort (not tags).
./scripts/classify_images.py 'https://example.com/meme.jpg' --model YOUR_MODEL_ID --reasoning-effort high

# Preview archive URLs without an API key or classification charges.
uv run --script scripts/classify_images.py --archive --month 2024/07 --limit 5 --dry-run

# Start with a small sample, then resume the whole archive.
uv run --script scripts/classify_images.py --archive --month 2024/07 --limit 5
uv run --script scripts/classify_images.py --archive --concurrency 4
```

`--concurrency N` limits simultaneous download/classification jobs (default: 4).
Each job makes its content, tag and translation calls sequentially, so at most N
API requests are in flight. A result is emitted/saved only after all required calls
succeed.
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
stderr lines, plus total batch timing. Usage, response IDs and timings are
reported separately for each call. `--pricing` applies only to the
OCR/description/template call for all explicit URLs, never to the tag or translation
call. The translation call uses Luna's built-in pricing.

Cost is an **estimate in USD** per returned response, not a billing lookup;
it excludes charges from earlier retries, account discounts and tax. Built-in
`gpt-6-luna` standard-tier rates are $0.10 input, $0.01 cached input, $0.125 cache
writes and $0.50 output per million tokens, verified on 2026-09-30 from
[OpenAI's model pricing](https://developers.openai.com/api/docs/models/gpt-6-luna).
Above 272K input tokens, input/cache rates double and output is multiplied by 1.5.
Reasoning tokens are already included in output usage and aren't charged twice.
Unknown models/tiers report cost as unavailable rather than guessing. Sol pricing
is not configured, so the tag call reports its token usage but cost is unavailable;
the Luna estimate is not the total cost per image. Override the content call rates
with `--pricing INPUT,CACHED,CACHE_WRITE,OUTPUT` using effective USD/1M token rates
for your model/tier (custom rates are used as-is, with no long-context adjustment).
For example: `--pricing 0.10,0.01,0.125,0.50`. Redirect stderr to a separate file
with `2>debug.log` if needed; API keys and image base64 data are never logged.

The content defaults are model `gpt-6-luna` and `--reasoning-effort low`. Use
`--model` with an identifier available to your account if OpenAI rejects it.
Tags always use `gpt-6.1-sol` with medium reasoning effort, independently of these
options; tag translation always uses `gpt-6-luna` with low reasoning effort, also
independently of these options. Your account needs access to both models. The
image models must support image input and structured JSON output. Authentication/model configuration errors stop the run.

`--reasoning-effort` sets the content call's OpenAI `reasoning.effort` directly.
It and `--no-reasoning` do not affect tags (medium) or tag translation (low).
Choices are `none`,
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
`tags` is a list of concise German or English search keywords or short phrases for supported
subjects, characters, objects, settings, formats and themes. Other-language
keywords are translated into German or English, regardless of the image's text
language. Proper names are
preserved; whitespace, duplicates and the watermark are removed. Tags are
predicted independently of Luna's OCR/template output, with a tags-only prompt
that explicitly requests both **Stichwörter** (subject/theme/joke keywords) and
**Bildmerkmale** (distinctive visible features) as concise entries in the same tags
list, favoring useful search terms over incidental details. A text-only Luna call
then generates counterpart translations: German tags gain English equivalents,
and English tags gain German equivalents. Originals are preserved first, and
translations are appended with case-insensitive deduplication and watermark
removal. The prompt forbids adding new topics and inventing translations of proper
names. For example:
`["Ernie", "Bert", "Sesamstraße", "konzertsaal", "meme", "Sesame Street", "concert hall"]`.

Archive results are appended to `images/classifications.jsonl` (override with
`--output PATH`). Each record includes its source URL, model, `reasoning_effort`,
schema version, timestamp and API token usage for the content call. A separate
`tags_call` object records the tag model, reasoning effort, response ID, returned
model, service tier and token usage. `translation_call` records the same metadata
for the additional Luna query (null when skipped for empty tags). The merged
`classification` fields remain compatible with the gallery and index exporter. Completed records are flushed
immediately and skipped on subsequent runs using the same content/tag/translation
models, reasoning efforts and schema; `--force` appends new classifications instead. Changing the model or
reasoning effort reclassifies images. Resume still reads legacy `thinking` fields
(`light` maps to `low`; `default` or a missing field maps to an omitted parameter),
so this terminology change alone does not trigger reclassification. New records
only use `reasoning_effort` (null when omitted). Results from before the
watermark-exclusion, German-alt-text, tags, separate-tag-call, German/English-only,
Stichwörter/Bildmerkmale or cross-translation changes (schema versions 1–7) are
reclassified on the next archive run, incurring up to three new API requests per
image. The current schema version is 8. Avoid
concurrent runs writing the same output file. If interrupted during a write,
remove/repair the partial final JSONL line before resuming.

The full archive makes **up to three paid API requests per pending image**, plus
possible retries (two when no tags require translation). `--limit` bounds the number of images attempted, not the number of API
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
Tags and meme templates appear below the Telegram metadata in at most three lines.
Only whole tags that fit are shown. Tags that do not fit are removed from the
layout, and later shorter tags are still tried in the remaining space. A compact
`...` button with optically centered periods is positioned over the tag area's
bottom-right corner, without adding vertical space, and appears when any
tags do not fit. Clicking it opens
all tags and the full template name in a floating overlay beside the pointer,
clamped inside the viewport with no gallery layout shifts. Keyboard activation
anchors the overlay to the button. Overlay tags still apply the search/template
filters. Close it with Escape, the close button, a second ellipsis click or an
outside click; page scroll and resize also dismiss it. Long overlay lists scroll
independently. Tag frequencies are counted once after loading,
case-insensitively and once per classified image across the entire catalog.
Thumbnail tags are ordered most-common-first (alphabetically on ties). All tags,
including those used in only one image, are eligible for display; only available
space limits which are shown. Descriptions provide thumbnail tooltips and
accessible labels. Images without results are marked “Not classified”; missing
metadata does not prevent browsing production-hosted images. Search matches OCR,
descriptions, template names and tags case-insensitively; tag suggestions are not
implemented yet.

Offline tests:

```bash
uv run --no-project --python 3.12 --with 'pillow>=11.1.0' --with 'telethon>=1.44.0' python -m unittest discover -s tests -v
node --test tests/test_gallery_classifications.js
```
