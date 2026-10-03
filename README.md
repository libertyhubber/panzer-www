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

## Header navigation

The header has separate **Links** and **Spenden** buttons. They open bottom sheets
on mobile and centered dialogs on desktop. The support dialog includes the GoFundMe
campaign and PayPal, Lightning, Bitcoin, and Monero, in that order.

Edit `templates/navigation.html` for link targets and content, `assets/navigation.css`
for appearance, and `assets/navigation.js` for dialog and clipboard behavior. Run
`uv run --script scripts/gen_html.py index.html` to regenerate the main page without
an index export. Navigation controls work independently of gallery initialization.
Escape, the close button, or a backdrop click closes a dialog and restores focus.
Without JavaScript, the links, donation targets, and QR codes remain visible.

The QR images and SVG overlays reuse `assets/icons.css` unchanged. The existing
Monero and Bitcoin QR destinations differ from the direct wallet links; this
existing discrepancy is retained to preserve the original codes. Confirm the
intended destinations before any address change. Clipboard access requires a
secure context, such as HTTPS or localhost. An HTTP preview through the WSL IP
falls back to address selection for manual copy.

Run navigation and gallery regression tests with:

```sh
node --test tests/test_navigation.js tests/test_gallery_classifications.js
```

## Shareable gallery navigation

The address bar records the gallery's active filters and selected lightbox image.
Copy the URL to another tab or share it to restore those filters and open that image.
Supported query parameters are:

- `q`: search text (including tag-click searches).
- `template`: the full meme-template name.
- `reactions`: minimum Telegram reaction count.
- `image`: a stable archive-relative image ID, such as `2024/07/filename.jpg`.

Defaults are omitted. Unrelated query parameters and URL fragments are preserved.
Filter changes, image opening and image closing create browser-history entries;
lightbox slide changes update the current entry. Back/Forward restores filters and
the lightbox without reloading the page. Scroll position is kept only in the tab's
local history state, never in the URL or shared between tabs. Filter changes reset
scroll to the top. Image links use filenames rather than list positions, so newer
uploads do not change which image opens. Filtered image links wait for indexes and
metadata before opening; missing or filter-excluded images leave the gallery usable
and show a status message. These links share browsing state, not a frozen snapshot
of metadata or archive contents. Transient tag/debug overlays and lightbox zoom are
not stored.

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
order and contains image names, original dimensions and an optional `bg` background
color (three uppercase hexadecimal digits without `#`, e.g. `000` or `FFF`). During
ingestion, pixels in the outer band (5% of the shorter dimension, at least one pixel)
are rounded to the nearest three-digit RGB color, then the most common color is
selected. Each pixel counts once and ties use the first encountered color. Sampling
a band prevents thin decorative frames from dominating the background selection.
Square or nearly square images omit `bg` when
`abs(w - h) / max(w, h) < 0.05`; exactly 5% still receives a color. Ingestion removes
obsolete `bg` values from near-square entries and reuses their saved dimensions
without reopening originals. Other archive entries missing `bg` are backfilled on
the next ingest; saved colors are reused. Sprite padding and original-image tiles
use this color, falling back to black for indexes without `bg`.
To refresh cached colors and sprites without any classification/API calls, run from
this repository (repeat for other archive checkouts as needed):

```bash
uv run python - <<'PY'
from pathlib import Path
from scripts.ingest_uploads import update_indexes
from scripts.generate_thumbnails import update_thumbnails

archive = Path('../panzer-archiv-02')
update_indexes(archive, refresh_backgrounds=True)
update_thumbnails(archive)
PY
```

The gallery displays 220 px tiles and loads **local sprites/indexes** as quick previews for unfiltered
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
in either mode. During scroll, the visible card window updates at most once per
animation frame. Cards show their indexed background color, date and available
metadata without starting new image requests; already loaded images remain visible.
Cards that remain in the rendered window retain their fitted tags and overflow
counters during scroll and idle image hydration. Tag previews are refitted only
when their content or card width changes; newly entered cards receive their first
fit at idle.
Uncached months first show neutral card shells, then receive dates and colors when
their indexes arrive. New sprite/original requests and queued quality upgrades
resume after 150 ms without scroll or resize events. Filtering loads all indexes,
but images only for this bounded window. Monthly indexes use six independent
request slots: each completed or failed request
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
Daily sync classifies only newly downloaded originals (plus staging originals
retained after a failed run), using both the OCR/content and tag calls before either
repository is pushed, then exports both gallery classification indexes. It never
classifies the unprocessed archive backlog. Originals are read from the local
archive, so newly ingested images do not need to be published first. Results are
appended to the website's `images/classifications.jsonl`; any existing result is
preserved regardless of schema, model, reasoning effort or partial-stage status.
Changing classification defaults therefore cannot trigger a paid archive-wide
rerun during daily sync. Archive backfills, schema upgrades and partial-stage
completion use `classify_images.py` explicitly. `OPENAI_API_KEY` is required only
when new-image work is pending. A classification failure aborts publishing and
retains successful records and staging originals for the next attempt.
Standalone `ingest_uploads.py` classifies all originals without any saved result
in the selected archive, but never refreshes existing results or runs Git itself.
For an alternate website checkout use `ingest_uploads.py ARCHIVE --www-repo PATH`.

