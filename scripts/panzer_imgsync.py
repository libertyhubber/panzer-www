"""
Implements a telegram bot.

Uses the following environment variables:
    PANZER_IMGSYNC_API_ID
    PANZER_IMGSYNC_API_HASH
    PANZER_IMGSYNC_CHANNEL

    PANZER_IMGSYNC_BOT_NAME
    PANZER_IMGSYNC_BOT_TOKEN

The bot downloads recent images from the channel
and write them to the "upload" directory.

The filenames should use the date (in isoformat) as a prefix
and the sha256 hash of the file contents as a suffix.
"""

import io
import os
import sys
import copy
import json
import pathlib as pl
import datetime as dt
import telethon

from PIL import Image

# Load environment variables
APP_TITLE = 'panzerimgsync'
API_ID = os.environ.get('PANZER_IMGSYNC_API_ID')
API_HASH = os.environ.get('PANZER_IMGSYNC_API_HASH')
assert API_ID, "missing envvar: PANZER_IMGSYNC_API_ID"
assert API_HASH, "missing envvar: PANZER_IMGSYNC_API_HASH"

# BOT_NAME = os.environ.get('PANZER_IMGSYNC_BOT_NAME', "panzer_imgsync_bot")
# BOT_TOKEN = os.environ.get('PANZER_IMGSYNC_BOT_TOKEN')
# assert BOT_TOKEN, "missing environment variable PANZER_IMGSYNC_BOT_TOKEN"

CHANNEL_NAME = os.environ.get('PANZER_IMGSYNC_CHANNEL', "@RosaroterPanzerBackup")

ROOT_DIR = pl.Path(__file__).parent.parent
IMAGES_DIR = ROOT_DIR / "images"

client = telethon.TelegramClient(APP_TITLE, API_ID, API_HASH)


MESSAGES_CACHE_PATH = ROOT_DIR / "scripts" / "telegram_messages_cache.json"


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


def digest_img(data: bytes, ) -> str:
    img = Image.open(io.BytesIO(data))
    img = img.resize((4, 4), Image.Resampling.LANCZOS)
    img = img.convert('P', palette=Image.ADAPTIVE, colors=8)

    # Get pixel values
    pixels = list(img.getdata())

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


test_fingerprint_image()


def _parse_date(date_str):
    date_str = date_str.replace("-", "")
    yyyy, mm, dd = date_str[0:4], date_str[4:6], date_str[6:8]
    return dt.date(int(yyyy, base=10), int(mm, base=10), int(dd, base=10))


assert _parse_date("2024-09-30") == dt.date(2024, 9, 30)


async def fetch_api_messages(old_messages: dict[int, dict]) -> dict[int, dict]:
    # Getting information about yourself
    me = await client.get_me()

    # print("client id/username:", me.id, me.username, me.phone)

    if len(old_messages) == 0:
        min_id = 0
    else:
        lookback = 20      # so we update the fwd and rct fields
        min_id = max(map(int, old_messages.keys())) - lookback

    limit = 100

    new_messages = copy.deepcopy(old_messages)

    fpaths = reversed(sorted(IMAGES_DIR.rglob("*.jpg")))

    # Used to prevent duplicate uploads.
    # files must have the same digest and have a date,
    #   within 3 days of each other
    digest_paths = {}
    for fpath in fpaths:
        if fpath.name == "thumbnails.jpg":
            continue

        date = _parse_date(fpath.name)

        with fpath.open(mode='rb') as fobj:
            data = fobj.read()
            digest = digest_img(data)
            if digest in digest_paths:
                for old_date, old_fname in digest_paths[digest]:
                    if abs((old_date - date).total_seconds()) < 3 * 24 * 3600:
                        errmsg = " ".join([
                            f"digest: {digest}",
                            f"old_path: {digest_paths[digest]}",
                            f"new_path : {fpath}",
                        ])
                        raise Exception(errmsg)
                digest_paths[digest].append((date, fpath.name))
            else:
                digest_paths[digest] = [(date, fpath.name)]

        if len(digest_paths) > limit * 10:
            break

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

        if msg.id in old_messages:
            digest = old_messages[msg.id]['dig']
            tgt_fname = old_messages[msg.id]['name']

            if msg.reactions:
                new_messages[msg.id].update({
                    'tfwd' : msg.forwards,
                    'trct' : sum(res.count for res in msg.reactions.results),
                })
            print("old         :", msg.id, digest, tgt_fname)
            continue

        blob = await client.download_media(msg, bytes)
        digest = digest_img(blob)

        fname_prefix = msg.date.isoformat().replace(":", "")[:17]
        tgt_fname = fname_prefix + "_" + str(msg.id) + "_" + digest + ".jpg"
        cur_date = _parse_date(fname_prefix)

        if msg.reactions:
            reactions = sum(res.count for res in msg.reactions.results)
        else:
            reactions = 0

        # TODO (mb 2024-07-31): views/comments ?
        new_messages[msg.id] = {
            'name': tgt_fname,
            'tfwd': int(msg.forwards),
            'trct': reactions,
            'dig' : digest,
        }

        # see if we can find an existing image that matches the digest
        if digest in digest_paths:
            for old_date, tgt_fname in digest_paths[digest]:
                if abs((old_date - cur_date).total_seconds()) < 3 * 24 * 3600:
                    new_messages[msg.id]['name'] = tgt_fname
                    print("dup detected:", msg.id, digest, fname_prefix, tgt_fname)
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

    return new_messages


def main(args: list[str]) -> int:
    old_messages = load_last_messages()

    with client:
        _fetch_cor = fetch_api_messages(old_messages)
        new_messages = client.loop.run_until_complete(_fetch_cor)

    if old_messages != new_messages:
        dump_messages(new_messages)

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))


