import json
from pathlib import Path
import tempfile
import unittest

from scripts.export_classifications import (
    export_index, export_monthly_indexes, read_monthly_indexes, write_index,
)
from test_export_classifications import record


class MonthlyClassificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / 'classifications.jsonl'
        self.output = self.root / 'classification_index.json'
        self.text = self.root / 'classification_text_index.json'
        empty = self.root / '2024/03'
        empty.mkdir(parents=True)
        (empty / 'entry_index.json').write_text('[]')

    def export(self, *records):
        self.source.write_text('\n'.join(json.dumps(row) for row in records))
        export_index(self.source, self.output)

    def test_chunks_counts_empty_months_latest_record_and_stable_writes(self):
        first = record('same.jpg', tag_format_version=2)
        first['classification'].update(tags=['Bert'], tags_de=['Katze'], tags_en=['cat'])
        second = record(url='https://archive.example/images/2024/02/same.jpg')
        latest = record(url=second['url'])
        latest['classification']['tags'] = ['Bert', 'bert', 'Cat']
        self.export(first, second, latest)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.text.exists())
        compact = read_monthly_indexes(self.output)
        self.assertEqual(set(compact), {'2024/01/same.jpg', '2024/02/same.jpg'})
        self.assertEqual(compact['2024/02/same.jpg']['tags'], ['Bert', 'bert', 'Cat'])
        self.assertNotIn('text', compact['2024/01/same.jpg'])
        self.assertEqual(read_monthly_indexes(self.text)['2024/01/same.jpg'], {
            'text': 'OCR text', 'description': 'German description',
        })
        self.assertEqual(json.loads((self.root / 'classification_catalog.json').read_text()), {
            'tagCounts': {'bert': 2, 'cat': 2},
            'templateCounts': {'example template': 2},
        })
        self.assertEqual(json.loads((self.root / '2024/03' / self.output.name).read_text()), {})
        paths = list(self.root.glob('*/*/*.json')) + [self.root / 'classification_catalog.json']
        before = {path: path.stat().st_mtime_ns for path in paths}
        export_monthly_indexes(self.source, self.output)
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in paths})
        self.export(first)
        self.assertEqual(json.loads((self.root / '2024/02' / self.output.name).read_text()), {})
        self.assertEqual(json.loads((self.root / '2024/02' / self.text.name).read_text()), {})

    def test_migrates_shipped_split_indexes_without_private_input(self):
        compact = {'2024/01/a.jpg': {'tags': ['Bert'], 'template': None}}
        text = {'2024/01/a.jpg': {'text': 'OCR', 'description': 'Description'}}
        write_index(self.output, compact)
        write_index(self.text, text)
        export_monthly_indexes(self.source, self.output)
        self.assertEqual(read_monthly_indexes(self.output), compact)
        self.assertEqual(read_monthly_indexes(self.text), text)
        export_monthly_indexes(self.source, self.output)
        self.assertEqual(read_monthly_indexes(self.text), text)

    def test_migrates_old_combined_index(self):
        combined = {'2024/01/a.jpg': {
            'tags': [], 'template': None, 'template_status': 'none',
            'text': 'OCR', 'description': 'Description',
        }}
        write_index(self.output, combined)
        export_monthly_indexes(self.source, self.output)
        self.assertEqual(read_monthly_indexes(self.output), {
            '2024/01/a.jpg': {'tags': [], 'template': None},
        })
        self.assertEqual(read_monthly_indexes(self.text)['2024/01/a.jpg']['text'], 'OCR')

    def test_invalid_input_leaves_all_previous_chunks_intact(self):
        self.export(record())
        before = {path: path.read_bytes() for path in self.root.rglob('*.json')}
        self.source.write_text('{broken')
        with self.assertRaises(ValueError):
            export_monthly_indexes(self.source, self.output)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*.json')})


if __name__ == '__main__':
    unittest.main()
