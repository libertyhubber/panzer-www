import ast
import json
from pathlib import Path
import tempfile
import unittest


class TelegramMetadataTests(unittest.TestCase):
    def test_counts_and_legacy_cache(self):
        # Load only the exporter to keep this test independent of Telegram
        # and image-processing dependencies.
        source = Path(__file__).resolve().parents[1] / 'scripts' / 'panzer_imgsync.py'
        tree = ast.parse(source.read_text())
        exporter = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == 'dump_gallery_metadata')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'metadata.json'
            namespace = {'json': json, 'GALLERY_METADATA_PATH': output}
            exec(compile(ast.Module(body=[exporter], type_ignores=[]), str(source), 'exec'), namespace)
            namespace['dump_gallery_metadata']({
                1: {'name': 'first.jpg', 'trct': 7, 'tview': 1234, 'tcomments': 0},
                2: {'name': 'legacy.jpg', 'trct': 3},
                3: {'name': 'first.jpg', 'trct': 99},
            })
            self.assertEqual(json.loads(output.read_text()), {
                'first.jpg': [1, 7, 1234, 0],
                'legacy.jpg': [2, 3, None, None],
            })


if __name__ == '__main__':
    unittest.main()