To sync/ingest without running Git, use `make sync_and_ingest SYNC_ARGS=--no-git`
(or `uv run --script scripts/panzer_imgsync.py --no-git`). This still downloads
photos, updates archive indexes and local assets, classifies only new/pending
staging originals (incurring API charges), and removes ingested staging originals.
Without new or retained staging originals, sync still updates the latest populated
archive's assets and retries publishing, but makes no classification API calls.
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
completed Telegram ID and archives awaiting ingest/publication. A local `--ingest`
retains the archive publication queue for a later `--ingest --publish` run.
Imports checkpoint every 50
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
Each full classification uses two structured-output calls to `gpt-6-luna`:
low reasoning effort for OCR, languages, German description, image type and meme
template; medium reasoning effort for tags and their German/English equivalents
in a single bilingual request. Both calls receive the original image,
downloaded/read only once. There is no separate translation request. The script
needs only Python's standard library and `OPENAI_API_KEY`.
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

# Count affected images and estimate the full cost, without an API key or charges.
uv run --script scripts/classify_images.py --archive --dry

# Keep schema-4-and-newer results; classify only missing/older images.
uv run --script scripts/classify_images.py --archive --min-schema 4 --dry
uv run --script scripts/classify_images.py --archive --min-schema 4

# Refresh only OCR/content, preserving existing tags.
uv run --script scripts/classify_images.py --archive --ocr-only --dry
uv run --script scripts/classify_images.py --archive --ocr-only

