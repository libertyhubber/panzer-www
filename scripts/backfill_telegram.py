#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow>=11.1.0", "telethon>=1.44.0"]
# ///
"""Find missing archive/Telegram links, or refresh recent counts, without ingesting images."""

import argparse
import asyncio
from collections import defaultdict
import datetime as dt
from functools import lru_cache
import hashlib
import io
import json
import logging
import math
from pathlib import Path
import sys
import tempfile
from urllib.parse import quote

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat
from telethon.tl.types import MessageMediaPhoto

import panzer_imgsync as sync
from generate_thumbnails import archive_host, download, positive_int

STATE_PATH = sync.ROOT_DIR / "scripts/telegram_backfill_state.json"
VERIFIED_MATCHES_PATH = sync.ROOT_DIR / "scripts/telegram_verified_matches.json"
DEFAULT_DATE_WINDOW = 7  # Archive filenames can lag Telegram posts by several days.
DEFAULT_PHOTO_CACHE_DIR = Path(tempfile.gettempdir()) / 'panzer-telegram-photos'

# Bounds on 128-pixel RGB previews: allow modest JPEG/chroma recompression,
# while rejecting appreciable changed content even when the average is close.
VISUAL_MAX_RMS = 3.0
VISUAL_MAX_OUTLIER_FRACTION = 0.002
VISUAL_OUTLIER_DIFFERENCE = 12
VISUAL_ALGORITHM_VERSION = 11
# JPEG chroma subsampling can alter RGB edges while leaving text/structure intact.
# The fallback independently bounds luminance/content and color changes.
VISUAL_LUMA_MAX_RMS = 3.0
VISUAL_LUMA_MAX_OUTLIER_FRACTION = 0.00225  # At most 36 pixels in a 128x128 preview.
VISUAL_LUMA_OUTLIER_DIFFERENCE = 8
VISUAL_CHROMA_MAX_RMS = 6.5
VISUAL_CHROMA_MAX_OUTLIER_FRACTION = 0.01
# Stronger chroma errors must disappear under mild smoothing and be confined
# predominantly to color edges present in BOTH images, not local recoloring.
VISUAL_CHROMA_EDGE_RMS = 3.25
VISUAL_CHROMA_EDGE_MAX_OUTLIER_FRACTION = 0.08
VISUAL_CHROMA_EDGE_MAX_FLAT_OUTLIER_FRACTION = 0.008  # At most 131 preview pixels per channel.
# Dense shared text/color edges can retain some error after chroma smoothing.
# Never give this extra allowance to broad color changes with no shared outliers.
VISUAL_CHROMA_EDGE_MAX_BLURRED_RMS = 4.5
VISUAL_CHROMA_EDGE_MAX_BLURRED_OUTLIER_FRACTION = 0.04
VISUAL_CHROMA_EDGE_MIN_SHARED_OUTLIER_FRACTION = 0.01
VISUAL_CHROMA_EDGE_RADIUS = 1.0
VISUAL_CHROMA_EDGE_MIN_DIFFERENCE = 2
# Allow a small chroma rounding bias, but not broad hue changes.
VISUAL_MAX_CHANNEL_MEAN_SHIFT = 1.875
# Resizing/recompression may bias brightness slightly more than hue. Keep
# chroma means tighter, and retain the independent luminance RMS/outlier gates.
VISUAL_MAX_LUMA_MEAN_SHIFT = 2.0


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    temporary.replace(path)


