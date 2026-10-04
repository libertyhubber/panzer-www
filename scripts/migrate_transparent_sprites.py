#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow>=11.1.0"]
# ///
"""Make existing WebP sprite padding transparent, without downloads or RGB re-encoding."""

import argparse
import io
import json
import os
import pathlib as pl
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageChops

if __package__:
    from . import generate_thumbnails as thumbs
else:
    import generate_thumbnails as thumbs

SHEET_PATTERN = re.compile(r"thumbnails-(\d+)(?:-q\d+)?\.webp")


def riff_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Read a complete WebP RIFF container, including odd-length chunk padding."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a WebP RIFF container")
    if int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise ValueError("invalid WebP RIFF length")
    chunks = []
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("truncated WebP chunk header")
        kind = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        start = offset + 8
        end = start + size
        offset = end + size % 2
        if offset > len(data):
            raise ValueError("truncated WebP chunk")
        chunks.append((kind, data[start:end]))
    return chunks


def pack_webp(chunks: list[tuple[bytes, bytes]]) -> bytes:
    body = b"WEBP" + b"".join(kind + len(payload).to_bytes(4, "little") + payload +
                             (b"\0" if len(payload) % 2 else b"") for kind, payload in chunks)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def with_alpha(data: bytes, alpha: Image.Image) -> bytes:
    """Mux a lossless ALPH channel into an unchanged lossy VP8 image payload."""
    chunks = riff_chunks(data)
    kinds = [kind for kind, _ in chunks]
    if kinds.count(b"VP8 ") != 1 or any(kind in kinds for kind in (b"VP8L", b"ANIM", b"ANMF")):
        raise ValueError("migration requires a static lossy VP8 WebP sheet")
    if kinds.count(b"VP8X") > 1 or kinds.count(b"ALPH") > 1:
        raise ValueError("duplicate WebP extended header or alpha channel")
    flags = 0
    for kind, payload in chunks:
        if kind == b"VP8X":
            if len(payload) != 10 or payload[0] & 0x02:
                raise ValueError("invalid or animated WebP extended header")
            flags = payload[0]
    # Encode ONLY a dummy black image to obtain libwebp's compressed lossless
    # alpha channel. Its RGB payload is discarded, never used for the sheet.
    dummy = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    dummy.putalpha(alpha)
    encoded = io.BytesIO()
    dummy.save(encoded, "WEBP", lossless=False, quality=0, alpha_quality=100, method=6)
    alpha_chunks = [payload for kind, payload in riff_chunks(encoded.getvalue()) if kind == b"ALPH"]
    if len(alpha_chunks) != 1:
        raise ValueError("encoder did not produce a separate WebP alpha channel")
    width, height = alpha.size
    extended = bytes([flags | 0x10, 0, 0, 0]) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    result = [(b"VP8X", extended)]
    for kind, payload in chunks:
        if kind in {b"VP8X", b"ALPH"}:
            continue
        if kind == b"VP8 ":
            result.append((b"ALPH", alpha_chunks[0]))
        result.append((kind, payload))
    return pack_webp(result)


def image_mask(entries: list[dict]) -> Image.Image:
    """Opaque only inside fitted image bounds; padding/gutters/empty cells are clear."""
    mask = Image.new("L", thumbs.sheet_dimensions(len(entries)), 0)
    for index, entry in enumerate(entries):
        x, y = thumbs.tile_position(index)
        left, top, right, bottom = thumbs.tile_bounds(entry["w"], entry["h"])
        mask.paste(255, (x + left, y + top, x + right, y + bottom))
    return mask


def migrate_sheet(path: pl.Path, entries: list[dict], *, dry_run: bool = False) -> bool:
    """Replace one sheet atomically after verifying its alpha; repeat runs are no-ops."""
    mask = image_mask(entries)
    data = path.read_bytes()
    with Image.open(io.BytesIO(data)) as original:
        if original.format != "WEBP" or original.size != mask.size:
            raise ValueError(f"{path}: dimensions/format do not match its index (expected {mask.size})")
        current_alpha = original.convert("RGBA").getchannel("A")
    # Also preserve genuine source-image transparency inside the fitted bounds.
    alpha = ImageChops.multiply(current_alpha, mask)
    if ImageChops.difference(current_alpha, alpha).getbbox() is None:
        return False
    if dry_run:
        return True
    migrated = with_alpha(data, alpha)
    with tempfile.TemporaryDirectory(prefix=".transparent-sprite-", dir=path.parent) as temporary:
        staged = pl.Path(temporary) / path.name
        staged.write_bytes(migrated)
        with Image.open(staged) as result:
            if result.size != mask.size or ImageChops.difference(result.getchannel("A"), alpha).getbbox() is not None:
                raise ValueError(f"{path}: encoded alpha does not match the requested mask")
        os.chmod(staged, path.stat().st_mode & 0o777)
        staged.replace(path)
    return True


def migration_jobs(output_dir: pl.Path, months: list[str]) -> list[tuple[pl.Path, list[dict]]]:
    """Preflight indexes and required sheets before modifying anything."""
    indexes = {str(path.parent.relative_to(output_dir)): path
               for path in output_dir.glob("*/*/entry_index.json")}
    missing = set(months) - indexes.keys()
    if missing:
        raise ValueError(f"months without local indexes: {', '.join(sorted(missing))}")
    jobs = []
    for month in sorted(set(months) if months else indexes):
        path = indexes[month]
        entries = thumbs.gallery_entries(json.loads(path.read_bytes()))
        for start in range(0, len(entries), thumbs.IMAGES_PER_SHEET):
            required = path.parent / thumbs.sheet_name(start // thumbs.IMAGES_PER_SHEET)
            if not required.is_file():
                raise ValueError(f"missing local sheet: {required}")
        # Include local -qNN comparison sheets too, without changing their RGB quality.
        for sheet in sorted(path.parent.glob("thumbnails-*.webp")):
            match = SHEET_PATTERN.fullmatch(sheet.name)
            if not match:
                continue
            start = int(match[1]) * thumbs.IMAGES_PER_SHEET
            batch = entries[start:start + thumbs.IMAGES_PER_SHEET]
            if not batch:
                raise ValueError(f"sheet has no indexed images: {sheet}")
            jobs.append((sheet, batch))
    return jobs


def migrate(output_dir: pl.Path, months: list[str], *, workers: int = 6, dry_run: bool = False) -> int:
    jobs = migration_jobs(output_dir, months)
    def run(job):
        path, entries = job
        try:
            changed = migrate_sheet(path, entries, dry_run=dry_run)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"could not migrate {path}: {exc}") from exc
        if changed:
            print(f"{'would migrate' if dry_run else 'migrated'} {path}", flush=True)
        return changed
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(run, jobs))


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=pl.Path, default=thumbs.ROOT_DIR / "images")
    parser.add_argument("--month", type=thumbs.month_arg, action="append", default=[], help="YYYY/MM (repeatable; default: all local months)")
    parser.add_argument("--workers", type=thumbs.positive_int, default=6)
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing files")
    options = parser.parse_args(args)
    if not options.output_dir.is_dir():
        parser.error(f"not a directory: {options.output_dir}")
    try:
        count = migrate(options.output_dir, options.month, workers=options.workers, dry_run=options.dry_run)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"sprite migration failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; rerun to skip already migrated sheets.", file=sys.stderr)
        return 130
    print(f"{'Would migrate' if options.dry_run else 'Migrated'} {count} local sprite sheets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
