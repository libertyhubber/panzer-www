#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "pudb", "ipython",
#   "pillow>=11.1.0",
#   "telethon>=1.44.0"
# ]
# ///
"""
Implements a telegram bot.

Uses the following environment variables:
    PANZER_IMGSYNC_API_ID
    PANZER_IMGSYNC_API_HASH
    PANZER_IMGSYNC_CHANNEL

    PANZER_IMGSYNC_BOT_NAME
    PANZER_IMGSYNC_BOT_TOKEN

Downloads recent channel photos, ingests them into the sibling archive
checkouts, and generates website-local indexes, thumbnail sheets and image
classifications. Publishes generated changes only after classification succeeds.

Filenames contain the post date, message ID and an image fingerprint.

Usage:

    ./scripts/panzer_imgsync.py [-h|--help] [--force] [--no-git]

--no-git performs the sync and ingest without staging, committing or pushing.
"""

import io
import os
import argparse
import re
import sys
import copy
import json
import shutil
import pathlib as pl
import datetime as dt
import contextlib
import subprocess as sp

# Load environment variables
APP_TITLE = 'panzerimgsync'
API_ID = os.environ.get('PANZER_IMGSYNC_API_ID')
API_HASH = os.environ.get('PANZER_IMGSYNC_API_HASH')

# BOT_NAME = os.environ.get('PANZER_IMGSYNC_BOT_NAME', "panzer_imgsync_bot")
# BOT_TOKEN = os.environ.get('PANZER_IMGSYNC_BOT_TOKEN')
# assert BOT_TOKEN, "missing environment variable PANZER_IMGSYNC_BOT_TOKEN"

CHANNEL_NAME = os.environ.get('PANZER_IMGSYNC_CHANNEL', "@RosaroterPanzerBackup")

ROOT_DIR = pl.Path(__file__).parent.parent
IMAGES_DIR = ROOT_DIR / "images"

MESSAGES_CACHE_PATH = ROOT_DIR / "scripts" / "telegram_messages_cache.json"
GALLERY_METADATA_PATH = ROOT_DIR / "images" / "telegram_metadata.json"

_CLIENT = None

def init_telethon_client() -> "telethon.TelegramClient":
    global _CLIENT

    import telethon
    if _CLIENT is None:
        if not API_ID or not API_HASH:
            raise ValueError("set PANZER_IMGSYNC_API_ID and PANZER_IMGSYNC_API_HASH")
        _CLIENT = telethon.TelegramClient(APP_TITLE, int(API_ID), API_HASH)
    return _CLIENT


@contextlib.contextmanager
def change_dir(new_dir: pl.Path):
    old_dir = os.getcwd()

    os.chdir(new_dir)

    try:
        yield
    finally:
        os.chdir(old_dir)


def mk_img_path(fname: str) -> pl.Path:
    datestr = fname.replace("-", "")
    return IMAGES_DIR / datestr[0:4] / datestr[4:6] / fname


def load_last_messages() -> dict[int, dict]:
    """Load messages from the cache file."""
    if not MESSAGES_CACHE_PATH.exists():
        return {}

    with MESSAGES_CACHE_PATH.open(mode='r') as fobj:
        return {int(key): val for key, val in json.load(fobj).items()}


def dump_messages(messages: dict[int, dict]) -> None:
    """Dump messages to the cache file with pretty printing."""
    msg_text = json.dumps(messages, sort_keys=True)
    msg_text = msg_text.replace('}, "', '},\n"')
    msg_data = msg_text.encode("utf-8")

    tmp_path = MESSAGES_CACHE_PATH.parent / (MESSAGES_CACHE_PATH.name + ".tmp")
    with tmp_path.open(mode="wb") as fobj:
        fobj.write(msg_data)

    tmp_path.rename(MESSAGES_CACHE_PATH)


