#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow>=11.1.0", "telethon>=1.44.0"]
# ///
"""Backfill Telegram photo attachments missing from the archive, oldest first.

Existing associations are authoritative. Unmapped photos are checked against the
existing archive before importing; ambiguous matches are left for manual review.
Default mode writes originals, archive indexes and Telegram metadata, but does
not classify, generate website sprites, commit or push. --ingest opts into the
normal classification/website pipeline; --publish also commits and pushes.
"""

import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import quote

import backfill_telegram as backfill
import ingest_uploads
from telethon.tl.types import MessageMediaPhoto

sync = backfill.sync
STATE_PATH = sync.ROOT_DIR / 'scripts/telegram_import_state.json'


def archive_for_year(year: str) -> Path:
    name = sync.IMG_REPOS.get(year)
    if not name:
        raise ValueError(f'no archive configured for year {year}')
    repo = sync.ROOT_DIR.parent / name
    if not (repo / 'images').is_dir():
        raise ValueError(f'archive checkout is missing: {repo}')
    return repo


def save_original(path: Path, blob: bytes) -> None:
    """A deterministic path makes retrying an interrupted import idempotent."""
    if path.exists():
        if path.read_bytes() != blob:
            raise ValueError(f'refusing to overwrite a different archive original: {path}')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.jpg.tmp')
    temporary.write_bytes(blob)
    temporary.replace(path)


def checkpoint(messages: dict, state: dict, dirty: set[Path]) -> None:
    # Publish indexes before mappings/cursor. If indexing fails, the saved cursor
    # remains behind the affected photos; deterministic paths make retries safe.
    for repo in sorted(dirty):
        ingest_uploads.update_indexes(repo)
    sync.dump_messages(messages)
    sync.dump_gallery_metadata(messages)
    backfill.atomic_json(STATE_PATH, state)
    dirty.clear()


async def import_history(client, channel, messages: dict, state: dict, matcher,
                         *, limit: int | None, dry_run: bool = False,
                         download_timeout: int = 120,
                         photo_cache_dir: Path | None = None) -> None:
    cursor = state.get('last_id', 0)
    outcomes = Counter()
    dirty = set()
    # Exact identities for newly imported photos avoid duplicate originals within
    # this batch. Never use the small perceptual filename fingerprint as identity.
    imported_bytes = {}
    processed = 0
    print(f'Telegram → archive: oldest first, after ID {cursor}.', flush=True)
    try:
        async for msg in client.iter_messages(channel, reverse=True, min_id=cursor, limit=limit):
            prefix = f'[{processed + 1}] {msg.date.isoformat()} | Telegram ID {msg.id}'
            record = dict(messages.get(msg.id, {}))
            if not msg.photo or not isinstance(msg.media, MessageMediaPhoto):
                outcome = 'non-photo attachment'
            elif record.get('name'):
                outcome = 'already mapped'
                if not dry_run and record.get('match_status') == 'imported':
                    # The cache may have been saved just before a failed state
                    # checkpoint. Recover its ingest queue before advancing.
                    repo = archive_for_year(record['name'][:4])
                    state['pending_archives'] = sorted(set(state.get('pending_archives', [])) | {repo.name})
            elif dry_run:
                repo = archive_for_year(str(msg.date.year))
                outcome = 'unmapped photo (would match/import)'
                print(f'{prefix} | {outcome} → {repo.name}', flush=True)
            else:
                # Check routing before spending time on a download. Unsupported
                # dates must fail without silently advancing the cursor.
                repo = archive_for_year(str(msg.date.year))
                blob = await backfill.download_photo(
                    client, msg, prefix=prefix, timeout=download_timeout,
                    cache_dir=photo_cache_dir,
                )
                backfill.validate_photo(blob)
                identity = hashlib.sha256(blob).hexdigest()
                name = imported_bytes.get(identity)
                status = 'bytes' if name else None
                if not name:
                    name, status = await backfill.wait_with_progress(
                        asyncio.to_thread(matcher.match, blob, msg.date.date(), message_id=msg.id),
                        label=f'{prefix} | checking existing archive',
                    )
                if status == 'ambiguous':
                    outcome = 'ambiguous (not imported)'
                    print(f'{prefix} | ambiguous existing archive match; left unmapped for review', flush=True)
                else:
                    digest = sync.digest_img(blob)
                    prefix_date = msg.date.isoformat().replace(':', '')[:17]
                    expected_name = f'{prefix_date}_{msg.id}_{digest}.jpg'
                    if name and name != expected_name:
                        outcome = 'linked existing original'
                    else:
                        # An interrupted checkpoint can leave an indexed original
                        # without its cache record/ingest queue. Recover both.
                        name = expected_name
                        path = repo / 'images' / str(msg.date.year) / f'{msg.date.month:02}' / name
                        save_original(path, blob)
                        dirty.add(repo)
                        # Include newly imported originals in subsequent visual/
                        # pixel comparisons, even before indexes are checkpointed.
                        relative = path.relative_to(repo / 'images').as_posix()
                        url = f'{backfill.archive_host(msg.date.year)}/images/{quote(relative)}'
                        if name not in matcher.image_dates:
                            matcher.by_date[msg.date.date()].append((relative, url))
                            matcher.image_dates[name] = msg.date.date()
                        state['pending_archives'] = sorted(set(state.get('pending_archives', [])) | {repo.name})
                        status = 'imported'
                        outcome = 'imported original'
                    imported_bytes[identity] = name
                    record.update(sync.telegram_stats(msg))
                    record.update(name=name, dig=digest, date=msg.date.isoformat(), match_status=status)
                    messages[msg.id] = record
                    print(f'{prefix} | {outcome}: {name}', flush=True)
            outcomes[outcome] += 1
            processed += 1
            if not dry_run:
                state['last_id'] = msg.id
                if processed % 50 == 0:
                    checkpoint(messages, state, dirty)
                    print(f'Checkpoint: {processed} messages, through Telegram ID {msg.id}', flush=True)
    finally:
        if not dry_run:
            checkpoint(messages, state, dirty)
    print(f'{"Previewed" if dry_run else "Processed"} {processed} messages: ' +
          ', '.join(f'{count} {outcome}' for outcome, count in outcomes.items()), flush=True)
    if outcomes['ambiguous (not imported)']:
        print('Resolve ambiguous mappings, then use --restart to revisit them.', flush=True)