def image_features(blob: bytes) -> tuple[str, str, float, Image.Image]:
    """Exact byte/pixel identities plus a higher-resolution visual comparison."""
    with Image.open(io.BytesIO(blob)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        pixels = hashlib.sha256(
            str(image.size).encode() + b":" + image.tobytes()
        ).hexdigest()
        preview = image.resize((128, 128), Image.Resampling.LANCZOS)
        return hashlib.sha256(blob).hexdigest(), pixels, image.width / image.height, preview


class CoarseImageIndex:
    """Inverted lookup on 4x4 luminance previews; never used to accept a match."""

    SIDE = 4
    BUCKET_WIDTH = 16

    def __init__(self):
        self.fingerprints = {}
        self.buckets = [defaultdict(set) for _ in range(self.SIDE ** 2)]

    @classmethod
    def fingerprint(cls, preview: Image.Image) -> bytes:
        # Use the same 128px preview and Y conversion as the final comparison.
        # Average disjoint blocks: unlike a perceptual hash or LANCZOS,
        # these averages give a conservative lower bound on full-image RMS.
        # Round once explicitly (Pillow resize can round in both passes).
        luma = preview.convert('YCbCr').getchannel('Y')
        width, height = luma.width // cls.SIDE, luma.height // cls.SIDE
        return bytes(round(ImageStat.Stat(luma.crop((x, y, x + width, y + height))).mean[0])
                     for y in range(0, luma.height, height)
                     for x in range(0, luma.width, width))

    def add(self, key: str, preview: Image.Image) -> None:
        fingerprint = self.fingerprint(preview)
        self.fingerprints[key] = fingerprint
        for buckets, value in zip(self.buckets, fingerprint):
            buckets[value // self.BUCKET_WIDTH].add(key)

    def candidates(self, preview: Image.Image, eligible: set[str]) -> set[str]:
        fingerprint = self.fingerprint(preview)
        # An RGB match has Y RMS <= RGB RMS + 1 (integer Y conversion).
        # The fallback already bounds Y directly. Mean rounding adds at most
        # one level to the difference of block means. A single block can have
        # SIDE times the whole-image RMS, so probe adjacent buckets too.
        luma_rms = max(VISUAL_MAX_RMS + 1, VISUAL_LUMA_MAX_RMS)
        radius = math.ceil(self.SIDE * luma_rms + 1)
        matches = eligible.copy()
        for buckets, value in zip(self.buckets, fingerprint):
            nearby = set()
            first = max(0, value - radius) // self.BUCKET_WIDTH
            last = min(255, value + radius) // self.BUCKET_WIDTH
            for bucket in range(first, last + 1):
                nearby.update(matches.intersection(buckets.get(bucket, ())))
            matches = nearby
            if not matches:
                return matches
        # Block-average RMS is also a lower bound, up to mean rounding.
        max_squared = self.SIDE ** 2 * (luma_rms + 1) ** 2
        return {key for key in matches
                if sum((a - b) ** 2 for a, b in zip(fingerprint, self.fingerprints[key])) <= max_squared}


def jpeg_chroma_edges_match(target_ycc: Image.Image, candidate_ycc: Image.Image) -> bool:
    """Bound larger JPEG chroma artifacts without smoothing away content edits."""
    samples = target_ycc.width * target_ycc.height
    for channel in ('Cb', 'Cr'):
        target, candidate = target_ycc.getchannel(channel), candidate_ycc.getchannel(channel)
        raw = ImageChops.difference(target, candidate)
        outliers = raw.point(lambda value: 255 if value > VISUAL_OUTLIER_DIFFERENCE else 0)
        raw_outliers = ImageStat.Stat(outliers).mean[0] / 255
        if raw_outliers > VISUAL_CHROMA_EDGE_MAX_OUTLIER_FRACTION:
            return False
        target_blur = target.filter(ImageFilter.GaussianBlur(VISUAL_CHROMA_EDGE_RADIUS))
        candidate_blur = candidate.filter(ImageFilter.GaussianBlur(VISUAL_CHROMA_EDGE_RADIUS))
        difference = ImageChops.difference(target_blur, candidate_blur)
        blurred_rms = ImageStat.Stat(difference).rms[0]
        blurred_outliers = sum(difference.histogram()[VISUAL_OUTLIER_DIFFERENCE + 1:]) / samples
        if (blurred_rms > VISUAL_CHROMA_EDGE_MAX_BLURRED_RMS
                or blurred_outliers > VISUAL_CHROMA_EDGE_MAX_BLURRED_OUTLIER_FRACTION):
            return False
        # A raw outlier needs color structure in both originals. Measuring each
        # channel against its own blur avoids treating an added colored patch
        # as evidence that its difference is an existing JPEG edge.
        common_edges = ImageChops.darker(ImageChops.difference(target, target_blur),
                                        ImageChops.difference(candidate, candidate_blur))
        flat = common_edges.point(lambda value: 255 if value <= VISUAL_CHROMA_EDGE_MIN_DIFFERENCE else 0)
        flat_outliers = ImageStat.Stat(ImageChops.multiply(outliers, flat)).mean[0] / 255
        if flat_outliers > VISUAL_CHROMA_EDGE_MAX_FLAT_OUTLIER_FRACTION:
            return False
        if (blurred_rms > VISUAL_CHROMA_EDGE_RMS or blurred_outliers > VISUAL_CHROMA_MAX_OUTLIER_FRACTION):
            if raw_outliers - flat_outliers < VISUAL_CHROMA_EDGE_MIN_SHARED_OUTLIER_FRACTION:
                return False
    return True


def jpeg_chroma_match(target: Image.Image, candidate: Image.Image) -> tuple[float, float] | None:
    """Accept limited JPEG color-edge artifacts, not changed text or color casts."""
    target_ycc, candidate_ycc = target.convert('YCbCr'), candidate.convert('YCbCr')
    difference = ImageChops.difference(target_ycc, candidate_ycc)
    rms = ImageStat.Stat(difference).rms
    if rms[0] > VISUAL_LUMA_MAX_RMS or max(rms[1:]) > VISUAL_CHROMA_MAX_RMS:
        return None
    histogram = difference.histogram()
    samples = target.width * target.height
    luma_outliers = sum(histogram[VISUAL_LUMA_OUTLIER_DIFFERENCE + 1:256]) / samples
    chroma_outliers = max(sum(histogram[channel * 256 + VISUAL_OUTLIER_DIFFERENCE + 1:(channel + 1) * 256])
                         / samples for channel in (1, 2))
    if luma_outliers > VISUAL_LUMA_MAX_OUTLIER_FRACTION:
        return None
    if (max(rms[1:]) > VISUAL_CHROMA_EDGE_RMS or chroma_outliers > VISUAL_CHROMA_MAX_OUTLIER_FRACTION):
        if not jpeg_chroma_edges_match(target_ycc, candidate_ycc):
            return None
    # Bound JPEG rounding bias independently for brightness and hue rather
    # than relying on luminance agreement alone to reject broad color shifts.
    target_mean, candidate_mean = ImageStat.Stat(target_ycc).mean, ImageStat.Stat(candidate_ycc).mean
    shifts = [abs(a - b) for a, b in zip(target_mean, candidate_mean)]
    if shifts[0] > VISUAL_MAX_LUMA_MEAN_SHIFT or max(shifts[1:]) > VISUAL_MAX_CHANNEL_MEAN_SHIFT:
        return None
    return rms[0], max(rms[1:])


def choose_match(target, candidates, *, allow_visual: bool = False) -> tuple[str | None, str]:
    """Reject ambiguous matches rather than inventing an original post link."""
    for identity, label in [(0, "bytes"), (1, "pixels")]:
        matches = [name for name, features in candidates if features[identity] == target[identity]]
        if matches:
            return (matches[0], label) if len(matches) == 1 else (None, "ambiguous")
    if not allow_visual:
        return None, "unmatched"

    matches = []
    for name, features in candidates:
        if abs(features[2] / target[2] - 1) > 0.005:
            continue
        difference = ImageChops.difference(target[3], features[3])
        # Tight JPEG/re-sizing tolerance, not a coarse perceptual hash match.
        rms = max(ImageStat.Stat(difference).rms)
        histogram = difference.histogram()
        outliers = sum(sum(histogram[channel * 256 + VISUAL_OUTLIER_DIFFERENCE + 1:(channel + 1) * 256])
                       for channel in range(3))
        if rms <= VISUAL_MAX_RMS and outliers / (128 * 128 * 3) <= VISUAL_MAX_OUTLIER_FRACTION:
            matches.append((name, f'visual (RMS={rms:.3f}; review)'))
        else:
            chroma = jpeg_chroma_match(target[3], features[3])
            if chroma is not None:
                luma_rms, chroma_rms = chroma
                matches.append((name, f'visual (RMS={rms:.3f}; Y={luma_rms:.3f}; '
                                      f'chroma={chroma_rms:.3f}; review)'))
    if len(matches) == 1:
        return matches[0]
    return None, "ambiguous" if matches else "unmatched"


def load_verified_matches() -> dict[int, dict]:
    """Owner-confirmed rendering variants, scoped to a channel and exact bytes."""
    if not VERIFIED_MATCHES_PATH.is_file():
        return {}
    document = json.loads(VERIFIED_MATCHES_PATH.read_text())
    if document['channel'].lstrip('@').casefold() != sync.CHANNEL_NAME.lstrip('@').casefold():
        return {}
    result = {}
    for record in document['matches']:
        message_id = record['message_id']
        if type(message_id) is not int or message_id <= 0:
            raise ValueError(f'invalid verified Telegram message ID: {message_id}')
        if message_id in result:
            raise ValueError(f'duplicate verified Telegram message ID: {message_id}')
        if Path(record['name']).name != record['name']:
            raise ValueError(f'verified archive name must be a filename: {record["name"]}')
        dt.datetime.fromisoformat(record['date'])
        for field in ('archive_sha256', 'telegram_sha256'):
            value = record[field]
            if len(value) != 64 or any(character not in '0123456789abcdef' for character in value):
                raise ValueError(f'invalid {field} for verified Telegram ID {message_id}')
        result[message_id] = record
    return result


class ArchiveMatcher:
    def __init__(self, images_dir: Path, *, days: int = DEFAULT_DATE_WINDOW, allow_visual: bool,
                 include_archive_months: bool = False):
        # Archive checkouts are siblings of the website checkout, just as in sync.
        checkouts_dir = images_dir.resolve().parent.parent
        self.archive_images = {
            year: checkouts_dir / repo / 'images'
            for year, repo in sync.IMG_REPOS.items()
        }
        self.days = days
        self.allow_visual = allow_visual
        self.verified_matches = load_verified_matches() if allow_visual else {}
        self.by_date = defaultdict(list)
        self.image_dates = {}
        # Compact indexes outlive the bounded 128px feature cache. Wide date
        # windows must not repeatedly decode every original after LRU eviction.
        self.indexed_paths = set()
        self.identities = [defaultdict(set), defaultdict(set)]
        self.visual_index = CoarseImageIndex()
        months = json.loads((images_dir / "dir_index.json").read_text())
        if include_archive_months:
            # Reverse imports can create months not yet ingested into the website.
            months = set(months)
            for archive_images in set(self.archive_images.values()):
                months.update(path.parent.relative_to(archive_images).as_posix()
                              for path in archive_images.glob('*/*/entry_index.json'))
        for month in sorted(months):
            index_path = images_dir / month / "entry_index.json"
            archive_index = self.archive_images[month[:4]] / month / 'entry_index.json'
            # Prefer the archive's current index over a potentially stale website copy.
            index_path = next((path for path in [archive_index, index_path] if path.is_file()), None)
            prefix = f"{archive_host(int(month[:4]))}/images/{month}/"
            entries = json.loads(index_path.read_text() if index_path else
                                 download(prefix + "entry_index.json", timeout=30, retries=3))
            for entry in entries:
                name = entry['name']
                if not name.lower().endswith('.jpg') or name.startswith('thumbnails'):
                    continue
                date = dt.date.fromisoformat(name[:10])
                self.by_date[date].append((month + "/" + name, prefix + quote(name)))
                self.image_dates[name] = date

    def unlinked_names(self, messages: dict) -> set[str]:
        linked = {record.get('name') for record in messages.values()}
        return set(self.image_dates) - linked

    def search_windows(self, pending: set[str]) -> list[tuple[dt.date, dt.date]]:
        """Merge missing images' date windows; visit only these periods, newest first."""
        delta = dt.timedelta(days=self.days)
        windows = []
        for date in sorted({self.image_dates[name] for name in pending}):
            start, end = date - delta, date + delta
            if windows and start <= windows[-1][1] + dt.timedelta(days=1):
                windows[-1] = (windows[-1][0], max(end, windows[-1][1]))
            else:
                windows.append((start, end))
        return list(reversed(windows))

    def candidates(self, date: dt.date):
        return [entry for offset in range(-self.days, self.days + 1)
                for entry in self.by_date[date + dt.timedelta(days=offset)]]

    def has_pending(self, date: dt.date, pending: set[str]) -> bool:
        return any(Path(path).name in pending for path, _ in self.candidates(date))

    def plan_signature(self) -> str:
        settings = [sorted(self.image_dates), self.days, self.allow_visual, VISUAL_ALGORITHM_VERSION,
                    VISUAL_MAX_RMS, VISUAL_MAX_OUTLIER_FRACTION, VISUAL_OUTLIER_DIFFERENCE,
                    VISUAL_LUMA_MAX_RMS, VISUAL_LUMA_MAX_OUTLIER_FRACTION, VISUAL_LUMA_OUTLIER_DIFFERENCE,
                    VISUAL_CHROMA_MAX_RMS, VISUAL_CHROMA_MAX_OUTLIER_FRACTION,
                    VISUAL_CHROMA_EDGE_RMS, VISUAL_CHROMA_EDGE_MAX_OUTLIER_FRACTION,
                    VISUAL_CHROMA_EDGE_MAX_FLAT_OUTLIER_FRACTION, VISUAL_CHROMA_EDGE_RADIUS,
                    VISUAL_CHROMA_EDGE_MIN_DIFFERENCE, VISUAL_CHROMA_EDGE_MAX_BLURRED_RMS,
                    VISUAL_CHROMA_EDGE_MAX_BLURRED_OUTLIER_FRACTION,
                    VISUAL_CHROMA_EDGE_MIN_SHARED_OUTLIER_FRACTION, VISUAL_MAX_CHANNEL_MEAN_SHIFT,
                    VISUAL_MAX_LUMA_MEAN_SHIFT, getattr(self, 'verified_matches', {})]
        return hashlib.sha256(json.dumps(settings).encode()).hexdigest()

    @lru_cache(maxsize=512)
    def features(self, relative_path: str, url: str):
        path = self.archive_images[relative_path[:4]] / relative_path
        if path.is_file():
            return image_features(path.read_bytes())
        return image_features(download(url, timeout=30, retries=3))

    def match(self, blob: bytes, date: dt.date, *, message_id: int | None = None) -> tuple[str | None, str]:
        target = image_features(blob)
        entries = self.candidates(date)
        eligible = {path for path, _ in entries}
        # Index *all* nearby originals, including linked ones, so excluding
        # linked duplicates cannot create a spurious unique match. Build lazily
        # as the scan reaches new dates, not for the entire archive at startup.
        for relative_path, url in entries:
            if relative_path in self.indexed_paths:
                continue
            features = self.features(relative_path, url)
            for index, identities in enumerate(self.identities):
                identities[features[index]].add(relative_path)
            if self.allow_visual:
                self.visual_index.add(relative_path, features[3])
            self.indexed_paths.add(relative_path)

        # Preserve bytes-before-pixels precedence and ambiguity across the
        # complete date window, independently of the coarse visual shortlist.
        for index, label in enumerate(('bytes', 'pixels')):
            matches = eligible.intersection(self.identities[index].get(target[index], ()))
            if matches:
                return (Path(next(iter(matches))).name, label) if len(matches) == 1 else (None, 'ambiguous')
        if not self.allow_visual:
            return None, 'unmatched'

        # These are explicit human approvals, not relaxed perceptual thresholds.
        # Preserve exact-match precedence, require the correct post/date/channel,
        # and refuse approvals if either image changed or fell outside the window.
        approved = self.verified_matches.get(message_id)
        if (approved and approved['telegram_sha256'] == target[0]
                and dt.datetime.fromisoformat(approved['date']).date() == date):
            for path, url in entries:
                if Path(path).name == approved['name'] and self.features(path, url)[0] == approved['archive_sha256']:
                    return approved['name'], 'visual (manually verified; byte-pinned)'

        shortlist = self.visual_index.candidates(target[3], eligible)
        candidates = [(Path(path).name, self.features(path, url))
                      for path, url in entries if path in shortlist]
        return choose_match(target, candidates, allow_visual=True)


def checkpoint(messages: dict, state: dict) -> None:
    # The cursor is saved last: an interrupted export can safely replay messages.
    sync.dump_messages(messages)
    sync.dump_gallery_metadata(messages)
    atomic_json(STATE_PATH, state)


async def wait_with_progress(operation, *, label: str, timeout: float | None = None,
                             interval: float = 10, status=None):
    """Report slow operations and cancel timed-out async work without skipping it."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    task = asyncio.create_task(operation)
    try:
        while True:
            elapsed = loop.time() - started
            remaining = interval if timeout is None else min(interval, max(0, timeout - elapsed))
            done, _ = await asyncio.wait({task}, timeout=remaining)
            elapsed = loop.time() - started
            if done:
                result = task.result()
                print(f'{label} | completed in {elapsed:.1f}s', flush=True)
                return result
            detail = f'; {status()}' if status else ''
            if timeout is not None and elapsed >= timeout:
                raise TimeoutError(f'{label} timed out after {timeout:g}s{detail}; '
                                   'cursor not advanced past this message. Rerun to retry, '
                                   'or increase --download-timeout.')
            print(f'{label} | still working after {elapsed:.0f}s{detail}', flush=True)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def photo_size_bytes(size) -> int | None:
    length = getattr(size, 'size', None)
    if length is None and getattr(size, 'sizes', None):
        length = max(size.sizes)
    if length is None and hasattr(size, 'bytes'):
        length = len(size.bytes)
    return length


def select_still_photo_size(photo):
    # photo.sizes contains still images; photo.video_sizes must never be selected.
    # Telethon's default (thumb=None) prefers video variants when they exist.
    sizes = [size for size in (getattr(photo, 'sizes', None) or [])
             if isinstance(getattr(size, 'type', None), str) and (photo_size_bytes(size) or 0) > 0]
    if not sizes:
        raise RuntimeError('photo has no downloadable still-image variant; refusing default media download')
    # A smaller Telegram thumbnail can have more JPEG bytes than the original.
    # Prefer resolution, using byte count only to break equal-resolution ties.
    return max(sizes, key=lambda size: (
        (getattr(size, 'w', 0) or 0) * (getattr(size, 'h', 0) or 0), photo_size_bytes(size),
    ))


def photo_details(photo) -> str:
    """Describe still sizes/DC without logging access hashes or file references."""
    variants = []
    for size in getattr(photo, 'sizes', None) or []:
        length = photo_size_bytes(size)
        byte_size = f'{length:,} bytes' if length is not None else 'size unknown'
        variants.append(f"{getattr(size, 'type', '?')} "
                        f"{getattr(size, 'w', '?')}x{getattr(size, 'h', '?')}: {byte_size}")
    return f"DC {getattr(photo, 'dc_id', '?')}; advertised still images: " + ('; '.join(variants) or 'none')


def photo_cache_path(cache_dir: Path, photo, size) -> Path:
    # Photo IDs identify the media, not the post: edited photos and changed still
    # variants must not reuse old bytes. File references/access hashes expire and
    # are deliberately excluded, as are all matching thresholds.
    identity = [photo.id, photo.dc_id, size.type, getattr(size, 'w', None),
                getattr(size, 'h', None), photo_size_bytes(size)]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return cache_dir / f'{key}.img'


def validate_photo(blob: bytes) -> None:
    # Fully decode rather than trusting a header on a truncated cache entry.
    with Image.open(io.BytesIO(blob)) as image:
        image.load()


def cache_photo(path: Path, blob: bytes) -> None:
    """Publish complete files atomically; cache failures must not block backfill."""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            output.write(blob)
        temporary.replace(path)
    except OSError as exc:
        logging.warning('Cannot write Telegram photo cache %s: %s', path, exc)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                logging.warning('Cannot remove Telegram photo cache temporary file %s: %s', temporary, exc)


async def download_photo(client, msg, *, prefix: str, timeout: float,
                         cache_dir: Path | None = None) -> bytes:
    media_type = type(getattr(msg, 'media', None)).__name__
    print(f'{prefix} | message media: {media_type}; {photo_details(msg.photo)}', flush=True)
    size = select_still_photo_size(msg.photo)
    skipped_videos = len(getattr(msg.photo, 'video_sizes', None) or [])
    print(f'{prefix} | selected still image {size.type}: {photo_size_bytes(size):,} bytes; '
          f'{skipped_videos} video variants ignored', flush=True)
    cache_path = photo_cache_path(cache_dir, msg.photo, size) if cache_dir is not None else None
    if cache_path is not None:
        try:
            blob = cache_path.read_bytes()
            validate_photo(blob)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            print(f'{prefix} | unusable Telegram photo cache; downloading again: {exc}', flush=True)
        else:
            print(f'{prefix} | Telegram photo cache hit: {len(blob):,} bytes ({cache_path})', flush=True)
            return blob
    loop = asyncio.get_running_loop()
    received = 0
    total = photo_size_bytes(size)
    last_report = None

    def transfer_status():
        if total:
            return f'{received:,}/{total:,} bytes ({100 * received / total:.1f}%)'
        return f'{received:,} bytes received' + (' (no chunks received yet)' if not received else '')

    def progress(current, expected):
        nonlocal received, total, last_report
        received, total = current, expected
        now = loop.time()
        if last_report is None or now - last_report >= 10 or (total and received >= total):
            print(f'{prefix} | Telegram transfer: {transfer_status()}', flush=True)
            last_report = now

    blob = await wait_with_progress(
        # Pass the Photo, not the message: webpage previews may also contain a
        # document, which download_media(message) prioritizes over their photo.
        client.download_media(msg.photo, bytes, thumb=size.type, progress_callback=progress),
        label=f'{prefix} | Telegram photo download', timeout=timeout, status=transfer_status,
    )
    if not blob:
        raise RuntimeError(f'no photo bytes returned for message {msg.id} '
                           f'(media={media_type}, still-image variant={size.type})')
    if cache_path is not None:
        validate_photo(blob)
        cache_photo(cache_path, blob)
    return blob


async def probe_messages(client, channel, ids: list[int], *, timeout: float) -> None:
    """Test message retrieval/media separately; never load or change backfill state."""
    failures = []
    print('Diagnostic probe: no backfill cursor, cache, metadata or archive files will be changed.', flush=True)
    for message_id in ids:
        prefix = f'Probe Telegram ID {message_id}'
        print(f'{prefix} | fetching message...', flush=True)
        try:
            results = await wait_with_progress(
                client.get_messages(channel, ids=[message_id]),
                label=f'{prefix} | message fetch', timeout=timeout,
            )
            msg = results[0] if results else None
            if msg is None or not getattr(msg, 'date', None):
                raise RuntimeError('message deleted/unavailable')
            prefix += f" | {msg.date.isoformat(sep=' ', timespec='seconds')}"
            if not msg.photo:
                print(f'{prefix} | no photo', flush=True)
                continue
            print(f'{prefix} | message fetched; downloading photo even if already mapped...', flush=True)
            blob = await download_photo(client, msg, prefix=prefix, timeout=timeout)
            print(f'{prefix} | downloaded {len(blob):,} bytes successfully (not saved)', flush=True)
        except Exception as exc:
            failures.append(message_id)
            print(f'{prefix} | failed: {type(exc).__name__}: {exc}', flush=True)
    if failures:
        raise RuntimeError('Probe failed for Telegram message IDs: ' + ', '.join(map(str, failures)))


async def backfill_history(client, channel, messages: dict, state: dict, matcher,
                           *, limit: int | None, download_timeout: float = 120,
                           photo_cache_dir: Path | None = None) -> None:
    pending = matcher.unlinked_names(messages)
    print(f'Archive gaps: {len(pending)} images without a Telegram association.', flush=True)
    if not pending:
        if 'history_plan' in state:
            state['history_plan']['remaining'] = []
        checkpoint(messages, state)
        print('No missing associations; no history scan needed.', flush=True)
        return
    signature = matcher.plan_signature()
    plan = state.get('history_plan', {})
    if plan.get('signature') != signature or not pending.issubset(set(plan.get('remaining', []))):
        # Old cursors, added archive images, changed match settings and newly
        # unlinked images need a fresh plan, not a cursor that may skip their dates.
        windows = matcher.search_windows(pending)
        plan = {'signature': signature, 'targets': sorted(pending), 'remaining': sorted(pending), 'window_index': 0,
                'windows': [[start.isoformat(), end.isoformat()] for start, end in windows]}
        state['history_plan'] = plan
        state['history_before_id'] = 0
        state.pop('history_last_id', None)
        print('Planning missing-image date windows, newest to oldest; keeping cached mappings.', flush=True)
    plan['remaining'] = sorted(pending)
    processed = 0
    outcomes = defaultdict(int)
    last_date = None
    try:
        while pending and plan['window_index'] < len(plan['windows']):
            start, end = map(dt.date.fromisoformat, plan['windows'][plan['window_index']])
            if not any(start <= matcher.image_dates[name] <= end for name in pending):
                plan['window_index'] += 1
                state['history_before_id'] = 0
                continue
            before_id = state.get('history_before_id', 0)
            position = f'before Telegram ID {before_id}' if before_id else f'before {end + dt.timedelta(days=1)}'
            print(f'History scan: {start} through {end}, newest to oldest, starting {position}; '
                  f'{len(pending)} missing images.', flush=True)
            upper = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), tzinfo=dt.timezone.utc)
            remaining = None if limit is None else limit - processed
            async for msg in client.iter_messages(channel, reverse=False, offset_id=before_id,
                                                  offset_date=upper, limit=remaining):
                date = msg.date.date()
                if date < start:
                    break
                last_date = msg.date.isoformat(sep=' ', timespec='seconds')
                progress = f'{processed + 1}/{limit}' if limit is not None else str(processed + 1)
                prefix = f'[{progress}] {last_date} | Telegram ID {msg.id}'
                filled_gap = False
                record = dict(messages.get(msg.id, {}))
                if not msg.photo:
                    outcome = 'no photo'
                    print(f'{prefix} | no photo', flush=True)
                elif not isinstance(msg.media, MessageMediaPhoto):
                    # msg.photo also exposes webpage preview thumbnails, not just
                    # photo attachments. Never match those against the archive.
                    outcome = 'non-photo media'
                elif record.get('name'):
                    outcome = 'already mapped'
                elif not matcher.has_pending(date, pending):
                    outcome = 'no missing images nearby'
                    print(f'{prefix} | no missing archive images in date window; download skipped', flush=True)
                else:
                    source = 'loading from disk cache or Telegram' if photo_cache_dir is not None else 'downloading from Telegram'
                    print(f'{prefix} | photo not yet mapped; {source}...', flush=True)
                    blob = await download_photo(client, msg, prefix=prefix, timeout=download_timeout,
                                                cache_dir=photo_cache_dir)
                    print(f'{prefix} | loaded {len(blob):,} photo bytes; matching archive originals...', flush=True)
                    name, status = await wait_with_progress(
                        asyncio.to_thread(matcher.match, blob, date, message_id=msg.id), label=f'{prefix} | archive image matching',
                    )
                    if name and name not in pending:
                        outcome = 'archive image already linked'
                        print(f'{prefix} | matches already-linked archive image: {name}; no gap filled', flush=True)
                    else:
                        record.update(sync.telegram_stats(msg))
                        record.update(name=name, dig=sync.digest_img(blob), match_status=status,
                                      date=msg.date.isoformat())
                        messages[msg.id] = record
                        if name:
                            pending.remove(name)
                            plan['remaining'] = sorted(pending)
                            filled_gap = True
                        if status in ('bytes', 'pixels'):
                            outcome = 'new exact match'
                            detail = f'new exact match ({status}): {name}'
                        elif status.startswith('visual'):
                            outcome = 'visual match'
                            detail = f'{status}: {name}'
                        else:
                            outcome = status
                            detail = f'{status}: no image link added'
                        print(f'{prefix} | {detail} | {len(pending)} missing images remain', flush=True)
                outcomes[outcome] += 1
                state['history_before_id'] = msg.id
                processed += 1
                if filled_gap or processed % 50 == 0:
                    checkpoint(messages, state)
                    print(f'Checkpoint: {processed} messages; latest processed date {last_date}; '
                          f'Telegram ID {msg.id}; {len(pending)} missing images', flush=True)
                if not pending or (limit is not None and processed >= limit):
                    break
            if not pending or (limit is not None and processed >= limit):
                break
            plan['window_index'] += 1
            if plan['window_index'] < len(plan['windows']):
                state['history_before_id'] = 0
            checkpoint(messages, state)
    finally:
        # Never advance past a failed download/match; Ctrl-C preserves finished work.
        checkpoint(messages, state)
    summary = ', '.join(f'{count} {outcome}' for outcome, count in outcomes.items())
    print(f'Processed {processed} messages' + (f': {summary}.' if summary else '.'), flush=True)
    if last_date:
        print(f'Latest processed date: {last_date}.', flush=True)
    print(f'{len(pending)} archive images still lack a Telegram association.', flush=True)
    if not pending:
        print('All archive images linked; stopping history scan.', flush=True)
    elif plan['window_index'] >= len(plan['windows']):
        print('All missing-image date windows searched. Remaining images may be absent from this channel '
              'or unmatchable; --restart retries them.', flush=True)
    else:
        print('Rerun to resume missing-image searches; --restart to retry searched windows.', flush=True)


def cached_post_date(record: dict) -> dt.date | None:
    # New records retain the actual post date. Legacy mapped records have only
    # the archive date in their filename; verify the real date after fetching.
    try:
        if record.get('date'):
            return dt.datetime.fromisoformat(record['date']).date()
        return dt.date.fromisoformat(record.get('name', '')[:10])
    except (TypeError, ValueError):
        return None


async def refresh_stats(client, channel, messages: dict, state: dict, *, limit: int | None,
                        recent_days: int = 30, today: dt.date | None = None) -> None:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    cutoff = today - dt.timedelta(days=max(0, recent_days - 1))
    if state.get('stats_recent_days') != recent_days:
        state['stats_last_id'] = 0
        state['stats_recent_days'] = recent_days
    pending = [key for key in sorted(messages) if recent_days and key > state.get('stats_last_id', 0)
               and messages[key].get('name') and cached_post_date(messages[key]) is not None
               and cached_post_date(messages[key]) >= cutoff]
    print(f'Refreshing counts only for mapped posts from {cutoff} onward '
          f'({recent_days} recent days; 0 disables refresh).', flush=True)
    selected = pending if limit is None else pending[:limit]
    for start in range(0, len(selected), 100):
        ids = selected[start:start + 100]
        results = await client.get_messages(channel, ids=ids)
        for message_id, msg in zip(ids, results):
            if msg is None or not getattr(msg, 'photo', None):
                print(f"{message_id}: deleted/unavailable; preserving cached totals", flush=True)
                continue
            messages[message_id]['date'] = msg.date.isoformat()
            if msg.date.date() < cutoff:
                print(f'{message_id}: older than recent window; preserving cached totals', flush=True)
                continue
            messages[message_id].update(sync.telegram_stats(msg))
        state['stats_last_id'] = ids[-1]
        checkpoint(messages, state)
        print(f"Refreshed through Telegram ID {ids[-1]}", flush=True)
    if len(selected) == len(pending):
        # The next invocation starts a fresh snapshot of all cached posts.
        state['stats_last_id'] = 0
        checkpoint(messages, state)
    print(f"Requested totals for {len(selected)} cached posts.")


async def run(options) -> None:
    client = sync.init_telethon_client()
    async with client:
        channel = await client.get_entity(sync.CHANNEL_NAME)
        if options.probe_message:
            await probe_messages(client, channel, options.probe_message, timeout=options.download_timeout)
            return
        state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
        messages = sync.load_last_messages()
        if state.get('channel_id', channel.id) != channel.id:
            raise ValueError("backfill state belongs to another Telegram channel")
        state['channel_id'] = channel.id
        cursor = 'stats_last_id' if options.stats_only else 'history_before_id'
        if options.restart:
            state[cursor] = 0
            if not options.stats_only:
                state.pop('history_plan', None)
        if options.stats_only:
            await refresh_stats(client, channel, messages, state, limit=options.limit,
                                recent_days=options.recent_days)
        else:
            print('Loading archive indexes for photo matching...', flush=True)
            matcher = ArchiveMatcher(sync.IMAGES_DIR, days=options.date_window,
                                     allow_visual=options.allow_visual_matches)
            photo_cache_dir = None if options.no_photo_cache else options.photo_cache_dir
            if photo_cache_dir is not None:
                print(f'Telegram photo disk cache: {photo_cache_dir}', flush=True)
            await backfill_history(client, channel, messages, state, matcher,
                                   limit=options.limit, download_timeout=options.download_timeout,
                                   photo_cache_dir=photo_cache_dir)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stats-only', action='store_true',
                        help='refresh counts only for recent mapped posts, without image downloads')
    parser.add_argument('--recent-days', type=int, default=30,
                        help='stats-only: refresh posts from the last N calendar days (default: 30; 0 disables)')
    parser.add_argument('--limit', type=positive_int, help='maximum messages to process this run')
    parser.add_argument('--restart', action='store_true',
                        help='replan searches for remaining archive gaps, or restart the recent stats scan')
    parser.add_argument('--date-window', type=int, default=DEFAULT_DATE_WINDOW,
                        help='candidate archive dates within +/- N days (default: %(default)s)')
    parser.add_argument('--allow-visual-matches', action='store_true',
                        help='opt in to approximate JPEG/resizing matches; review resulting links')
    parser.add_argument('--download-timeout', type=positive_int, default=120,
                        help='maximum seconds per Telegram photo download (default: 120; retry on rerun)')
    parser.add_argument('--photo-cache-dir', type=Path, default=DEFAULT_PHOTO_CACHE_DIR,
                        help='temporary Telegram photo cache directory (default: %(default)s)')
    parser.add_argument('--no-photo-cache', action='store_true',
                        help='download Telegram photos without reading/writing the disk cache')
    parser.add_argument('--probe-message', type=positive_int, nargs='+', metavar='ID',
                        help='diagnose downloads for these message IDs without changing backfill files')
    parser.add_argument('--telegram-log', action='store_true',
                        help='show Telethon INFO logs for flood waits, connections and retries')
    options = parser.parse_args(argv)
    if options.date_window < 0:
        parser.error('--date-window must be nonnegative')
    if options.recent_days < 0:
        parser.error('--recent-days must be nonnegative')
    if options.probe_message and (options.stats_only or options.restart or options.limit is not None):
        parser.error('--probe-message cannot be combined with --stats-only, --restart or --limit')
    if options.telegram_log or options.probe_message:
        logging.basicConfig(level=logging.WARNING,
                            format='%(asctime)s %(levelname)s %(name)s: %(message)s')
        logging.getLogger('telethon').setLevel(logging.INFO)
    try:
        asyncio.run(run(options))
    except KeyboardInterrupt:
        print('Interrupted; rerun the same command to resume.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'Backfill failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
