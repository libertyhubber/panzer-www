#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow>=11.1.0", "telethon>=1.44.0"]
# ///
"""Rank Telegram photos for manual archive-link review; never change mappings."""

import argparse
import asyncio
from bisect import bisect_left, bisect_right
import contextlib
from dataclasses import dataclass, field
import datetime as dt
import io
import math
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageChops, ImageOps, ImageStat
from telethon.tl.types import MessageMediaPhoto

import backfill_telegram as backfill

sync = backfill.sync
TOP_COUNT = 5


@dataclass
class Features:
    aspect: float
    preview: Image.Image


def image_features(blob: bytes) -> Features:
    with Image.open(io.BytesIO(blob)) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
        preview = image.resize((128, 128), Image.Resampling.LANCZOS).convert('YCbCr')
        aspect = image.width / image.height
    return Features(aspect, preview)


def visual_distance(target: Features, candidate: Features) -> dict:
    """Continuous pixel-distance ranking, not an acceptance test or confidence."""
    y, cb, cr = ImageStat.Stat(ImageChops.difference(target.preview, candidate.preview)).rms
    aspect_difference = abs(math.log(candidate.aspect / target.aspect))
    # Favor structure/luminance, retain color evidence, and penalize stretched
    # comparisons. No hard aspect/RMS/outlier gates: even rejected automatic
    # matches can be useful suggestions. Not invariant to crops or added borders.
    score = math.sqrt((2 * y * y + cb * cb + cr * cr) / 4) + 32 * aspect_difference
    return {'score': score, 'luma_rms': y, 'chroma_rms': max(cb, cr),
            'aspect_log_difference': aspect_difference}


@dataclass
class Candidate:
    message_id: int
    date: dt.date
    url: str
    linked_name: str | None
    features: Features


@dataclass
class Target:
    name: str
    date: dt.date
    url: str
    features: Features
    matches: list = field(default_factory=list)
    compared: int = 0

    def consider(self, candidate: Candidate) -> None:
        distance = visual_distance(self.features, candidate.features)
        self.compared += 1
        self.matches.append((distance, candidate))
        # Keep distinct posts even if their photos are identical. Ties are
        # deterministic, oldest message ID first; suggestions never create links.
        self.matches.sort(key=lambda item: (item[0]['score'], item[1].message_id))
        del self.matches[TOP_COUNT:]


def select_entries(matcher, identifiers: list[str], *, unmatched: bool,
                   messages: dict, limit: int | None) -> list[tuple[str, str]]:
    entries = {Path(path).name: (path, url)
               for daily in matcher.by_date.values() for path, url in daily}
    if unmatched or not identifiers:
        # Match the gallery's newest-first order; limit before decoding originals.
        names = sorted(matcher.unlinked_names(messages), reverse=True)
    else:
        names = []
        for identifier in identifiers:
            parsed = urlsplit(identifier)
            name = Path(unquote(parsed.path)).name
            if name not in entries:
                raise ValueError(f'archive image not present in indexes: {identifier}')
            if '://' in identifier or parsed.netloc:
                canonical = urlsplit(entries[name][1])
                allowed_hosts = {canonical.hostname, 'localhost', '127.0.0.1', '::1'}
                if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in allowed_hosts
                        or unquote(parsed.path) != unquote(canonical.path)):
                    raise ValueError(f'expected an archive or localhost image URL for {name}, got {identifier}')
            if name not in names:
                names.append(name)
    if limit is not None:
        names = names[:limit]
    return [entries[name] for name in names]


def load_targets(matcher, entries) -> list[Target]:
    targets = []
    for number, (path, url) in enumerate(entries, 1):
        if number == 1 or number % 100 == 0:
            print(f'Loading archive images: {number}/{len(entries)}', file=sys.stderr, flush=True)
        original = matcher.archive_images[path[:4]] / path
        blob = original.read_bytes() if original.is_file() else backfill.download(url, timeout=30, retries=3)
        name = Path(path).name
        targets.append(Target(name, matcher.image_dates[name], url, image_features(blob)))
    return targets


def telegram_url(channel, message_id: int) -> str:
    if getattr(channel, 'username', None):
        return f'https://t.me/{channel.username}/{message_id}'
    return f'https://t.me/c/{channel.id}/{message_id}'


async def photo_features(client, msg, options) -> Features:
    size = backfill.select_still_photo_size(msg.photo)
    path = backfill.photo_cache_path(options.photo_cache_dir, msg.photo, size)
    try:
        # Decoding/converting fully loads the image and detects truncated bytes.
        return image_features(path.read_bytes())
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        print(f'Telegram ID {msg.id}: unusable photo cache: {exc}', file=sys.stderr, flush=True)
    with contextlib.redirect_stdout(sys.stderr):
        blob = await backfill.download_photo(client, msg, prefix=f'Review Telegram ID {msg.id}',
                                             timeout=options.download_timeout,
                                             cache_dir=options.photo_cache_dir)
    return image_features(blob)


