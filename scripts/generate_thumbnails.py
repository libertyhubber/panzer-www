#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow>=11.1.0"]
# ///
"""Generate local gallery sprites from archive originals, without storing originals."""

import argparse
import io
import json
import math
import pathlib as pl
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

from PIL import Image, ImageOps

ROOT_DIR = pl.Path(__file__).resolve().parent.parent
THUMBNAIL_SIZE = 220
IMAGES_PER_SHEET = 20
SHEET_COLUMNS = 5
THUMBNAIL_PADDING = 2
WEBP_QUALITY = 55
WEBP_METHOD = 6
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024


def sheet_name(index: int) -> str:
    return f"thumbnails-{index:02d}.webp"


def tile_position(index: int) -> tuple[int, int]:
    local_index = index % IMAGES_PER_SHEET
    stride = THUMBNAIL_SIZE + THUMBNAIL_PADDING
    return (local_index % SHEET_COLUMNS * stride, local_index // SHEET_COLUMNS * stride)


def archive_host(year: int) -> str:
    # Same mapping as IMG_HOSTS in templates/index.html.
    if year < 2021 or year > 2033:
        raise ValueError(f"no archive host configured for year {year}")
    return f"https://archiv{max(0, year - 2024)}.derrosarotepanzer.com"


def month_arg(value: str) -> str:
    if not re.fullmatch(r"\d{4}/(0[1-9]|1[0-2])", value):
        raise argparse.ArgumentTypeError("month must be YYYY/MM")
    return value


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def download(url: str, *, timeout: int, retries: int) -> bytes:
    for attempt in range(retries + 1):
        try:
            with urlopen(url, timeout=timeout) as response:
                data = response.read(MAX_DOWNLOAD_BYTES + 1)
                if len(data) > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"download exceeds {MAX_DOWNLOAD_BYTES} bytes: {url}")
                return data
        except HTTPError as exc:
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == retries:
                raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt == retries:
                raise
        time.sleep(min(2 ** attempt, 30))
    raise AssertionError("unreachable")


def gallery_entries(entries: list[dict]) -> list[dict]:
    """Keep source order: sheet assignment is implicit in this exact index order."""
    if not isinstance(entries, list):
        raise ValueError("entry index must be a list")
    result = []
    seen = set()
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError(f"invalid image filename: {name!r}")
        if name in {"thumbnails.jpg", "thumbnails.webp"} or re.fullmatch(r"thumbnails-\d+(?:-q\d+)?\.(?:jpg|webp)", name):
            continue
        if name in seen:
            raise ValueError(f"duplicate image filename: {name}")
        seen.add(name)
        if any(type(entry.get(key)) is not int or entry[key] <= 0 for key in ("w", "h")):
            raise ValueError(f"invalid image dimensions: {name}")
        result.append({"name": name, "w": entry["w"], "h": entry["h"]})
    return result


