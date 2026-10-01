#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow>=11.1.0", "telethon>=1.39.0"]
# ///
"""Backfill Telegram links/current totals without ingesting or renaming archive images."""

import argparse
import asyncio
from collections import defaultdict
import datetime as dt
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import sys
from urllib.parse import quote

from PIL import Image, ImageChops, ImageOps, ImageStat

import panzer_imgsync as sync
from generate_thumbnails import archive_host, download, positive_int

STATE_PATH = sync.ROOT_DIR / "scripts/telegram_backfill_state.json"


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
        outliers = sum(sum(histogram[channel * 256 + 13:(channel + 1) * 256])
                       for channel in range(3))
        if rms <= 2.5 and outliers / (128 * 128 * 3) <= 0.001:
            matches.append((name, rms))
    if len(matches) == 1:
        name, rms = matches[0]
        return name, f"visual (RMS={rms:.3f}; review)"
    return None, "ambiguous" if matches else "unmatched"


class ArchiveMatcher:
    def __init__(self, images_dir: Path, archive_roots: list[Path], *, days: int,
                 allow_visual: bool):
        self.archive_roots = archive_roots
        self.days = days
        self.allow_visual = allow_visual
        self.by_date = defaultdict(list)
        months = json.loads((images_dir / "dir_index.json").read_text())
        for month in sorted(months):
            index_path = images_dir / month / "entry_index.json"
            local_indexes = [root / "images" / month / "entry_index.json" for root in archive_roots]
            index_path = next((path for path in [index_path, *local_indexes] if path.exists()), None)
            prefix = f"{archive_host(int(month[:4]))}/images/{month}/"
            entries = json.loads(index_path.read_text() if index_path else
                                 download(prefix + "entry_index.json", timeout=30, retries=3))
            for entry in entries:
                name = entry['name']
                if not name.lower().endswith('.jpg') or name.startswith('thumbnails'):
                    continue
                date = dt.date.fromisoformat(name[:10])
                self.by_date[date].append((month + "/" + name, prefix + quote(name)))

    @lru_cache(maxsize=512)
    def features(self, relative_path: str, url: str):
        for root in self.archive_roots:
            path = root / "images" / relative_path
            if path.exists():
                return image_features(path.read_bytes())
        return image_features(download(url, timeout=30, retries=3))

    def match(self, blob: bytes, date: dt.date) -> tuple[str | None, str]:
        candidates = []
        for offset in range(-self.days, self.days + 1):
            for relative_path, url in self.by_date[date + dt.timedelta(days=offset)]:
                candidates.append((Path(relative_path).name, self.features(relative_path, url)))
        return choose_match(image_features(blob), candidates, allow_visual=self.allow_visual)


def checkpoint(messages: dict, state: dict) -> None:
    # The cursor is saved last: an interrupted export can safely replay messages.
    sync.dump_messages(messages)
    sync.dump_gallery_metadata(messages)
    atomic_json(STATE_PATH, state)


async def backfill_history(client, channel, messages: dict, state: dict, matcher,
                           *, limit: int | None) -> None:
    processed = 0
    try:
        # Ascending traversal needs its own cursor, not max(existing cache IDs).
        async for msg in client.iter_messages(channel, reverse=True,
                                              min_id=state.get('history_last_id', 0), limit=limit):
            if msg.photo:
                record = dict(messages.get(msg.id, {}))
                record.update(sync.telegram_stats(msg))
                if not record.get('name'):
                    blob = await client.download_media(msg, bytes)
                    if not blob:
                        raise RuntimeError(f"no photo bytes returned for message {msg.id}")
                    name, status = await asyncio.to_thread(matcher.match, blob, msg.date.date())
                    record.update(name=name, dig=sync.digest_img(blob), match_status=status)
                    print(f"{msg.id}: {status}: {name or '-'}", flush=True)
                messages[msg.id] = record
            state['history_last_id'] = msg.id
            processed += 1
            if processed % 50 == 0:
                checkpoint(messages, state)
                print(f"Checkpoint: {processed} messages; Telegram ID {msg.id}", flush=True)
    finally:
        # Never advance past a failed download/match; Ctrl-C preserves finished work.
        checkpoint(messages, state)
    print(f"Processed {processed} messages. Rerun to continue; --restart to rescan.")


async def refresh_stats(client, channel, messages: dict, state: dict, *, limit: int | None) -> None:
    pending = [key for key in sorted(messages) if key > state.get('stats_last_id', 0)]
    selected = pending if limit is None else pending[:limit]
    for start in range(0, len(selected), 100):
        ids = selected[start:start + 100]
        results = await client.get_messages(channel, ids=ids)
        for message_id, msg in zip(ids, results):
            if msg is None or not getattr(msg, 'photo', None):
                print(f"{message_id}: deleted/unavailable; preserving cached totals", flush=True)
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
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    messages = sync.load_last_messages()
    client = sync.init_telethon_client()
    async with client:
        channel = await client.get_entity(sync.CHANNEL_NAME)
        if state.get('channel_id', channel.id) != channel.id:
            raise ValueError("backfill state belongs to another Telegram channel")
        state['channel_id'] = channel.id
        cursor = 'stats_last_id' if options.stats_only else 'history_last_id'
        if options.restart:
            state[cursor] = 0
        if options.stats_only:
            await refresh_stats(client, channel, messages, state, limit=options.limit)
        else:
            matcher = ArchiveMatcher(sync.IMAGES_DIR, options.archive_root, days=options.date_window,
                                     allow_visual=options.allow_visual_matches)
            await backfill_history(client, channel, messages, state, matcher, limit=options.limit)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stats-only', action='store_true',
                        help='refresh all cached posts in batches, without image downloads')
    parser.add_argument('--limit', type=positive_int, help='maximum messages to process this run')
    parser.add_argument('--restart', action='store_true', help='restart the selected scan from the beginning')
    parser.add_argument('--archive-root', type=Path, action='append', default=[],
                        help='prefer originals from an archive checkout (repeatable)')
    parser.add_argument('--date-window', type=int, default=3,
                        help='candidate archive dates within +/- N days (default: 3)')
    parser.add_argument('--allow-visual-matches', action='store_true',
                        help='opt in to approximate JPEG/resizing matches; review resulting links')
    options = parser.parse_args(argv)
    if options.date_window < 0:
        parser.error('--date-window must be nonnegative')
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