def dump_gallery_metadata(messages: dict[int, dict]) -> None:
    """Write the Telegram fields needed by the public gallery.

    A filename can occasionally occur in more than one Telegram message. In
    that case, retain the oldest post as the original.
    """
    metadata = {}
    for message_id in sorted(messages):
        message = messages[message_id]
        image_name = message.get('name')
        if image_name and image_name not in metadata:
            metadata[image_name] = [
                message_id, message.get('trct', 0),
                message.get('tview'), message.get('tcomments'),
            ]

    metadata_data = json.dumps(
        metadata, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    if GALLERY_METADATA_PATH.exists():
        if GALLERY_METADATA_PATH.read_bytes() == metadata_data:
            return

    tmp_path = GALLERY_METADATA_PATH.with_suffix('.json.tmp')
    tmp_path.write_bytes(metadata_data)
    tmp_path.rename(GALLERY_METADATA_PATH)


def digest_img(data: bytes, ) -> str:
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img = img.resize((4, 4), Image.Resampling.LANCZOS)
    img = img.convert('P', palette=Image.ADAPTIVE, colors=8)

    # Get pixel values
    pixels = list(img.get_flattened_data() if hasattr(img, 'get_flattened_data') else img.getdata())

    # Calculate the mean pixel value
    mean_pixel = sum(pixels) / len(pixels)

    # Generate the fingerprint
    fingerprint = [int(pixel - mean_pixel) for pixel in pixels]
    offset = abs(min(fingerprint))
    octal_str = ''.join(str(offset + val) for val in fingerprint)

    # Convert the list to a hash string
    return hex(int(octal_str, 8))[2:].zfill(12)


def digest_img_path(path: pl.Path) -> str:
    with path.open(mode="rb") as fobj:
        return digest_img(fobj.read())


def test_fingerprint_image():
    imgdir = pl.Path(__file__).parent / "test_images/"
    # print(digest_img_path(imgdir / "test_1_small.jpg"))
    # print(digest_img_path(imgdir / "test_2_small.jpg"))

    assert digest_img_path(imgdir / "test_1_full.jpg")  == "12c254fd9a82"
    assert digest_img_path(imgdir / "test_1_small.jpg") == "12c254fd9a82"
    assert digest_img_path(imgdir / "test_2_full.jpg")  == "a9a0086dbdad"
    assert digest_img_path(imgdir / "test_2_small.jpg") == "a9a0086dbdad"


def _parse_date(date_str):
    date_str = date_str.replace("-", "")
    yyyy, mm, dd = date_str[0:4], date_str[4:6], date_str[6:8]
    return dt.date(int(yyyy, base=10), int(mm, base=10), int(dd, base=10))


assert _parse_date("2024-09-30") == dt.date(2024, 9, 30)


def telegram_stats(msg) -> dict:
    """Current totals from Telegram; absent comment/view fields remain unknown."""
    return {
        'tfwd': msg.forwards or 0,
        'trct': sum(res.count for res in msg.reactions.results) if msg.reactions else 0,
        'tview': msg.views,
        'tcomments': msg.replies.replies if msg.replies else None,
    }


async def fetch_api_messages(old_messages: dict[int, dict]) -> dict[int, dict]:
    # Getting information about yourself
    client = init_telethon_client()
    me = await client.get_me()

    # print("client id/username:", me.id, me.username, me.phone)

    if len(old_messages) == 0:
        min_id = 0
    else:
        lookback = 50      # so we update the fwd and rct fields
        min_id = max(0, max(old_messages) - lookback)

    # Do not silently skip posts when more than 200 arrived since the last sync.
    limit = None if old_messages else 200

    new_messages = copy.deepcopy(old_messages)

    fpaths = reversed(sorted(IMAGES_DIR.rglob("*.jpg")))

    # Used to prevent duplicate uploads.
    # files must have the same digest and have a date,
    #   within 3 days of each other
    digest_paths = {}
    for message in old_messages.values():
        if message.get('dig') and message.get('name'):
            digest_paths.setdefault(message['dig'], []).append(
                (_parse_date(message['name']), message['name'])
            )
    for fpath in fpaths:
        if fpath.name == "thumbnails.jpg" or fpath.name.startswith("thumbnails-"):
            continue

        date = _parse_date(fpath.name)

        with fpath.open(mode='rb') as fobj:
            data = fobj.read()
            digest = digest_img(data)
            candidate = (date, fpath.name)
            candidates = digest_paths.setdefault(digest, [])
            if candidate not in candidates:
                candidates.append(candidate)

    msg_iter = client.iter_messages(CHANNEL_NAME, min_id=min_id, limit=limit)
    async for msg in msg_iter:
        if msg.photo is None:
            # Skip if the message has no photo attached
            continue

        if msg.media.__class__.__name__ != 'MessageMediaPhoto':
            continue

        if msg.file.mime_type != 'image/jpeg':
            print("invalid mime type", msg.file.mime_type)
            continue

        # print("...", {
        #     res.reaction.emoticon: res.count
        #     for res in msg.reactions.results
        # })

        stats = telegram_stats(msg)

        if msg.id in old_messages and old_messages[msg.id].get('name'):
            digest = old_messages[msg.id].get('dig')
            tgt_fname = old_messages[msg.id]['name']

            new_messages[msg.id].update(stats)
            print("old         :", msg.id, digest, tgt_fname)
            continue

        blob = await client.download_media(msg, bytes)
        digest = digest_img(blob)

        fname_prefix = msg.date.isoformat().replace(":", "")[:17]
        tgt_fname = fname_prefix + "_" + str(msg.id) + "_" + digest + ".jpg"
        cur_date = _parse_date(fname_prefix)

        new_messages[msg.id] = {
            'name': tgt_fname,
            **stats,
            'dig' : digest,
        }

        # see if we can find an existing image that matches the digest
        duplicate = next((name for date, name in digest_paths.get(digest, [])
                          if abs((date - cur_date).days) < 3), None)
        if duplicate:
            new_messages[msg.id]['name'] = duplicate
            print("dup detected:", msg.id, digest, fname_prefix, duplicate)
            continue

        if msg.id < 13310:
            new_messages[msg.id]['name'] = None
            print("missing     :", msg.id, digest, fname_prefix, tgt_fname)
        else:
            print("new         :", msg.id, digest, fname_prefix, tgt_fname)

            tgt_fpath = mk_img_path(tgt_fname)
            tgt_fpath.parent.mkdir(parents=True, exist_ok=True)

            tmp_fpath = tgt_fpath.parent / (tgt_fpath.name + ".tmp")
            with tmp_fpath.open(mode='wb') as fobj:
                fobj.write(blob)
            tmp_fpath.rename(tgt_fpath)
            digest_paths.setdefault(digest, []).append((cur_date, tgt_fname))

    return new_messages


IMG_REPOS = {
    "2021": "panzer-archiv-00",
    "2022": "panzer-archiv-00",
    "2023": "panzer-archiv-00",
    "2024": "panzer-archiv-00",
    "2025": "panzer-archiv-01",
    "2026": "panzer-archiv-02",
    "2027": "panzer-archiv-03",
    "2028": "panzer-archiv-04",
    "2029": "panzer-archiv-05",
    "2030": "panzer-archiv-06",
    "2031": "panzer-archiv-07",
    "2032": "panzer-archiv-08",
    "2033": "panzer-archiv-09",
}


def _update_images(args: list[str]) -> list[tuple[pl.Path | None, pl.Path]]:
    client = init_telethon_client()
    old_messages = load_last_messages()

    with client:
        _fetch_cor = fetch_api_messages(old_messages)
        new_messages = client.loop.run_until_complete(_fetch_cor)

    if old_messages != new_messages:
        dump_messages(new_messages)
    dump_gallery_metadata(new_messages)

    cur_dir = pl.Path(".").absolute()
    new_img_dirs = sorted(
        directory for directory in (cur_dir / "images").glob("20*/*")
        if directory.is_dir() and any(
            path.suffix == ".jpg" and path.name != "thumbnails.jpg"
            and not path.name.startswith("thumbnails-")
            for path in directory.iterdir()
        )
    )
    updates = []
    for www_img_dir in new_img_dirs:
        year = www_img_dir.parent.name
        archiv_repo = cur_dir.parent / IMG_REPOS[year]
        if not (archiv_repo / "images").is_dir():
            raise ValueError(f"archive checkout is missing: {archiv_repo}")
        archiv_img_dir = archiv_repo / "images" / year / www_img_dir.name
        archiv_img_dir.mkdir(parents=True, exist_ok=True)

        # Only originals belong in the archive. Never overwrite its index with
        # the website's (possibly stale) sprite index.
        for path in www_img_dir.glob("*.jpg"):
            if path.name == "thumbnails.jpg" or path.name.startswith("thumbnails-"):
                continue
            target = archiv_img_dir / path.name
            if not target.exists() or target.read_bytes() != path.read_bytes():
                print(f"cp {path} {target}")
                shutil.copyfile(path, target)
        updates.append((www_img_dir, archiv_repo))

    if not updates:
        # Retry pending classifications/pushes even when there are no new photos.
        # Future archive checkouts can exist but contain no months yet.
        archives = sorted(set(IMG_REPOS.values()), reverse=True)
        for name in archives:
            repo = cur_dir.parent / name
            if any((repo / "images").glob("*/*/entry_index.json")):
                updates.append((None, repo))
                break
        else:
            if '--force' in args:
                raise ValueError("no populated archive checkout found")
    return updates


def _update_dir_index(www_img_dir):
    if www_img_dir:
        for path in www_img_dir.iterdir():
            if path.name.startswith("thumbnails-") or path.name == "entry_index.json":
                continue
            if path.is_file():
                path.unlink()

    cur_dir = pl.Path(".").absolute()
    dir_index = {}
    for year, repo in IMG_REPOS.items():
        repo_dir = cur_dir.parent / repo
        repo_dir_index_path = repo_dir / "images" / "dir_index.json"
        if repo_dir_index_path.exists():
            with repo_dir_index_path.open() as fobj:
                dir_index.update(json.load(fobj))

    dir_index_path = cur_dir / "images" / "dir_index.json"
    if dir_index_path.exists():
        with dir_index_path.open(mode="r") as fobj:
            if dir_index == json.load(fobj):
                return

    with dir_index_path.open(mode="w") as fobj:
        print("writing dir_index.json")
        json.dump(dir_index, fobj, sort_keys=True, indent=2)


def _commit_archive(archiv_repo, *, no_git: bool = False):
    with change_dir(archiv_repo):
        import ingest_uploads
        ingest_uploads.ingest_archive(archiv_repo, ROOT_DIR)

        if not no_git:
            _publish_generated(["images/"])


def _publish_generated(paths: list[str]) -> None:
    result = sp.run(
        ["git", "status", "--porcelain", "--", *paths],
        capture_output=True, text=True, check=True,
    )
    if result.stdout.strip():
        sp.run(["git", "add", "--", *paths], check=True)
        # Do not include unrelated changes that were already staged by the user.
        sp.run(["git", "commit", "--only", "-m", "update " + dt.date.today().isoformat(),
                "--", *paths], check=True)
    # Retry a previous failed push even if this run generated no new changes.
    sp.run(["git", "push"], check=True)


def _commit_www():
    cur_dir = pl.Path(".").absolute()
    generated_paths = [
        "images/dir_index.json",
        "images/telegram_metadata.json",
        "scripts/telegram_messages_cache.json",
    ]

    if (cur_dir / "images").exists():
        generated_paths.append("images/")

    generated_paths = [path for path in generated_paths if (cur_dir / path).exists()]
    _publish_generated(generated_paths)


def main(args: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--force', action='store_true', help='require a populated archive even without new photos')
    parser.add_argument('--no-git', action='store_true', help='sync and ingest without running git')
    options = parser.parse_args(args)

    updates = _update_images(args)
    for archiv_repo in dict.fromkeys(repo for _, repo in updates):
        if options.no_git:
            _commit_archive(archiv_repo, no_git=True)
        else:
            _commit_archive(archiv_repo)
    # Only remove staging originals after every archive was ingested/published
    # successfully. A failure must leave them available for the next run.
    for www_img_dir, _ in updates:
        _update_dir_index(www_img_dir)
    if not updates:
        _update_dir_index(None)
    if not options.no_git:
        _commit_www()
    return 0

if __name__ == '__main__':
    test_fingerprint_image()
    sys.exit(main(sys.argv[1:]))
