#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow>=11.1.0"]
# ///

import argparse
import os
import re
import sys
import json
import pathlib as pl
import collections
import datetime as dt
from urllib.parse import quote
from PIL import Image

if __package__:
    from . import classify_images as classifier
    from .export_classifications import export_index
    from .generate_thumbnails import ROOT_DIR, update_thumbnails
else:
    import classify_images as classifier
    from export_classifications import export_index
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


def update_classifications(archive_repo_dir: pl.Path, www_repo_dir: pl.Path = ROOT_DIR,
                           *, concurrency: int = 4) -> int:
    """Classify pending local originals and export website indexes before publishing."""
    output = www_repo_dir / 'images/classifications.jsonl'
    completed = classifier.completed_urls(output, classifier.DEFAULT_MODEL,
                                          classifier.DEFAULT_REASONING_EFFORT)
    archive_images = archive_repo_dir / 'images'
    pending = {}
    for path in sorted(archive_images.glob('*/*/*.jpg')):
        if path.name == 'thumbnails.jpg' or path.name.startswith('thumbnails-'):
            continue
        relative = path.relative_to(archive_images)
        url = f'{classifier.archive_host(int(relative.parts[0]))}/images/{quote(relative.as_posix())}'
        if url not in completed:
            pending[url] = path

    succeeded = failed = 0
    if pending:
        api_key = os.environ.get('OPENAI_API_KEY', '').strip()
        if not api_key:
            raise classifier.APIConfigurationError('set OPENAI_API_KEY before ingesting unclassified images')
        print(f'Classifying {len(pending)} pending archive originals', flush=True)

        def classify_one(url):
            return classifier.classify(
                url, classifier.DEFAULT_MODEL, api_key,
                reasoning_effort=classifier.DEFAULT_REASONING_EFFORT,
                image_path=pending[url], timeout=90, retries=3,
            )

        output.parent.mkdir(parents=True, exist_ok=True)
        results = classifier.classify_many(pending, classify_one, concurrency)
        try:
            with output.open('a', encoding='utf-8') as file:
                # A valid last record may have no trailing newline.
                if output.stat().st_size:
                    with output.open('rb') as tail:
                        tail.seek(-1, os.SEEK_END)
                        if tail.read(1) != b'\n':
                            file.write('\n')
                for url, record, error in results:
                    if error is not None:
                        failed += 1
                        print(f'ERROR [{url}]: {error}', file=sys.stderr)
                        continue
                    file.write(json.dumps(record, ensure_ascii=False) + '\n')
                    file.flush()
                    succeeded += 1
                    print(f'Classified [{succeeded}/{len(pending)}] {url}', flush=True)
        finally:
            results.close()
            # Successful records are reusable even when another image fails.
            export_index(output, www_repo_dir / 'images/classification_index.json',
                         www_repo_dir / 'images/classification_text_index.json')
        if failed:
            raise classifier.ClassificationError(f'{failed} image classifications failed; publishing aborted')
    else:
        export_index(output, www_repo_dir / 'images/classification_index.json',
                     www_repo_dir / 'images/classification_text_index.json')
    return succeeded


def ingest_archive(archive_repo_dir: pl.Path, www_repo_dir: pl.Path = ROOT_DIR) -> None:
    update_indexes(archive_repo_dir)
    update_thumbnails(archive_repo_dir, www_repo_dir)
    update_classifications(archive_repo_dir, www_repo_dir)


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
    parser = argparse.ArgumentParser(description="Update archive indexes, generate website sprites and classify pending originals.")
    parser.add_argument("archive_repo", type=pl.Path)
    parser.add_argument("--www-repo", type=pl.Path, default=ROOT_DIR)
    options = parser.parse_args(args)
    try:
        ingest_archive(options.archive_repo.resolve(), options.www_repo.resolve())
    except (classifier.ClassificationError, OSError, ValueError) as exc:
        print(f'Ingest failed: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted; completed classifications are saved. Rerun to resume.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
