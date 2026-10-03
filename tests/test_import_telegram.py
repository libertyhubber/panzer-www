import asyncio
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from telethon.tl.types import MessageMediaPhoto

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import import_telegram as importer


def jpeg(color):
    output = io.BytesIO()
    Image.new('RGB', (40, 30), color).save(output, 'JPEG')
    return output.getvalue()


def message(identifier, day=8, *, photo=True):
    attached = SimpleNamespace(id=identifier, dc_id=2, sizes=[
        SimpleNamespace(type='y', w=40, h=30, size=1000),
    ], video_sizes=[]) if photo else None
    return SimpleNamespace(id=identifier, photo=attached,
                           media=MessageMediaPhoto(attached) if photo else None,
                           date=dt.datetime(2021, 9, day, tzinfo=dt.timezone.utc),
                           forwards=1, reactions=None, views=42, replies=None)


class Client:
    def __init__(self, messages, blobs):
        self.messages = messages
        self.blobs = blobs
        self.requests = []
        self.downloads = []

    async def iter_messages(self, channel, *, reverse, min_id, limit):
        assert reverse
        self.requests.append((min_id, limit))
        selected = sorted((msg for msg in self.messages if msg.id > min_id), key=lambda msg: msg.id)
        for msg in selected if limit is None else selected[:limit]:
            yield msg

    async def download_media(self, photo, target, *, thumb, progress_callback):
        self.downloads.append((photo.id, thumb))
        result = self.blobs[photo.id]
        if isinstance(result, Exception):
            raise result
        return result


class ImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.www = self.root / 'www'
        self.images = self.www / 'images'
        self.images.mkdir(parents=True)
        (self.www / 'scripts').mkdir()
        (self.images / 'dir_index.json').write_text('{}')
        self.archive = self.root / 'archive'
        (self.archive / 'images').mkdir(parents=True)
        self.state_path = self.www / 'scripts/telegram_import_state.json'
        self.cache_path = self.www / 'scripts/telegram_messages_cache.json'
        self.metadata_path = self.images / 'telegram_metadata.json'
        self.addCleanup(patch.stopall)
        patch.object(importer, 'STATE_PATH', self.state_path).start()
        patch.object(importer.sync, 'ROOT_DIR', self.www).start()
        patch.object(importer.sync, 'IMAGES_DIR', self.images).start()
        patch.object(importer.sync, 'IMG_REPOS', {'2021': 'archive'}).start()
        patch.object(importer.sync, 'MESSAGES_CACHE_PATH', self.cache_path).start()
        patch.object(importer.sync, 'GALLERY_METADATA_PATH', self.metadata_path).start()
        patch.object(importer.backfill, 'load_verified_matches', return_value={}).start()
        self.red = jpeg('red')
        self.blue = jpeg('blue')

    def matcher(self):
        return importer.backfill.ArchiveMatcher(self.images, allow_visual=True, include_archive_months=True)

    def run_import(self, client, messages=None, state=None, **kwargs):
        messages = {} if messages is None else messages
        state = {} if state is None else state
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(importer.import_history(client, 'channel', messages, state, self.matcher(),
                                                limit=kwargs.pop('limit', None), **kwargs))
        return messages, state

    def archived(self, name, blob):
        path = self.archive / 'images/2021/09' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        importer.ingest_uploads.update_indexes(self.archive)
        return path

    def test_imports_old_photo_unchanged_and_updates_indexes_metadata_cursor(self):
        client = Client([message(4)], {4: self.red})
        messages, state = self.run_import(client)
        name = messages[4]['name']
        self.assertIn('_4_', name)
        self.assertEqual((self.archive / 'images/2021/09' / name).read_bytes(), self.red)
        index = json.loads((self.archive / 'images/2021/09/entry_index.json').read_text())
        self.assertEqual(index, [{'w': 40, 'h': 30, 'bg': 'F00', 'name': name}])
        self.assertEqual(json.loads(self.metadata_path.read_text())[name], [4, 0, 42, None])
        self.assertEqual(json.loads(self.state_path.read_text())['last_id'], 4)
        self.assertEqual(state['pending_archives'], ['archive'])
        self.assertEqual(messages[4]['match_status'], 'imported')
        self.assertEqual(client.downloads, [(4, 'y')])

    def test_existing_mapping_and_webpage_preview_are_never_downloaded(self):
        old = {'name': '2021-09-08_existing.jpg', 'custom': 'preserve', 'tview': 99}
        preview = message(5)
        preview.media = SimpleNamespace()
        client = Client([message(4), preview, message(6, photo=False)], {})
        messages, state = self.run_import(client, {4: old.copy()})
        self.assertEqual(messages, {4: old})
        self.assertFalse(client.downloads)
        self.assertEqual(state['last_id'], 6)

    def test_unmapped_repost_links_existing_original_even_in_website_missing_month(self):
        name = '2021-09-08_existing.jpg'
        self.archived(name, self.red)
        client = Client([message(4)], {4: self.red})
        messages, state = self.run_import(client, {4: {'name': None, 'match_status': 'unmatched', 'custom': True}})
        self.assertEqual(messages[4]['name'], name)
        self.assertTrue(messages[4]['custom'])
        self.assertEqual(messages[4]['match_status'], 'bytes')
        self.assertNotIn('pending_archives', state)
        self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 1)

    def test_ambiguous_existing_originals_are_not_imported(self):
        self.archived('2021-09-08_a.jpg', self.red)
        self.archived('2021-09-08_b.jpg', self.red)
        messages, state = self.run_import(Client([message(4)], {4: self.red}))
        self.assertEqual(messages, {})
        self.assertEqual(state['last_id'], 4)
        self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 2)

    def test_duplicate_new_photos_write_only_one_original(self):
        client = Client([message(4), message(5)], {4: self.red, 5: self.red})
        messages, _ = self.run_import(client)
        self.assertEqual(messages[4]['name'], messages[5]['name'])
        self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 1)

    def test_decoded_pixel_duplicate_of_new_original_is_also_reused(self):
        output = io.BytesIO()
        with Image.open(io.BytesIO(self.red)) as image:
            image.save(output, 'PNG')
        messages, _ = self.run_import(Client([message(4), message(5)], {4: self.red, 5: output.getvalue()}))
        self.assertEqual(messages[4]['name'], messages[5]['name'])
        self.assertEqual(messages[5]['match_status'], 'pixels')

    def test_failed_download_preserves_completed_work_and_resumes_failed_message(self):
        client = Client([message(4), message(5)], {4: self.red, 5: RuntimeError('transfer failed')})
        with self.assertRaisesRegex(RuntimeError, 'transfer failed'):
            self.run_import(client)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state['last_id'], 4)
        messages = importer.sync.load_last_messages()
        self.assertIn(4, messages)
        retry = Client([message(4), message(5)], {5: self.blue})
        self.run_import(retry, messages, state)
        self.assertEqual(retry.requests, [(4, None)])
        self.assertEqual(state['last_id'], 5)
        self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 2)

    def test_limit_counts_history_messages_and_resume_skips_completed_messages(self):
        client = Client([message(4, photo=False), message(5)], {5: self.red})
        messages, state = self.run_import(client, limit=1)
        self.assertEqual(messages, {})
        self.assertEqual(state['last_id'], 4)
        self.run_import(client, messages, state)
        self.assertEqual(state['last_id'], 5)
        self.assertEqual(client.requests, [(0, 1), (4, None)])

    def test_dry_run_does_not_download_or_mutate_files_records_or_cursor(self):
        before = sorted(path.relative_to(self.root) for path in self.root.rglob('*'))
        messages = {4: {'name': None}}
        state = {'last_id': 3}
        client = Client([message(4)], {})
        with patch.object(importer.backfill, 'download_photo', new_callable=AsyncMock) as download:
            self.run_import(client, messages, state, dry_run=True)
        download.assert_not_called()
        self.assertEqual(messages, {4: {'name': None}})
        self.assertEqual(state, {'last_id': 3})
        self.assertEqual(before, sorted(path.relative_to(self.root) for path in self.root.rglob('*')))

    def test_corrupt_download_is_not_archived_and_cursor_does_not_advance(self):
        with self.assertRaises(Exception):
            self.run_import(Client([message(4)], {4: b'not an image'}))
        self.assertFalse(list(self.archive.rglob('*.jpg')))
        self.assertNotIn('last_id', json.loads(self.state_path.read_text()))

    def test_missing_archive_fails_before_download(self):
        (self.archive / 'images').rmdir()
        client = Client([message(4)], {4: self.red})
        with self.assertRaisesRegex(ValueError, 'archive checkout is missing'):
            self.run_import(client)
        self.assertFalse(client.downloads)

    def test_original_collision_is_not_overwritten(self):
        path = self.archived('2021-09-08_collision.jpg', self.red)
        with self.assertRaisesRegex(ValueError, 'refusing to overwrite'):
            importer.save_original(path, self.blue)
        self.assertEqual(path.read_bytes(), self.red)

    def test_index_failure_does_not_publish_mapping_or_cursor(self):
        self.state_path.write_text('{"last_id": 3}')
        with patch.object(importer.ingest_uploads, 'update_indexes', side_effect=RuntimeError('index failure')):
            with self.assertRaisesRegex(RuntimeError, 'index failure'):
                self.run_import(Client([message(4)], {4: self.red}), state={'last_id': 3})
        self.assertEqual(json.loads(self.state_path.read_text()), {'last_id': 3})
        self.assertFalse(self.cache_path.exists())
        # Retry recovers the already written deterministic original.
        self.run_import(Client([message(4)], {4: self.red}), state={'last_id': 3})
        self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 1)

    def test_recovery_of_indexed_orphan_retains_ingest_queue(self):
        name = f'2021-09-08T000000_4_{importer.sync.digest_img(self.red)}.jpg'
        self.archived(name, self.red)
        messages, state = self.run_import(Client([message(4)], {4: self.red}))
        self.assertEqual(messages[4]['name'], name)
        self.assertEqual(messages[4]['match_status'], 'imported')
        self.assertEqual(state['pending_archives'], ['archive'])
        self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 1)

    def test_cache_saved_before_cursor_failure_recovers_ingest_queue(self):
        name = f'2021-09-08T000000_4_{importer.sync.digest_img(self.red)}.jpg'
        self.archived(name, self.red)
        client = Client([message(4)], {})
        _, state = self.run_import(client, {4: {'name': name, 'match_status': 'imported'}})
        self.assertEqual(state['pending_archives'], ['archive'])
        self.assertFalse(client.downloads)

    def test_ingest_failure_retains_pending_queue_and_never_publishes_website(self):
        state = {'pending_archives': ['archive']}
        with patch.object(importer.sync, '_commit_archive', side_effect=RuntimeError('classification failed')), \
             patch.object(importer.sync, '_commit_www') as publish:
            with self.assertRaisesRegex(RuntimeError, 'classification failed'):
                importer.ingest_pending(state, publish=True)
        self.assertEqual(state['pending_archives'], ['archive'])
        publish.assert_not_called()

    def test_ingest_is_local_unless_publish_explicitly_requested(self):
        state = {'pending_archives': ['archive']}
        with patch.object(importer.sync, '_commit_archive') as archive, \
             patch.object(importer.sync, '_update_dir_index') as indexes, \
             patch.object(importer.sync, '_commit_www') as publish:
            importer.ingest_pending(state, publish=False)
        archive.assert_called_once_with(self.archive, no_git=True)
        indexes.assert_called_once_with(None)
        publish.assert_not_called()
        self.assertEqual(state['pending_archives'], [])
        self.assertEqual(state['unpublished_archives'], ['archive'])
        # A later explicit publish must push archives previously ingested locally.
        with patch.object(importer.sync, '_commit_archive') as archive, \
             patch.object(importer.sync, '_update_dir_index'), \
             patch.object(importer.sync, '_commit_www') as publish:
            importer.ingest_pending(state, publish=True)
        archive.assert_called_once_with(self.archive, no_git=False)
        publish.assert_called_once_with()
        self.assertEqual(state['unpublished_archives'], [])

    def test_cli_defaults_do_not_classify_or_publish(self):
        with patch.object(importer, 'run', new_callable=AsyncMock) as run:
            self.assertEqual(importer.main(['--limit', '100']), 0)
        options = run.call_args.args[0]
        self.assertFalse(options.ingest)
        self.assertFalse(options.publish)

    def test_cli_rejects_unsafe_combinations(self):
        for args in [['--publish'], ['--dry-run', '--ingest'], ['--limit', '0']]:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    importer.main(args)


if __name__ == '__main__':
    unittest.main()