# Refresh only tags + translations, preserving OCR/descriptions/templates.
uv run --script scripts/classify_images.py --archive --tags-only --dry
uv run --script scripts/classify_images.py --archive --tags-only
```

`--ocr-only` runs the existing **content call**: OCR, languages, German alt text,
image type and meme template. It makes no tag or translation calls, preserves
existing tags and their API metadata, and uses an empty tag list for a new image.
`--tags-only` runs one bilingual Luna tagging call with medium reasoning,
preserving all content fields and content-call metadata. Archive
images without a saved content record are skipped and reported; classify them
with `--ocr-only` or a full run first. For explicit URLs, missing content in
`--output` is an error detected before any image downloads or API calls. Explicit
URL results still go to stdout; `--output` supplies the existing records for merging,
but is only appended automatically in archive mode. The two stage options are
mutually exclusive. `--force` reruns the selected stage(s).

Partial updates record `stage_schema_versions` for `content` and `tags`, while the
whole record's `schema_version` is their minimum. A tag refresh therefore does not
claim that old OCR was upgraded. A new OCR-only result has content version 8 and
tag version 0 (not yet generated); it is usable by the gallery, but not considered
a complete classification. Repeating a partial run skips completed work for that
stage, independently of the untouched stage's model/schema. Upgrading both stages
brings the whole record to schema 8. Older records without stage metadata inherit
their recorded version (an absent tag field means tag version 0).

`--min-schema N` explicitly accepts schemas **N or newer**, regardless of model or
reasoning-effort differences. It selects only missing/older results, using the
**last appended record** for each image. With partial modes, it checks the selected
stage's version instead of the whole record. It requires archive mode and accepts
1–8. Without this option, current-schema/model/effort matching still applies.
`--force` overrides acceptance. Automatic ingest/sync preserves every saved result,
including old-schema and partial-stage records. Daily sync additionally restricts
classification to the download/staging batch; it does not backfill the archive.
The standalone classifier still uses strict model/effort matching unless
`--min-schema` is supplied.

`--dry` uses the same selection, month filters, force flag and attempt limit as an
actual run, but prints an affected-image count, maximum call count and approximate
USD cost instead of URLs. It fetches monthly archive indexes, **not images**, makes
no OpenAI calls, writes nothing and needs no API key. Estimates sum the selected
stages' historical mean costs for matching models/reasoning settings from
`--output`. If usage samples are unavailable, fallback input/output counts are
915/277 for content and 799/281 for bilingual tags, assuming uncached input.
Content counts are historical schema-8 averages; the combined tag estimate uses
old tag input and summed tag/translation output counts and is not yet calibrated
for Luna medium reasoning. Historical separate-translation calls are excluded
from new tagging estimates. Sizes, caching and reasoning can vary;
retry charges, discounts and tax are excluded. Unknown model pricing is reported
as unavailable, not a misleading partial total; use `--pricing` to supply content
rates. `--dry-run` retains its existing URL-list behavior and cannot be combined
with `--dry`.

`--concurrency N` limits simultaneous download/classification jobs (default: 4).
Each job makes its content and bilingual tag calls sequentially, so at most N
API requests are in flight. A result is emitted/saved only after all required calls
succeed.
Use `--concurrency 1` for sequential processing or lower the value if you hit API
rate limits. Before paid work begins, batch runs discover the selected URLs and
print the pending-image total. Each completion reports `[completed/total]`, a
percentage, and separate success/failure counts on stderr. The total respects
resume/schema filtering, skipped images and `--limit`; failures count as completed
attempts, not successes. Resumed runs show progress for the remaining selected
work, not previously completed images. Discovery collects only URLs; image downloads
and API work submission remain bounded rather than queueing the entire archive.
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
OCR/description/template call for all explicit URLs, never to the tag call.
The bilingual tag call uses Luna's built-in pricing.

Cost is an **estimate in USD** per returned response, not a billing lookup;
it excludes charges from earlier retries, account discounts and tax. Built-in
`gpt-6-luna` standard-tier rates are $0.10 input, $0.01 cached input, $0.125 cache
writes and $0.50 output per million tokens, verified on 2026-09-30 from
[OpenAI's model pricing](https://developers.openai.com/api/docs/models/gpt-6-luna).
Above 272K input tokens, input/cache rates double and output is multiplied by 1.5.
For legacy records, Sol standard-tier rates are **$2.00 input, $0.10 cached input, $2.50 cache writes
and $10.00 output** per million tokens, verified on 2026-10-02 from
[Sol's model pricing](https://developers.openai.com/api/docs/models/gpt-6.1-sol).
The same >272K long-context multipliers apply to Sol. Both models now report
per-call costs. Reasoning tokens are already included in output usage and aren't
charged twice. Unknown models/tiers report cost as unavailable rather than guessing.
Partial runs report only the calls actually made, not preserved historical calls.
Override the content call rates with `--pricing INPUT,CACHED,CACHE_WRITE,OUTPUT`
for explicit URLs or an archive `--dry` preview, using effective USD/1M token rates
for your model/tier (custom rates are used as-is, with no long-context adjustment).
For example: `--pricing 0.10,0.01,0.125,0.50`. Redirect stderr to a separate file
with `2>debug.log` if needed; API keys and image base64 data are never logged.

The content defaults are model `gpt-6-luna` and `--reasoning-effort low`. Use
`--model` with an identifier available to your account if OpenAI rejects it.
Tags and translations always use one `gpt-6-luna` call with medium reasoning
effort, independently of these options. The image models must support image input
and structured JSON output. Authentication/model configuration errors stop the run.

`--reasoning-effort` sets the content call's OpenAI `reasoning.effort` directly.
It and `--no-reasoning` do not affect bilingual tags (medium).
Choices are `none`,
`low`, `medium`, `high`, and `xhigh`; support varies by model. Use
`--no-reasoning` to omit the reasoning parameter entirely and use the model's
default (for example, with a model that doesn't support configurable reasoning).
This differs from `--reasoning-effort none`, which explicitly requests no reasoning.
The former `--thinking` option and `light` alias have been removed.

Bilingual Luna tagging replaces Sol tagging plus a separate Luna translation
without changing the output schema (still version 8). Normal sync/ingest preserves
all existing results, including older schemas. Standalone strict resume treats the former
pipeline as outdated; use `--min-schema 8` there to preserve existing results, or
`--tags-only --dry` to preview an explicit tag refresh before spending credits.
`--dry` uses matching bilingual medium-effort samples when available, otherwise
the documented, provisional fallback counts.

Archive mode reads `images/dir_index.json`, fetches monthly `entry_index.json`
files from the gallery's **`archivX.derrosarotepanzer.com`** hosts, and downloads
full-resolution images (not thumbnail sheets). `--month YYYY/MM` is repeatable;
`--dir-index PATH` selects another directory index. Supported inputs are JPEG,
PNG, WebP and non-animated GIF, up to 20 MiB each.

Results include OCR `text`, `languages`, `tags`, `tags_de`, `tags_en`, `image_type`, a concise factual German
`description` suitable for the image's `alt` attribute, and a `meme_template` with `status` (`recognized`, `unknown`, or `none`), nullable `name`,
confidence and visual evidence. OCR and template matches are model predictions
and can be wrong; `unknown` avoids forcing a guessed template name.
Classification tags are concise German or English search keywords or short phrases for supported
subjects, characters, objects, settings, formats and themes. Other-language
keywords are translated into German or English, regardless of the image's text
language. Proper names are
preserved; whitespace, duplicates and the watermark are removed. Tags are
predicted independently of Luna's OCR/template output, with a tags-only prompt
that explicitly requests both **Stichwörter** (subject/theme/joke keywords) and
**Bildmerkmale** (distinctive visible features) as concise entries in the same tags
list, favoring useful search terms over incidental details. The same image-based
Luna call returns **two explicit language lists**, `tags_de` and `tags_en`, with
German keywords and their English equivalents. Both lists receive case-insensitive
deduplication and watermark removal. Shared terms and unchanged proper names belong
to both API lists; established translations use their respective language's name.
For saved results, terms present in both languages are moved into the neutral `tags`
list, leaving German-only `tags_de` and English-only `tags_en`. During backfill,
uncertain or uncached terms also go into neutral `tags`, without guessing a language.
The three lists are disjoint. Search, catalog counts and the full overlay use their
deduplicated union; thumbnails show `tags_de` plus neutral `tags`.
The prompt forbids adding new topics
and inventing translations of proper names. For example:

```json
{
  "tags_de": ["Sesamstraße", "konzertsaal"],
  "tags_en": ["Sesame Street", "concert hall"],
  "tags": ["Bert"]
}
```

Language lists are optional when reading older saved classifications. New tag
results record `tag_format_version: 2` independently of the content/stage schema
(still 8). Format 0/1 records with combined `tags` remain readable; the exporter and
UI distinguish their old semantics from format 2's neutral tags. Strict tag/full
resume requires the current format; OCR-only resume is unchanged.
`--min-schema` remains an explicit override accepting legacy combined tags, and
normal ingest still preserves every existing record. OCR-only updates preserve
all saved tag lists and their format marker; tag-only updates replace all three lists.

Archive results are appended to `images/classifications.jsonl` (override with
`--output PATH`). Each record includes its source URL, model, `reasoning_effort`,
schema version, timestamp and API token usage for the content call. A separate
`tags_call` object records the tag model, reasoning effort, response ID, returned
model, service tier and token usage, including the bilingual output.
`translation_call` is null for new tag results; historical separate-translation
metadata remains readable and is preserved by OCR-only updates. The merged
`classification` fields remain compatible with the gallery and index exporter.
Completed records are flushed immediately. Standalone strict resume skips records
using the same content/tag models, reasoning efforts and schema with no separate
translation call and with the current split-tag format, unless `--min-schema` supplies an explicit
acceptance threshold; partial runs check only their selected stage. `--force`
appends new classifications instead. Without `--min-schema`, changing the model or
reasoning effort reclassifies the affected stage(s). Resume still reads legacy `thinking` fields
(`light` maps to `low`; `default` or a missing field maps to an omitted parameter),
so this terminology change alone does not trigger reclassification. New records
use `reasoning_effort` (null when omitted); tag-only updates can retain legacy
content metadata. Without `--min-schema`, full archive runs reclassify results from
before the watermark-exclusion, German-alt-text, tags, separate-tag-call,
German/English-only, Stichwörter/Bildmerkmale or cross-translation changes
(schema versions 1–7), incurring two new API requests per image. The current schema version is 8. Avoid
concurrent runs writing the same output file. If interrupted during a write,
remove/repair the partial final JSONL line before resuming.

A full run makes **two paid API requests per pending image**, plus possible
retries. OCR-only and tag-only runs each make one. `--limit` bounds the number of images attempted, not the number of API
requests including retries. Image-specific failures are reported on stderr and
retried on the next run; a run with any failures exits nonzero. Use `--timeout`
and `--retries` to adjust network behavior. Downloaded image bytes are sent to
OpenAI, with response storage disabled (`store: false`); images are not saved
locally by this script.

### Backfilling tag languages without reclassifying images

`scripts/backfill_tag_languages.py` splits the latest existing tags using a reusable
case-normalized language lookup. It never downloads images or rewrites OCR.
The default/`--dry` mode is read-only; `--label` explicitly opts into **paid text-only
API calls** and `--apply` separately appends partitioned classifications without API
calls. No monthly `entry_index.json` files are changed.

```bash
# Preview distinct tags and request counts, without API calls or writes.
uv run --script scripts/backfill_tag_languages.py --dry

