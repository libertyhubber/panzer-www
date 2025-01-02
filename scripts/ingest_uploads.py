#!/usr/bin/env python3

import os
import io
import re
import sys
import json
import hashlib as hl
import pathlib as pl
import itertools as it
import subprocess as sp
import collections
import datetime as dt
from PIL import Image


DEBUG_ENTRY_INDEX = True


THUMBNAIL_SIZE = 150


def update_thumbnails():
    for entry_index_path in sorted(pl.Path("images").glob("*/*/entry_index.json")):
        dirpath = entry_index_path.parent
        thumbnails_path = dirpath / "thumbnails.jpg"
        is_thumbnails_fresh = (
            thumbnails_path.exists()
            and thumbnails_path.stat().st_mtime >= entry_index_path.stat().st_mtime
        )
        if is_thumbnails_fresh:
            continue

        with entry_index_path.open('rb') as fobj:
            entry_index = json.loads(fobj.read().decode("utf-8"))

        print("updating thumbnails", thumbnails_path)
        num_cols = 10
        num_rows = len(entry_index) // num_cols

        padding_x = num_cols * 2
        padding_y = num_rows * 2

        thumbnails_width = THUMBNAIL_SIZE * num_cols + padding_x
        thumbnails_height = THUMBNAIL_SIZE * (num_rows + 1) + padding_y

        thumbnails_image = Image.new('RGB', (thumbnails_width, thumbnails_height))
        for i, entry in enumerate(entry_index):
            img_path = dirpath / entry['name']
            with Image.open(img_path) as img:
                img.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE))
                thumb_width, thumb_height = img.size

                if entry['w'] > entry['h']:
                    offset_x = 0
                    offset_y = (THUMBNAIL_SIZE - thumb_height) // 2
                else:
                    offset_x = (THUMBNAIL_SIZE - thumb_width) // 2
                    offset_y = 0

                thumbnails_image.paste(img.copy(), (offset_x + entry['x'], offset_y + entry['y']))

        thumbnails_image.save(str(thumbnails_path), "JPEG", quality=75, optimize=True, progressive=True)


def update_indexes():
    img_by_dir = collections.defaultdict(list)
    for fpath in sorted(pl.Path("images").glob("**/*.jpg")):
        if fpath.name == "thumbnails.jpg":
            continue

        dirpath = str(fpath.parent)[len("images/"):]
        img_by_dir[dirpath].append(fpath)

    dir_index_dicts = {
        dirpath: len(img_paths)
        for dirpath, img_paths in img_by_dir.items()
    }

    dir_index_data = json.dumps(dir_index_dicts, indent=2).encode("utf-8")
    dir_index_path = pl.Path("images") / "dir_index.json"

    is_dir_index_fresh = (
        dir_index_path.exists()
        and dir_index_path.open('rb').read() == dir_index_data
    )
    if not is_dir_index_fresh:
        with dir_index_path.open('wb') as fobj:
            fobj.write(dir_index_data)

    for dirname, img_paths in img_by_dir.items():
        dirpath = pl.Path("images").joinpath(*dirname.split("/"))
        entry_index_path = dirpath / "entry_index.json"

        if entry_index_path.exists():
            with entry_index_path.open('rb') as fobj:
                old_entry_index_data = fobj.read()
                old_entry_index = json.loads(old_entry_index_data.decode("utf-8"))
        else:
            old_entry_index_data = None
            old_entry_index = []

        new_entry_index = []
        old_entries = {entry['name']: entry for entry in old_entry_index}

        for i, img_path in enumerate(reversed(img_paths)):
            column = i % 10
            row = i // 10
            padding_x = column * 2
            padding_y = row * 2
            offset_x = padding_x + THUMBNAIL_SIZE * column
            offset_y = padding_y + THUMBNAIL_SIZE * row

            if not DEBUG_ENTRY_INDEX and img_path.name in old_entries:
                new_entry_index.append(old_entries[img_path.name])
            else:
                with Image.open(img_path) as img:
                    img_width, img_height = img.size

                new_entry_index.append({
                    'x': offset_x,
                    'y': offset_y,
                    'w': img_width,
                    'h': img_height,
                    'name': img_path.name,
                })

        new_entry_index.sort(key=lambda e: e['name'])
        new_entry_index_data = (
            json.dumps(new_entry_index)
                .replace("}, {", "},\n{")
                .encode("utf-8")
        )
        if old_entry_index_data != new_entry_index_data:
            print("updating index   ", entry_index_path)
            with entry_index_path.open('wb') as fobj:
                fobj.write(new_entry_index_data)


def mk_datestr(datestr=None):
    if datestr is None:
        datestr = dt.datetime.now().isoformat()
        return datestr.replace("-", "").replace(":", "")[:15]

    datestr = datestr.replace("-", "").replace(":", "")[:15]
    if not datestr[0:4].isdigit() or not datestr[4:6].isdigit():
        datestr = dt.datetime.now().isoformat()
        datestr = datestr.replace("-", "").replace(":", "")[:15]

    return datestr



def ingest_uploads():
    extensions = ("jpg", "jpeg", "png", "webp")
    directories = (".", "images", "upload")

    upload_paths = []

    for directory in directories:
        for fpath in pl.Path(directory).glob("*.*"):
            basename, suffix = fpath.name.rsplit(".", 1)
            if basename.startswith("favico"):
                continue
            if suffix.lower() in extensions:
                upload_paths.append(fpath)

    for src_fpath in upload_paths:
        basename, suffix = src_fpath.name.rsplit(".", 1)

        if match := re.search(r"20[0-9]{2}-[0-1][0-9]-[0-3][0-9]", src_fpath.name):
            datestr = mk_datestr(src_fpath.name[:10])
            tgt_fname = src_fpath.name
        else:
            datestr = mk_datestr()

            with src_fpath.open(mode="rb") as fobj:
                digest = hl.sha256(fobj.read()).hexdigest()[:15]
            tgt_fname = datestr + "_" + digest + src_fpath.name

        year = datestr[0:4]
        month = datestr[4:6]

        dirpath = pl.Path("images") / year / month
        dirpath.mkdir(parents=True, exist_ok=True)

        tgt_fname = tgt_fname.rsplit(".", 1)[0] + ".jpg"
        tgt_fpath = dirpath / tgt_fname

        print(src_fpath, "->", tgt_fpath, src_fpath.stat().st_mtime)

        if suffix.lower() in ("png", "webp"):
            with Image.open(src_fpath) as png_img:
                jpg_img = Image.new('RGB', png_img.size)
                jpg_img.paste(png_img.copy())
                jpg_img.save(tgt_fpath, "JPEG", quality=95, optimize=True, progressive=True)
            src_fpath.unlink()
        else:
            src_fpath.rename(tgt_fpath)

        sp.call(["git", "add", str(tgt_fpath.absolute())])
        sp.call(["git", "rm", str(src_fpath.absolute())])


def main(args: list[str]) -> int:
    ingest_uploads()
    update_indexes()
    update_thumbnails()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
