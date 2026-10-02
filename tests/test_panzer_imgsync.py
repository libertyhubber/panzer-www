import asyncio
import contextlib
import datetime as dt
import io
import json
import importlib.util
import sqlite3
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from PIL import Image
from scripts import panzer_imgsync as sync


@contextlib.contextmanager
def change_dir(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.www = self.root / 'www'
        self.www.mkdir()
        self.images = self.www / 'images'
        self.images.mkdir()
        self.archive = self.root / 'archive'
        (self.archive / 'images').mkdir(parents=True)
        self.addCleanup(patch.stopall)
        patch.object(sync, 'IMAGES_DIR', self.images).start()
        self.output = io.BytesIO()
        Image.new('RGB', (20, 30), 'red').save(self.output, 'JPEG')
        self.blob = self.output.getvalue()
        self.digest = sync.digest_img(self.blob)

    def test_fingerprint_remains_compatible(self):
        sync.test_fingerprint_image()

    def photo(self, message_id, day=1):
        return SimpleNamespace(
            id=message_id, photo=object(), media=type('MessageMediaPhoto', (), {})(),
            file=SimpleNamespace(mime_type='image/jpeg'),
            date=dt.datetime(2026, 10, day, tzinfo=dt.timezone.utc),
            forwards=0, reactions=None, views=10, replies=None,
        )

    def fetch(self, messages, cache):
        async def iterate(*args, **kwargs):
            for message in messages:
                yield message

        async def download(*args):
            return self.blob

        client = SimpleNamespace(iter_messages=Mock(side_effect=iterate),
                                 download_media=Mock(side_effect=download))
        async def get_me():
            return SimpleNamespace()
        client.get_me = get_me
        with patch.object(sync, 'init_telethon_client', return_value=client), contextlib.redirect_stdout(None):
            result = asyncio.run(sync.fetch_api_messages(cache))
        return result, client

    def test_duplicate_reuses_archived_cache_mapping_without_writing_original(self):
        name = f'2026-10-01T000000_23000_{self.digest}.jpg'
        result, _ = self.fetch([self.photo(23001)], {23000: {'name': name, 'dig': self.digest}})
        self.assertEqual(result[23001]['name'], name)
        self.assertFalse(list(self.images.rglob('*.jpg')))

    def test_duplicates_within_same_batch_only_write_one_original(self):
        result, _ = self.fetch([self.photo(23002), self.photo(23001)], {})
        self.assertEqual(result[23002]['name'], result[23001]['name'])
        self.assertEqual(len(list(self.images.rglob('*.jpg'))), 1)

    def test_nonmatching_digest_date_does_not_replace_new_filename(self):
        old_name = f'2026-09-01T000000_23000_{self.digest}.jpg'
        result, client = self.fetch([self.photo(23001)], {23000: {'name': old_name, 'dig': self.digest}})
        self.assertNotEqual(result[23001]['name'], old_name)
        self.assertTrue(sync.mk_img_path(result[23001]['name']).exists())
        self.assertIsNone(client.iter_messages.call_args.kwargs['limit'])

    def test_unmatched_backfill_entry_can_be_retried(self):
        result, _ = self.fetch([self.photo(23001)], {23001: {'name': None, 'match_status': 'unmatched'}})
        self.assertTrue(sync.mk_img_path(result[23001]['name']).exists())

    def test_existing_mapping_without_digest_can_refresh_stats(self):
        result, client = self.fetch([self.photo(23001)], {23001: {'name': '2026-10-01T000000_23001.jpg'}})
        self.assertEqual(result[23001]['tview'], 10)
        client.download_media.assert_not_called()

    def update_images(self, args=()):
        client = MagicMock()
        async def fetch(messages):
            return messages
        # Close the mocked coroutine to avoid unawaited-coroutine warnings.
        def complete(coroutine):
            coroutine.close()
            return {}
        client.loop.run_until_complete.side_effect = complete
        with patch.object(sync, 'init_telethon_client', return_value=client), \
             patch.object(sync, 'load_last_messages', return_value={}), \
             patch.object(sync, 'fetch_api_messages', side_effect=fetch), \
             patch.object(sync, 'dump_gallery_metadata'), \
             patch.object(sync, 'IMG_REPOS', {'2025': 'archive', '2026': 'archive', '2027': 'future'}), \
             change_dir(self.www), contextlib.redirect_stdout(None):
            return sync._update_images(list(args))

    def test_ingests_all_pending_months_and_only_originals(self):
        for month in ['2025/12', '2026/01']:
            directory = self.images / month
            directory.mkdir(parents=True)
            (directory / 'original.jpg').write_bytes(self.blob)
            (directory / 'thumbnails-00.jpg').write_bytes(b'sprite')
            (directory / 'thumbnails-00.webp').write_bytes(b'sprite')
            (directory / 'entry_index.json').write_text('[]')
        updates = self.update_images()
        self.assertEqual(len(updates), 2)
        for month in ['2025/12', '2026/01']:
            directory = self.archive / 'images' / month
            self.assertEqual([path.name for path in directory.iterdir()], ['original.jpg'])

    def test_ingest_replaces_different_original_with_same_size(self):
        source = self.images / '2026/10'
        target = self.archive / 'images/2026/10'
        source.mkdir(parents=True)
        target.mkdir(parents=True)
        (source / 'original.jpg').write_bytes(b'new')
        (target / 'original.jpg').write_bytes(b'old')
        self.update_images()
        self.assertEqual((target / 'original.jpg').read_bytes(), b'new')

    def test_no_new_photos_still_ingests_latest_archive(self):
        month = self.archive / 'images/2026/10'
        month.mkdir(parents=True)
        (month / 'entry_index.json').write_text('[]')
        self.assertEqual(self.update_images(), [(None, self.archive)])

    def test_force_skips_empty_future_archive(self):
        month = self.archive / 'images/2026/10'
        month.mkdir(parents=True)
        (month / 'entry_index.json').write_text('[]')
        (self.root / 'future/images').mkdir(parents=True)
        self.assertEqual(self.update_images(['--force']), [(None, self.archive)])

    def test_legacy_thumbnail_does_not_trigger_ingest(self):
        month = self.images / '2026/10'
        month.mkdir(parents=True)
        (month / 'thumbnails.jpg').write_bytes(b'sprite')
        self.assertEqual(self.update_images(), [])

    def test_missing_archive_fails_without_removing_originals(self):
        directory = self.images / '2027/01'
        directory.mkdir(parents=True)
        (directory / 'original.jpg').write_bytes(self.blob)
        with self.assertRaisesRegex(ValueError, 'archive checkout is missing'):
            self.update_images()
        self.assertTrue((directory / 'original.jpg').exists())

    def test_archive_failure_prevents_cleanup_and_website_publish(self):
        with patch.object(sync, '_update_images', return_value=[(self.images, self.archive)]), \
             patch.object(sync, '_commit_archive', side_effect=subprocess.CalledProcessError(1, ['git', 'push'])), \
             patch.object(sync, '_update_dir_index') as cleanup, \
             patch.object(sync, '_commit_www') as publish:
            with self.assertRaises(subprocess.CalledProcessError):
                sync.main([])
        cleanup.assert_not_called()
        publish.assert_not_called()

    def test_multiple_months_in_same_archive_publish_once(self):
        updates = [(self.images / '2026/01', self.archive), (self.images / '2026/02', self.archive)]
        with patch.object(sync, '_update_images', return_value=updates), \
             patch.object(sync, '_commit_archive') as archive, \
             patch.object(sync, '_update_dir_index') as cleanup, \
             patch.object(sync, '_commit_www'):
            self.assertEqual(sync.main([]), 0)
        archive.assert_called_once_with(self.archive, classification_paths=[])
        self.assertEqual(cleanup.call_count, 2)

    def test_no_git_still_ingests_and_cleans_up(self):
        with patch.object(sync, '_update_images', return_value=[(self.images, self.archive)]), \
             patch.object(sync, '_commit_archive') as archive, \
             patch.object(sync, '_update_dir_index') as cleanup, \
             patch.object(sync, '_commit_www') as publish:
            self.assertEqual(sync.main(['--no-git']), 0)
        archive.assert_called_once_with(self.archive, no_git=True, classification_paths=[])
        cleanup.assert_called_once_with(self.images)
        publish.assert_not_called()

    def test_daily_sync_scopes_classification_to_staging_originals(self):
        staging = self.images / '2026/10'
        staging.mkdir(parents=True)
        (staging / 'new.jpg').write_bytes(self.blob)
        (staging / 'retry.jpg').write_bytes(self.blob)
        (staging / 'thumbnails-00.jpg').write_bytes(b'sprite')
        (staging / 'thumbnails.jpg').write_bytes(b'sprite')
        with patch.object(sync, '_update_images', return_value=[(staging, self.archive)]), \
             patch.object(sync, '_commit_archive') as archive, \
             patch.object(sync, '_update_dir_index'), patch.object(sync, '_commit_www'):
            self.assertEqual(sync.main([]), 0)
        archive.assert_called_once_with(self.archive, classification_paths=[
            self.archive / 'images/2026/10/new.jpg',
            self.archive / 'images/2026/10/retry.jpg',
        ])

    def test_daily_sync_without_new_photos_never_classifies_backlog(self):
        with patch.object(sync, '_update_images', return_value=[(None, self.archive)]), \
             patch.object(sync, '_commit_archive') as archive, \
             patch.object(sync, '_update_dir_index'), patch.object(sync, '_commit_www'):
            self.assertEqual(sync.main(['--force']), 0)
        archive.assert_called_once_with(self.archive, classification_paths=[])

    def test_scoped_archive_batch_is_forwarded_to_ingest(self):
        ingest = SimpleNamespace(ingest_archive=Mock())
        paths = [self.archive / 'images/2026/10/new.jpg']
        with patch.dict('sys.modules', {'ingest_uploads': ingest}), \
             patch.object(sync, '_publish_generated'):
            sync._commit_archive(self.archive, classification_paths=paths)
        ingest.ingest_archive.assert_called_once_with(self.archive, sync.ROOT_DIR, classification_paths=paths)

    def test_no_git_archive_updates_assets_without_publication(self):
        ingest = SimpleNamespace(ingest_archive=Mock())
        with patch.dict('sys.modules', {'ingest_uploads': ingest}), \
             patch.object(sync, '_publish_generated') as publish:
            sync._commit_archive(self.archive, no_git=True)
        ingest.ingest_archive.assert_called_once_with(self.archive, sync.ROOT_DIR)
        publish.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec('telethon'), 'requires Telethon')
    def test_telethon_can_open_existing_version_8_session(self):
        from telethon.sessions import SQLiteSession
        path = self.root / 'test.session'
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('CREATE TABLE version (version INTEGER PRIMARY KEY)')
            connection.execute('INSERT INTO version VALUES (8)')
            connection.execute('CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, '
                               'server_address TEXT, port INTEGER, auth_key BLOB, '
                               'takeout_id INTEGER, tmp_auth_key BLOB)')
            connection.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)',
                               (2, '127.0.0.1', 443, bytes(256), None, None))
        session = SQLiteSession(str(path))
        try:
            self.assertEqual(session.dc_id, 2)
            self.assertEqual(session.server_address, '127.0.0.1')
        finally:
            session.close()

    def test_directory_index_can_be_created_on_first_run(self):
        with patch.object(sync, 'IMG_REPOS', {'2026': 'archive'}), \
             change_dir(self.www), contextlib.redirect_stdout(None):
            sync._update_dir_index(None)
        self.assertEqual(json.loads((self.images / 'dir_index.json').read_text()), {})