async def rank_history(client, channel, targets, matcher, messages, options, progress) -> None:
    ordered = sorted(targets, key=lambda target: target.date)
    dates = [target.date for target in ordered]
    delta = dt.timedelta(days=options.date_window)
    windows = [(None, None)] if options.all_history else matcher.search_windows({t.name for t in targets})
    for start, end in windows:
        upper = None if end is None else dt.datetime.combine(
            end + dt.timedelta(days=1), dt.time(), tzinfo=dt.timezone.utc)
        print(f'Scanning {start or "start of channel"} through {end or "latest post"}...',
              file=sys.stderr, flush=True)
        async for msg in client.iter_messages(channel, reverse=False, offset_date=upper):
            date = msg.date.date()
            if start is not None and date < start:
                break
            progress['messages'] += 1
            already_mapped = bool(messages.get(msg.id, {}).get('name'))
            if already_mapped:
                progress['skipped_mapped'] += 1
            if progress['messages'] % 100 == 0:
                print(f"Reviewed {progress['messages']} posts; {progress['photos']} unmatched photos; "
                      f"{progress['skipped_mapped']} mapped posts skipped; "
                      f"at {date}, Telegram ID {msg.id}", file=sys.stderr, flush=True)
            # Review is intentionally different from automatic backfill: only
            # propose unassociated posts, skipping cache reads/decodes/downloads
            # for mapped ones. Unmatched and ambiguous records remain eligible.
            if already_mapped or not isinstance(msg.media, MessageMediaPhoto) or not msg.photo:
                continue
            nearby = ordered if options.all_history else ordered[
                bisect_left(dates, date - delta):bisect_right(dates, date + delta)]
            if not nearby:
                continue
            try:
                features = await photo_features(client, msg, options)
            except Exception as exc:
                detail = f'Telegram ID {msg.id}: {type(exc).__name__}: {exc}'
                progress['failures'].append(detail)
                print(detail, file=sys.stderr, flush=True)
                continue
            progress['photos'] += 1
            candidate = Candidate(msg.id, date, telegram_url(channel, msg.id),
                                  messages.get(msg.id, {}).get('name'), features)
            for target in nearby:
                target.consider(candidate)
    progress['complete'] = not progress['failures']


def print_results(targets, options, progress) -> None:
    scope = 'entire channel history' if options.all_history else f'±{options.date_window} calendar days per archive image'
    status = 'Complete scan' if progress['complete'] else 'INCOMPLETE scan — suggestions may change on rerun'
    print(f"{status}. Scope: {scope}; {progress['photos']} unmatched photos; "
          f"{progress['skipped_mapped']} mapped posts skipped; "
          f"{len(progress['failures'])} failures. Lower score means closer pixels, not confidence.",
          file=sys.stderr, flush=True)
    for target in targets:
        print(f'\n{target.name} — {target.compared} Telegram photos compared\n  Archive: {target.url}', flush=True)
        for rank, (distance, candidate) in enumerate(target.matches, 1):
            linked = f'  already linked to {candidate.linked_name}' if candidate.linked_name else ''
            print(f"  {rank}. {candidate.url}  score={distance['score']:.3f}  {candidate.date}{linked}", flush=True)
        if not target.matches:
            print('  No Telegram photo candidates in the scanned scope.', flush=True)


async def run(options) -> None:
    print('Review only: no backfill cursor, message mappings, metadata or archive files will be changed.',
          file=sys.stderr, flush=True)
    print(f'Telegram photo cache: {options.photo_cache_dir}', file=sys.stderr, flush=True)
    messages = sync.load_last_messages()
    matcher = backfill.ArchiveMatcher(sync.IMAGES_DIR, days=options.date_window, allow_visual=False)
    entries = select_entries(matcher, options.images, unmatched=options.unmatched,
                             messages=messages, limit=options.limit)
    targets = load_targets(matcher, entries)
    progress = {'messages': 0, 'photos': 0, 'skipped_mapped': 0, 'failures': [], 'complete': False}
    try:
        if targets:
            print(f'Reviewing {len(targets)} archive images; top {TOP_COUNT} distinct posts per image.',
                  file=sys.stderr, flush=True)
            async with sync.init_telethon_client() as client:
                channel = await client.get_entity(sync.CHANNEL_NAME)
                await rank_history(client, channel, targets, matcher, messages, options, progress)
        else:
            progress['complete'] = True
            print('No unmatched archive images selected.', file=sys.stderr, flush=True)
    finally:
        # Interrupted/failed runs still print explicitly incomplete suggestions.
        print_results(targets, options, progress)
    if progress['failures']:
        raise RuntimeError(f'{len(progress["failures"])} photo failures; results are incomplete. Rerun to retry.')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('images', nargs='*',
                        help='archive JPEG URLs or filenames; omit to review unmatched archive images')
    parser.add_argument('--unmatched', action='store_true',
                        help='review unmatched archive images (default when no images are supplied)')
    parser.add_argument('--limit', type=backfill.positive_int,
                        help='review only the first N archive images, newest first in unmatched mode (not a post limit)')
    parser.add_argument('--date-window', type=int, default=90, help='search ±N calendar days per image (default: 90)')
    parser.add_argument('--all-history', action='store_true', help='rank photos from the entire channel, ignoring date windows')
    parser.add_argument('--photo-cache-dir', type=Path, default=backfill.DEFAULT_PHOTO_CACHE_DIR)
    parser.add_argument('--download-timeout', type=backfill.positive_int, default=120)
    options = parser.parse_args(argv)
    if options.images and options.unmatched:
        parser.error('provide archive images or --unmatched, but not both')
    options.unmatched = options.unmatched or not options.images
    if options.date_window < 0:
        parser.error('--date-window must be nonnegative')
    try:
        asyncio.run(run(options))
    except KeyboardInterrupt:
        print('Interrupted; suggestions are incomplete. Rerun to reuse cached photos.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'Review failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