# Paid pilot: label at most 100 distinct tags, using Luna/low by default.
uv run --script scripts/backfill_tag_languages.py --label --limit 100

# Resume paid text-only labeling; cache each validated batch immediately.
uv run --script scripts/backfill_tag_languages.py --label

# Retry missing/conflicting/failed tags after the pass, using smaller batches if needed.
# Existing cached labels are skipped; no repeated calls for the successful tags.
uv run --script scripts/backfill_tag_languages.py --label --batch-size 20

# Inspect counts and uncertain tags before applying the migration.
uv run --script scripts/backfill_tag_languages.py --dry --report /tmp/tag-language-report.json

# Apply without review: shared/uncertain/uncached tags go into neutral tags.
uv run --script scripts/backfill_tag_languages.py --apply
# Optional reviewed labels can still be supplied with --overrides PATH.
make classification-index
```

Default paths are `images/classifications.jsonl` (`--input`) and
`images/tag_language_cache.jsonl` (`--cache`). The cache is append-only, with batch
API diagnostics and labels `de`, `en`, `both` (shared words/names), or `unknown`.
Reviewed `--overrides` take precedence. `--retry-unknown` explicitly retries uncertain
labels during `--label`; otherwise only uncached tags incur new calls. `--batch-size`
(default 40) and `--concurrency` (default 8) control tags per call and maximum
simultaneous text calls. `--limit` (tags, not images), `--model`, `--reasoning-effort`,
`--timeout` and `--retries` further bound/configure labeling. Use `--concurrency 1`
for sequential processing, or lower concurrency if API rate limits cause retries.
Only a bounded set of batches is submitted; the main thread caches results in
completion order and updates progress/cost totals. Isolated request/response failures
are reported without stopping later batches. For well-formed partial responses, only
exact requested tags with unambiguous labels are cached: agreeing duplicate rows
are accepted, conflicting duplicates remain pending, and unexpected/rewritten tags
are ignored. Missing tags are not guessed. Even an empty usable result retains the
response's usage metadata in the cache. Malformed or incomplete responses are discarded.

After all scheduled batches finish, the pass reports attempted/cached tag counts and
exits 1 if any batches failed or returned incomplete coverage (not an early abort).
Rerun `--label` to retry only remaining uncached tags, optionally with `--batch-size 20`;
repeat `--retry-unknown` if uncertain cached labels should also be retried.
`--report` includes the pass counts and the remaining missing/uncertain tag lists.
Authentication/model-configuration errors and interruption still stop new submissions
and drain successful in-flight work before exit. Cache write failures remain fatal.

Returned token costs are reported per batch with a running total for the current run
(excluding previously cached batches). Partial responses count their full returned
usage, not just the labels accepted. If a batch cost is unavailable (including failed
requests without accessible usage), the total is marked as known costs only.
Retries can incur additional charges. Tags are treated as untrusted text.

Missing/uncertain labels do **not** block migration: shared (`both`), uncertain
(`unknown`) and uncached tags go into neutral `tags`. Only `de`/`en` labels populate
`tags_de`/`tags_en`. `--report PATH` still lists missing/uncertain labels for optional
review, but all pending records are ready to apply. `--apply` upgrades legacy combined
records (including format 1) to format 2; existing format-2 records are skipped, so
applying again is idempotent. The union of all original tags, content, model/usage
metadata, `classified_at` and stage versions are preserved; a separate
`tag_language_backfill` object records migration time and the neutral unresolved
policy. No translations or new tags are invented, and no paid calls are needed to apply.
Do not run concurrent writers against the same input/cache, and repair a truncated
JSONL tail before resuming. Regenerate the gallery classification index after applying.

### Classification metadata in the gallery

```bash
# Refresh the compact UI index after classifying more images.
make classification-index
# Also included in the normal HTML build.
make html
```

`scripts/export_classifications.py` reads `images/classifications.jsonl` and writes
two indexes, keyed by archive-relative image paths: `images/classification_index.json`
contains neutral `tags`, `tags_de`/`tags_en`, a tag-format marker and template names
(or legacy combined tags for older records); `images/classification_text_index.json` contains
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
At viewport widths up to 600px, the filter form uses two control rows: reactions
and template side by side, then full-width search. Controls have fixed heights,
and the template wrapper has an explicit width before options load, preventing
classification data and changing placeholders from reflowing the form. An empty
status is hidden; active filter/status messages appear below the controls.

JavaScript positions 220px × 290px gallery cards in 228–240px wide column slots
and 305px rows, without CSS card margins, padding, or background colors. Column counts use the minimum
8px horizontal gap; spare width increases that gap up to 20px without sacrificing
columns. Cards are centered horizontally within each slot, and any remaining width
centers the grid. Vertical gaps are 15px. The maximum container width allows six
columns with 20px horizontal gaps.
Below 456px gallery width, two columns use whole-pixel thumbnail widths, a 7px
gap, and at least 4px outer gutters. At 375px, thumbnails and cards are 180px wide;
remaining space after rounding is centered. Below 320px, the gallery falls back
to one column. Card heights decrease by the same amount as the thumbnail width,
leaving the metadata area unscaled: 180px thumbnails use 250px cards and 265px rows.
The 15px vertical gap remains fixed. Gallery height, row positioning, and viewport
rendering all use the responsive row height. Existing 220px sprite sheets
are scaled only in the browser: background widths and tile offsets use the same
scale, with automatic height for partial sheets. Sprite generation is unchanged;
originals still use `contain`. Metadata text is never scaled and uses 24px lines,
while tags retain 18px lines and transparent backgrounds. Cards have 5px rounded corners and clip
overflowing content; wrapped metadata can leave less room for the lower tags.
Tags and meme templates appear below the Telegram metadata in a 39px section,
with at most two lines of tags.
Only whole tags that fit are shown. Tags that do not fit are removed from the
layout, and later shorter tags are still tried in the remaining space. An inline
`+N` button appears after the visible tags when any do not fit, showing the number
of omitted items and reserving its own space within the same two lines. Clicking
it opens all tags and the full template name in a floating overlay beside the
pointer, with its top aligned to the tag group and no gallery layout shifts.
Keyboard activation anchors the overlay horizontally to the button. The overlay
stays within the viewport, scrolling long lists within the space below its top.
Overlay tags still apply the search/template filters. Close it with Escape, the
close button, a second counter click or an outside click; page scroll and resize
also dismiss it. The image metadata debug button is disabled by default; add
`debug=1` to the query string to enable it on any host. Tag frequencies are counted once after loading,
case-insensitively and once per classified image across the entire catalog.
Thumbnail tags are ordered most-common-first (alphabetically on ties). All tags,
including those used in only one image, are eligible for display if they belong to
`tags_de` or neutral `tags` (shared/unresolved). English-only tags are overlay-only
and count in `+N` even without spatial
overflow. Empty German and neutral lists show no inline tags. For older records
without language lists, the conservative JavaScript vocabulary heuristic remains
a temporary fallback until migration; ambiguous/shared vocabulary stays eligible.
Templates are not language-filtered. All combined tags remain in the overlay and searchable,
and available space limits which other tags are shown. Descriptions provide thumbnail tooltips and
accessible labels. Images without results are marked “Not classified”; missing
metadata does not prevent browsing production-hosted images. Search matches OCR,
descriptions, template names and tags case-insensitively. Unquoted terms use AND
matching in any order: `steuern diebstahl` and `diebstahl steuern` find the same
images, even with intervening words or terms in different fields. Double quotes
require a phrase within one OCR text, description, template name or individual
tag: `"steuern sind diebstahl"`. Terms and phrases can be combined, for example
`politik "steuern sind diebstahl"`; every term and phrase must match. An unfinished
quote treats the rest of the input as a phrase. Empty quoted phrases are ignored;
a query with only empty quotes has no matches.

Search retains substring matching, whitespace normalization, `ä/ö/ü` equivalence
to `a/o/u`, and tolerance for punctuation not explicitly present in each term or
phrase. Original spelling and quotes remain in the input and shared URL. Queries
are parsed and regex matchers are compiled once per filter pass. Results remain
newest-first; typo tolerance, relevance ranking and tag suggestions are not
implemented yet.

Offline tests:

```bash
uv run --no-project --python 3.12 --with 'pillow>=11.1.0' --with 'telethon>=1.44.0' python -m unittest discover -s tests -v
node --test tests/test_gallery_classifications.js
```
