import ast
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / "scripts/panzer_imgsync.py"


def sync_function(name, **namespace):
    # Importing the sync module would require Telegram credentials.
    tree = ast.parse(SOURCE.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace[name]


@contextlib.contextmanager
def change_dir(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class ThumbnailSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.www = self.root / "www"
        self.www.mkdir()
        self.sp = Mock()

    def test_archive_commit_generates_sprites_in_www_checkout(self):
        ingest = SimpleNamespace(update_indexes=Mock(), update_thumbnails=Mock())
        archive = self.root / "archive"
        commit = sync_function("_commit_archive", change_dir=lambda path: contextlib.nullcontext(),
                               ROOT_DIR=self.www, sp=self.sp, dt=dt)
        with patch.dict("sys.modules", {"ingest_uploads": ingest}), contextlib.redirect_stdout(None):
            commit(archive)
        ingest.update_indexes.assert_called_once_with(archive)
        ingest.update_thumbnails.assert_called_once_with(archive, self.www)
        self.assertEqual(self.sp.call.call_args_list[0].args[0], ["git", "add", "images/"])

    def test_www_commit_includes_local_sprites_and_not_unrelated_changes(self):
        (self.www / "images").mkdir()
        self.sp.run.return_value = SimpleNamespace(returncode=0, stdout="?? images/\n")
        commit = sync_function("_commit_www", pl=__import__("pathlib"), sp=self.sp, dt=dt)
        with change_dir(self.www), contextlib.redirect_stdout(None):
            commit()
        staged = self.sp.call.call_args_list[0].args[0]
        self.assertIn("images/", staged)
        self.assertIn("images/dir_index.json", staged)
        self.assertNotIn("assets/app.js", staged)
        self.assertNotIn("media.html", staged)
        self.assertIn("images/", self.sp.run.call_args.args[0])

    def test_ingest_cleanup_retains_local_sprites(self):
        staging = self.www / "images/2024/01"
        staging.mkdir(parents=True)
        (staging / "original.jpg").write_bytes(b"original")
        sprites = self.www / "images/2024/01"
        sprites.mkdir(parents=True, exist_ok=True)
        (sprites / "thumbnails-00.webp").write_bytes(b"sprite")
        (sprites / "entry_index.json").write_text("[]")
        (self.www / "images/dir_index.json").write_text("{}")
        archive = self.root / "archive/images"
        archive.mkdir(parents=True)
        (archive / "dir_index.json").write_text(json.dumps({"2024/01": 1}))
        update = sync_function("_update_dir_index", pl=__import__("pathlib"), json=json,
                               shutil=shutil, sp=self.sp, IMG_REPOS={"2024": "archive"})
        with change_dir(self.www), contextlib.redirect_stdout(None):
            update(staging)
        self.assertFalse((staging / "original.jpg").exists())
        self.assertEqual((sprites / "thumbnails-00.webp").read_bytes(), b"sprite")
        self.assertEqual((sprites / "entry_index.json").read_text(), "[]")
        self.assertEqual(json.loads((self.www / "images/dir_index.json").read_bytes()), {"2024/01": 1})


if __name__ == "__main__":
    unittest.main()
