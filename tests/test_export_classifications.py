import json
from pathlib import Path
import tempfile
import unittest

from scripts.export_classifications import build_index, export_index


def record(name="example.jpg", **changes):
    result = {
        "url": f"https://archiv0.derrosarotepanzer.com/images/2024/01/{name}",
        "usage": {"total_tokens": 123},
        "classification": {
            "tags": ["Bert", "Sesamstraße"],
            "text": "OCR text",
            "description": "German description",
            "meme_template": {"status": "recognized", "name": "Example template", "confidence": 0.9},
        },
    }
    result.update(changes)
    return result


class ExportClassificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "classifications.jsonl"
        self.output = Path(self.directory.name) / "classification_index.json"

    def write_records(self, *records):
        self.source.write_text("\n".join(json.dumps(value) for value in records) + "\n", encoding="utf-8")

    def test_compact_index_uses_paths_and_latest_appended_record(self):
        latest = record()
        latest["classification"]["tags"] = ["new tag"]
        self.write_records(record(), latest, record(url="https://example.com/nonarchive.jpg"))
        export_index(self.source, self.output)
        self.assertEqual(json.loads(self.output.read_text()), {
            "2024/01/example.jpg": {
                "tags": ["new tag"], "template": "example template",
            },
        })
        text_output = self.output.with_name("classification_text_index.json")
        self.assertEqual(json.loads(text_output.read_text()), {
            "2024/01/example.jpg": {"text": "OCR text", "description": "German description"},
        })
        text_mtime = text_output.stat().st_mtime_ns
        previous_mtime = self.output.stat().st_mtime_ns
        export_index(self.source, self.output)
        self.assertEqual(self.output.stat().st_mtime_ns, previous_mtime)
        self.assertEqual(text_output.stat().st_mtime_ns, text_mtime)

    def test_blacklisted_tags_are_removed_case_insensitively(self):
        value = record()
        value["classification"]["tags"] = [
            "memes", "Meme", " AUSDRUCK ", "Gesichtsausdruck", "text-meme", " TEXT-MEME ",
            "Bert", "politisches Meme",
        ]
        self.write_records(value)
        entry = build_index(self.source)["2024/01/example.jpg"]
        self.assertEqual(entry["tags"], ["Bert", "politisches Meme"])
        # Keep the raw classifier results intact.
        self.assertEqual(json.loads(self.source.read_text()), value)

    def test_quote_tags_are_converted_case_insensitively_and_deduplicated(self):
        value = record()
        value["classification"]["tags"] = ["Zitatgrafik", " ZITAT-MEME ", "zitat", "Bert"]
        self.write_records(value)
        entry = build_index(self.source)["2024/01/example.jpg"]
        self.assertEqual(entry["tags"], ["zitat", "Bert"])
        self.assertEqual(json.loads(self.source.read_text()), value)

    def test_decodes_paths_and_keeps_months_distinct(self):
        self.write_records(record("example%20image.jpg"), record(url="https://archiv0.derrosarotepanzer.com/images/2024/02/example%20image.jpg"))
        self.assertEqual(set(build_index(self.source)), {"2024/01/example image.jpg", "2024/02/example image.jpg"})

    def test_template_names_are_trimmed_and_casefolded(self):
        records = []
        for i, name in enumerate(("Expanding Brain", " expanding brain ", "EXPANDING BRAIN", "Straße", "STRASSE")):
            value = record(f"{i}.jpg")
            value["classification"]["meme_template"]["name"] = name
            records.append(value)
        self.write_records(*records)
        export_index(self.source, self.output)
        index = json.loads(self.output.read_text())
        self.assertEqual([entry["template"] for entry in index.values()], [
            "expanding brain", "expanding brain", "expanding brain", "strasse", "strasse",
        ])
        self.assertEqual([json.loads(line) for line in self.source.read_text().splitlines()], records)

    def test_unknown_and_none_templates_and_legacy_tags(self):
        for status in ("unknown", "none"):
            with self.subTest(status=status):
                value = record()
                value["classification"]["meme_template"] = {"status": status, "name": None}
                del value["classification"]["tags"]
                self.write_records(value)
                entry = build_index(self.source)["2024/01/example.jpg"]
                self.assertEqual(entry["tags"], [])
                self.assertIsNone(entry["template"])
                self.assertEqual(entry["template_status"], status)

    def test_missing_input_creates_empty_index_or_preserves_existing(self):
        export_index(self.source, self.output)
        self.assertEqual(json.loads(self.output.read_text()), {})
        self.output.write_text('{"already": "exported"}')
        export_index(self.source, self.output)
        self.assertEqual(json.loads(self.output.read_text()), {"already": "exported"})

    def test_migrates_combined_index_without_raw_input(self):
        self.write_records(record())
        self.output.write_text(json.dumps(build_index(self.source)))
        self.source.unlink()
        export_index(self.source, self.output)
        self.assertEqual(json.loads(self.output.read_text()), {
            "2024/01/example.jpg": {"tags": ["Bert", "Sesamstraße"], "template": "example template"},
        })
        text_output = self.output.with_name("classification_text_index.json")
        expected = {"2024/01/example.jpg": {"text": "OCR text", "description": "German description"}}
        self.assertEqual(json.loads(text_output.read_text()), expected)
        export_index(self.source, self.output)
        self.assertEqual(json.loads(text_output.read_text()), expected)

    def test_invalid_input_leaves_previous_export_intact(self):
        self.output.write_text("{}\n")
        self.source.write_text('{"partial":')
        with self.assertRaisesRegex(ValueError, "classifications.jsonl:1"):
            export_index(self.source, self.output)
        self.assertEqual(self.output.read_text(), "{}\n")


if __name__ == "__main__":
    unittest.main()