class PublishingTests(unittest.TestCase):
    def test_git_errors_stop_publication(self):
        for failed_step in ['status', 'add', 'commit', 'push']:
            calls = []
            def run(command, **kwargs):
                calls.append(command[1])
                self.assertTrue(kwargs['check'])
                if command[1] == failed_step:
                    raise subprocess.CalledProcessError(1, command)
                return SimpleNamespace(stdout=' M images/index.json\n')
            with self.subTest(step=failed_step), patch.object(sync.sp, 'run', side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError):
                    sync._publish_generated(['images/'])
            self.assertEqual(calls[-1], failed_step)

    def test_unchanged_archive_skips_commit_but_retries_push(self):
        with patch.object(sync.sp, 'run', return_value=SimpleNamespace(stdout='')) as run:
            sync._publish_generated(['images/'])
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ['status', 'push'])

    def test_unrelated_staged_changes_are_not_committed(self):
        with tempfile.TemporaryDirectory() as directory, change_dir(directory):
            def git(*args):
                return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout
            git('init', '-q')
            git('config', 'user.name', 'Sync Test')
            git('config', 'user.email', 'sync@example.invalid')
            images = Path('images')
            images.mkdir()
            (images / 'index.json').write_text('{}')
            Path('unrelated.txt').write_text('before')
            git('add', '.')
            git('commit', '-qm', 'initial')
            Path('unrelated.txt').write_text('after')
            git('add', 'unrelated.txt')
            (images / 'index.json').write_text('{"new":1}')
            original_run = subprocess.run
            def run(command, **kwargs):
                if command == ['git', 'push']:
                    return SimpleNamespace(returncode=0)
                kwargs.setdefault('capture_output', True)
                return original_run(command, **kwargs)
            with patch.object(sync.sp, 'run', side_effect=run):
                sync._publish_generated(['images/', 'images/index.json'])
            self.assertEqual(git('show', '--format=', '--name-only', 'HEAD').strip(), 'images/index.json')
            self.assertEqual(git('diff', '--cached', '--name-only').strip(), 'unrelated.txt')


if __name__ == '__main__':
    unittest.main()