def make_tile(source) -> Image.Image:
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.LANCZOS)
        tile = Image.new("RGB", (THUMBNAIL_SIZE, THUMBNAIL_SIZE), "black")
        tile.paste(image, ((THUMBNAIL_SIZE - image.width) // 2, (THUMBNAIL_SIZE - image.height) // 2))
        return tile


def save_sheet(sheet: Image.Image, path: pl.Path) -> None:
    sheet.save(path, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD, lossless=False)


def convert_existing_sheets(output_dir: pl.Path, *, workers: int = 6) -> int:
    """Convert local JPEG sheets atomically, removing each JPEG only after success."""
    paths = sorted(path for path in output_dir.glob("*/*/thumbnails-*.jpg")
                   if re.fullmatch(r"thumbnails-\d+\.jpg", path.name))

    def convert(path):
        output = path.with_suffix(".webp")
        with tempfile.TemporaryDirectory(prefix=".thumbnails-", dir=path.parent) as temporary:
            staged = pl.Path(temporary) / output.name
            with Image.open(path) as sheet:
                save_sheet(sheet.convert("RGB"), staged)
            staged.replace(output)
        path.unlink()
        return output

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for output in pool.map(convert, paths):
            print(f"converted {output}", flush=True)
    return len(paths)


def generate_month(entries: list[dict], output_dir: pl.Path, open_image, *, workers: int = 6, force: bool = False) -> int:
    """Regenerate changed batches only; publish the matching index after all succeed."""
    entries = gallery_entries(entries)
    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / "entry_index.json"
    old_entries = json.loads(index_path.read_bytes()) if index_path.exists() else []
    index_data = (json.dumps(entries, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    generated = 0
    expected = set()

    def load_tile(entry):
        try:
            return make_tile(open_image(entry["name"]))
        except Exception as exc:
            raise RuntimeError(f"could not generate thumbnail for {entry['name']}: {exc}") from exc

    # Stage the month first so a failed download doesn't publish a mismatched index.
    with tempfile.TemporaryDirectory(prefix=".thumbnails-", dir=output_dir) as temporary, ThreadPoolExecutor(max_workers=workers) as pool:
        staging_dir = pl.Path(temporary)
        for start in range(0, len(entries), IMAGES_PER_SHEET):
            name = sheet_name(start // IMAGES_PER_SHEET)
            expected.add(name)
            batch = entries[start:start + IMAGES_PER_SHEET]
            if not force and old_entries[start:start + IMAGES_PER_SHEET] == batch and (output_dir / name).exists():
                continue
            rows = math.ceil(len(batch) / SHEET_COLUMNS)
            width = SHEET_COLUMNS * THUMBNAIL_SIZE + (SHEET_COLUMNS - 1) * THUMBNAIL_PADDING
            height = rows * THUMBNAIL_SIZE + (rows - 1) * THUMBNAIL_PADDING
            sheet = Image.new("RGB", (width, height), "black")
            for index, tile in enumerate(pool.map(load_tile, batch)):
                sheet.paste(tile, tile_position(index))
            save_sheet(sheet, staging_dir / name)
            generated += 1
            print(f"generated {output_dir / name} ({len(batch)} images)", flush=True)

        for staged_path in staging_dir.iterdir():
            staged_path.replace(output_dir / staged_path.name)
        if not index_path.exists() or index_path.read_bytes() != index_data:
            staged_index = staging_dir / "entry_index.json"
            staged_index.write_bytes(index_data)
            staged_index.replace(index_path)
        for old_sheet in output_dir.glob("thumbnails-*"):
            if re.fullmatch(r"thumbnails-\d+\.(?:jpg|webp)", old_sheet.name) and old_sheet.name not in expected:
                old_sheet.unlink()
    return generated


def update_thumbnails(archive_repo_dir: pl.Path, www_repo_dir: pl.Path = ROOT_DIR, *, force: bool = False) -> int:
    """Telegram ingest: read originals from the archive checkout, write only locally."""
    archive_images = archive_repo_dir / "images"
    if not archive_images.is_dir():
        raise ValueError(f"archive images directory does not exist: {archive_images}")
    generated = 0
    for index_path in sorted(archive_images.glob("*/*/entry_index.json")):
        month_dir = index_path.parent
        generated += generate_month(
            json.loads(index_path.read_bytes()),
            www_repo_dir / "images" / month_dir.relative_to(archive_images),
            lambda name, directory=month_dir: directory / name,
            force=force,
        )
    return generated


def regenerate_from_archive(dir_index: pl.Path, output_dir: pl.Path, months: list[str], *, workers: int, timeout: int, retries: int, force: bool) -> int:
    catalog = json.loads(dir_index.read_bytes())
    if not isinstance(catalog, dict) or any(not re.fullmatch(r"\d{4}/(0[1-9]|1[0-2])", key) for key in catalog):
        raise ValueError(f"invalid directory index: {dir_index}")
    missing = set(months) - catalog.keys()
    if missing:
        raise ValueError(f"months not in directory index: {', '.join(sorted(missing))}")
    counts = dict(catalog)
    generated = 0
    for month in sorted(set(months) if months else catalog):
        prefix = f"{archive_host(int(month[:4]))}/images/{month}/"
        entries = gallery_entries(json.loads(download(prefix + "entry_index.json", timeout=timeout, retries=retries)))
        generated += generate_month(
            entries, output_dir / month,
            lambda name, prefix=prefix: io.BytesIO(download(prefix + quote(name, safe=""), timeout=timeout, retries=retries)),
            workers=workers, force=force,
        )
        counts[month] = len(entries)
        # A completed month is resumable even if a later month fails.
        if counts != catalog:
            temporary = dir_index.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(counts, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            temporary.replace(dir_index)
            catalog = dict(counts)
    return generated


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir-index", type=pl.Path, default=ROOT_DIR / "images/dir_index.json")
    parser.add_argument("--output-dir", type=pl.Path, default=ROOT_DIR / "images")
    parser.add_argument("--month", type=month_arg, action="append", default=[], help="YYYY/MM (repeatable; default: all)")
    parser.add_argument("--workers", type=positive_int, default=6, help="parallel image downloads (default: 6)")
    parser.add_argument("--timeout", type=positive_int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--force", action="store_true", help="regenerate unchanged sheets too")
    parser.add_argument("--convert-existing", action="store_true", help="convert local JPEG sheets to WebP without archive downloads; remove converted JPEGs")
    options = parser.parse_args(args)
    if options.retries < 0:
        parser.error("--retries cannot be negative")
    try:
        if options.convert_existing:
            if options.month or options.force:
                parser.error("--convert-existing cannot be combined with --month or --force")
            generated = convert_existing_sheets(options.output_dir, workers=options.workers)
        else:
            generated = regenerate_from_archive(options.dir_index, options.output_dir, options.month,
                workers=options.workers, timeout=options.timeout, retries=options.retries, force=options.force)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"thumbnail generation failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; completed months are saved. Run again to resume.", file=sys.stderr)
        return 130
    print(f"Generated {generated} local sprite sheets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
