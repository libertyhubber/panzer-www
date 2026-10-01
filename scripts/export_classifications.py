#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Export append-only classifier results as a compact browser-facing index."""

import argparse
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

ROOT_DIR = Path(__file__).resolve().parents[1]
TAG_BLACKLIST = {"memes", "meme", "symbolbild", "bildunterschrift", "ausdruck", "gesichtsausdruck", "reaktionsbild", "bildmakro", "text-meme"}
TAG_ALIASES = {"zitatgrafik": "zitat", "zitat-meme": "zitat"}


def build_index(source: Path) -> dict:
    index = {}
    with source.open(encoding="utf-8") as file:
        for number, line in enumerate(file, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                path = unquote(urlsplit(record["url"]).path)
                match = re.fullmatch(r"/images/(\d{4}/(?:0[1-9]|1[0-2])/[^/]+)", path)
                # Explicit-URL results outside the archive don't belong in the gallery.
                if not match:
                    continue
                result = record["classification"]
                tags = result.get("tags", [])
                template = result["meme_template"]
                if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                    raise ValueError("invalid tags")
                if template["status"] not in {"recognized", "unknown", "none"}:
                    raise ValueError("invalid template status")
                name = template["name"] if template["status"] == "recognized" else None
                if name is not None and not isinstance(name, str):
                    raise ValueError("invalid template name")
                if any(not isinstance(result[field], str) for field in ("text", "description")):
                    raise ValueError("invalid text or description")
                # The last appended result replaces earlier classifications of this image.
                index[match[1]] = {
                    "tags": list(dict.fromkeys(
                        TAG_ALIASES.get(tag.strip().casefold(), tag)
                        for tag in tags if tag.strip().casefold() not in TAG_BLACKLIST
                    )),
                    "template": name.strip().casefold() if name is not None else None,
                    "template_status": template["status"],
                    "text": result["text"],
                    "description": result["description"],
                }
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ValueError(f"Invalid classification in {source}:{number}: {exc}") from exc
    return index


def export_index(source: Path, output: Path, text_output: Path | None = None) -> None:
    text_output = text_output or output.with_name("classification_text_index.json")
    # Preserve shipped indexes without raw results; migrate the old combined index.
    if not source.exists() and output.exists():
        index = json.loads(output.read_text(encoding="utf-8"))
        if not any(isinstance(entry, dict) and "text" in entry for entry in index.values()):
            return
    else:
        index = build_index(source) if source.exists() else {}
    compact = {key: {field: entry[field] for field in ("tags", "template")}
               for key, entry in index.items()}
    text = {key: {field: entry[field] for field in ("text", "description")}
            for key, entry in index.items()}
    # Write text first so migration never discards the only copy of it.
    write_index(text_output, text)
    write_index(output, compact)


def write_index(output: Path, index: dict) -> None:
    data = json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if output.exists() and output.read_text(encoding="utf-8") == data:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(data, encoding="utf-8")
    temporary.replace(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT_DIR / "images/classifications.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT_DIR / "images/classification_index.json")
    parser.add_argument("--text-output", type=Path, help="Text index path (defaults beside --output)")
    args = parser.parse_args()
    export_index(args.input, args.output, args.text_output)


if __name__ == "__main__":
    main()
