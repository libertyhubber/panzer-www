#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow>=11.1.0"]
# ///

import argparse
import re
import sys
import json
import pathlib as pl
import collections
import datetime as dt
from PIL import Image

if __package__:
    from .generate_thumbnails import ROOT_DIR, update_thumbnails
else:
    from generate_thumbnails import ROOT_DIR, update_thumbnails

def update_indexes(archive_repo_dir: pl.Path) -> None:
    archiv_img_dir = archive_repo_dir / "images"
    assert archiv_img_dir.exists(), archiv_img_dir

    img_by_dir = collections.defaultdict(list)
    for fpath in sorted(archiv_img_dir.glob("**/*.jpg")):
        if fpath.name == "thumbnails.jpg" or re.fullmatch(r"thumbnails-\d+\.jpg", fpath.name):
            continue

        yyyy_mm_dirpath = str(fpath.parent).split("images/", maxsplit=1)[-1]
        assert re.match(r"\d{4}/\d{2}", yyyy_mm_dirpath), yyyy_mm_dirpath
        img_by_dir[yyyy_mm_dirpath].append(fpath)

    dir_index_path = archiv_img_dir / "dir_index.json"
    if dir_index_path.exists():
        with dir_index_path.open(mode='rb') as fobj:
            merged_dir_index_dicts = json.load(fobj)
    else:
        merged_dir_index_dicts = {}

    for yyyy_mm_dirpath, img_paths in img_by_dir.items():
        merged_dir_index_dicts[yyyy_mm_dirpath] = len(img_paths)

    merged_dir_index_data = json.dumps(merged_dir_index_dicts, indent=2).encode("utf-8")

    is_dir_index_fresh = (
        dir_index_path.exists()
        and dir_index_path.open(mode='rb').read() == merged_dir_index_data
    )
    if not is_dir_index_fresh:
        with dir_index_path.open(mode='wb') as fobj:
            fobj.write(merged_dir_index_data)

    for yyyy_mm_dirpath, img_paths in img_by_dir.items():
        dirpath = archiv_img_dir.joinpath(*yyyy_mm_dirpath.split("/"))
        entry_index_path = dirpath / "entry_index.json"

        if entry_index_path.exists():
            with entry_index_path.open(mode='rb') as fobj:
                old_entry_index_data = fobj.read()
                old_entry_index = json.loads(old_entry_index_data.decode("utf-8"))
        else:
            old_entry_index_data = None
            old_entry_index = []

        new_entry_index = []
        old_entries = {entry['name']: entry for entry in old_entry_index}

        for img_path in img_paths:
            if img_path.name in old_entries:
                old_entry = old_entries[img_path.name]
                img_width, img_height = old_entry['w'], old_entry['h']
            else:
                with Image.open(img_path) as img:
                    img_width, img_height = img.size

            # Sprite coordinates are derived from the final local index order.
            new_entry_index.append({
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
            with entry_index_path.open(mode='wb') as fobj:
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


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update archive indexes and generate website-local sprite sheets.")
    parser.add_argument("archive_repo", type=pl.Path)
    parser.add_argument("--www-repo", type=pl.Path, default=ROOT_DIR)
    options = parser.parse_args(args)
    update_indexes(options.archive_repo)
    update_thumbnails(options.archive_repo, options.www_repo)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