def ingest_pending(state: dict, *, publish: bool) -> None:
    """Keep a durable ingest queue across classification or git failures."""
    pending = set(state.get('pending_archives', []))
    if publish:
        pending.update(state.get('unpublished_archives', []))
    for name in sorted(pending):
        repo = sync.ROOT_DIR.parent / name
        sync._commit_archive(repo, no_git=not publish)
    with sync.change_dir(sync.ROOT_DIR):
        sync._update_dir_index(None)
        if publish:
            sync._commit_www()
    # Clear only after all website assets and optional pushes succeeded.
    state['pending_archives'] = []
    state['unpublished_archives'] = [] if publish else sorted(
        set(state.get('unpublished_archives', [])) | pending
    )
    backfill.atomic_json(STATE_PATH, state)


async def run(options) -> None:
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    messages = sync.load_last_messages()
    client = sync.init_telethon_client()
    async with client:
        channel = await client.get_entity(sync.CHANNEL_NAME)
        if state.get('channel_id', channel.id) != channel.id:
            raise ValueError('import state belongs to another Telegram channel')
        state['channel_id'] = channel.id
        if options.restart:
            state['last_id'] = 0
        matcher = None
        if not options.dry_run:
            matcher = backfill.ArchiveMatcher(sync.IMAGES_DIR, allow_visual=True,
                                              include_archive_months=True)
        await import_history(client, channel, messages, state, matcher,
                             limit=options.limit, dry_run=options.dry_run,
                             download_timeout=options.download_timeout,
                             photo_cache_dir=None if options.no_photo_cache else options.photo_cache_dir)
    if options.ingest:
        ingest_pending(state, publish=options.publish)
    elif not options.dry_run and state.get('pending_archives'):
        print('Originals/indexes saved locally. Rerun with --ingest to generate website assets '
              'and classify (requires OPENAI_API_KEY); add --publish to commit/push.', flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=backfill.positive_int,
                        help='maximum history messages scanned this run, including mapped/non-photo posts')
    parser.add_argument('--dry-run', action='store_true',
                        help='list unmapped photo attachments without downloads or file changes')
    parser.add_argument('--restart', action='store_true', help='rescan from the oldest post; preserve existing mappings')
    parser.add_argument('--ingest', action='store_true', help='also generate website sprites and classify pending originals')
    parser.add_argument('--publish', action='store_true', help='with --ingest, commit and push archives and website')
    parser.add_argument('--download-timeout', type=backfill.positive_int, default=120)
    parser.add_argument('--photo-cache-dir', type=Path, default=backfill.DEFAULT_PHOTO_CACHE_DIR)
    parser.add_argument('--no-photo-cache', action='store_true')
    options = parser.parse_args(argv)
    if options.publish and not options.ingest:
        parser.error('--publish requires --ingest')
    if options.dry_run and options.ingest:
        parser.error('--dry-run cannot be combined with --ingest')
    try:
        asyncio.run(run(options))
    except KeyboardInterrupt:
        print('Interrupted; rerun to resume completed imports.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'Telegram import failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
