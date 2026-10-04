import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import panzer_imgsync as sync
from scripts.export_classifications import read_monthly_indexes


class TelegramMetadataTests(unittest.TestCase):
    def test_counts_and_legacy_cache_are_chunked_by_image_month(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'telegram_metadata.json'
            empty_month = output.parent / '2024/03'
            empty_month.mkdir(parents=True)
            (empty_month / 'entry_index.json').write_text('[]')
            messages = {
                1: {'name': '2024-01-01_first.jpg', 'trct': 7, 'tview': 1234, 'tcomments': 0},
                2: {'name': '2024-02-01_legacy.jpg', 'trct': 3},
                3: {'name': '2024-01-01_first.jpg', 'trct': 99},
                4: {'name': None},
            }
            output.write_text('{}')  # Remove the old monolithic export after migration.
            with patch.object(sync, 'GALLERY_METADATA_PATH', output):
                sync.dump_gallery_metadata(messages)
                self.assertFalse(output.exists())
                first = output.parent / '2024/01/telegram_metadata.json'
                self.assertEqual(json.loads(first.read_text()), {'2024-01-01_first.jpg': [1, 7, 1234, 0]})
                self.assertEqual(read_monthly_indexes(output), {
                    '2024-01-01_first.jpg': [1, 7, 1234, 0],
                    '2024-02-01_legacy.jpg': [2, 3, None, None],
                })
                self.assertEqual(json.loads((empty_month / output.name).read_text()), {})
                mtime = first.stat().st_mtime_ns
                sync.dump_gallery_metadata(messages)
                self.assertEqual(first.stat().st_mtime_ns, mtime)
                sync.dump_gallery_metadata({2: messages[2]})
                self.assertEqual(json.loads(first.read_text()), {})


if __name__ == '__main__':
    unittest.main()
