import asyncio
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import backfill_telegram as backfill


def photo_blob(color='white', format='PNG', **options):
    image = Image.new('RGB', (160, 120), color)
    ImageDraw.Draw(image).rectangle((20, 30, 80, 90), fill='blue')
    output = io.BytesIO()
    image.save(output, format=format, **options)
    return output.getvalue()


def message(message_id, *, photo=True):
    return SimpleNamespace(
        id=message_id, photo=photo, date=dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc),
        forwards=2, reactions=SimpleNamespace(results=[SimpleNamespace(count=3), SimpleNamespace(count=4)]),
        views=123, replies=SimpleNamespace(replies=0),
    )


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.downloads = []
        self.requests = []
        self.fail_id = None

    async def iter_messages(self, channel, *, reverse, min_id, limit):
        assert reverse
        selected = [msg for msg in self.messages if msg.id > min_id]
        for msg in selected if limit is None else selected[:limit]:
            yield msg

    async def download_media(self, msg, result_type):
        self.downloads.append(msg.id)
        if msg.id == self.fail_id:
            raise RuntimeError('download failed')
        return photo_blob()

    async def get_messages(self, channel, *, ids):
        self.requests.append(ids)
        lookup = {msg.id: msg for msg in self.messages}
        return [lookup.get(key) for key in ids]


class MatchTests(unittest.TestCase):
    def test_exact_bytes_and_unique_decoded_pixels(self):
        target = backfill.image_features(photo_blob())
        other_encoding = backfill.image_features(photo_blob(format='BMP'))
        self.assertEqual(backfill.choose_match(target, [('original.jpg', target)]),
                         ('original.jpg', 'bytes'))
        self.assertEqual(backfill.choose_match(target, [('original.jpg', other_encoding)]),
                         ('original.jpg', 'pixels'))

    def test_ambiguous_exact_match_is_not_assigned(self):
        target = backfill.image_features(photo_blob())
        self.assertEqual(backfill.choose_match(target, [('a.jpg', target), ('b.jpg', target)],
                                              allow_visual=True), (None, 'ambiguous'))

    def test_visual_matches_require_opt_in_and_reject_different_images(self):
        # Simulate recompression without relying on JPEG codec rounding.
        target = backfill.image_features(photo_blob())
        features = ('different bytes', 'different pixels', target[2],
                    Image.new('RGB', (128, 128), 'black'))
        self.assertEqual(backfill.choose_match(target, [('a.jpg', features)], allow_visual=True),
                         (None, 'unmatched'))
        features = ('different bytes', 'different pixels', target[2], target[3])
        self.assertEqual(backfill.choose_match(target, [('a.jpg', features)]), (None, 'unmatched'))
        name, status = backfill.choose_match(target, [('a.jpg', features)], allow_visual=True)
        self.assertEqual(name, 'a.jpg')
        self.assertTrue(status.startswith('visual'))
        self.assertEqual(backfill.choose_match(target, [('a.jpg', features), ('b.jpg', features)],
                                              allow_visual=True), (None, 'ambiguous'))

    def test_visual_matches_reject_aspect_ratio_changes(self):
        target = backfill.image_features(photo_blob())
        features = ('other', 'other', target[2] * 1.02, target[3])
        self.assertEqual(backfill.choose_match(target, [('a.jpg', features)], allow_visual=True),
                         (None, 'unmatched'))

    def test_archive_matching_uses_originals_and_date_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'images'
            month = images / '2022/01'
            month.mkdir(parents=True)
            (images / 'dir_index.json').write_text('{"2022/01": 2}')
            name = '2022-01-01_original.jpg'
            (month / 'entry_index.json').write_text(json.dumps([
                {'name': name}, {'name': 'thumbnails.jpg'},
            ]))
            (month / name).write_bytes(photo_blob())
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                matcher = backfill.ArchiveMatcher(images, [root], days=3, allow_visual=False)
                self.assertEqual(matcher.match(photo_blob(), dt.date(2022, 1, 4)), (name, 'bytes'))
                self.assertEqual(matcher.match(photo_blob(), dt.date(2022, 1, 5)), (None, 'unmatched'))


class BackfillTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.state_path = root / 'state.json'
        self.cache_path = root / 'messages.json'
        self.metadata_path = root / 'metadata.json'
        for module, name, value in [
            (backfill, 'STATE_PATH', self.state_path),
            (backfill.sync, 'MESSAGES_CACHE_PATH', self.cache_path),
            (backfill.sync, 'GALLERY_METADATA_PATH', self.metadata_path),
        ]:
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        output = patch('builtins.print')
        output.start()
        self.addCleanup(output.stop)

    def test_history_fills_old_links_preserves_existing_and_resumes(self):
        client = FakeClient([message(1), message(2, photo=False), message(3), message(100)])
        messages = {100: {'name': 'known.jpg', 'dig': 'existing', 'trct': 1}}
        state = {}
        matcher = Mock()
        matcher.match.return_value = ('old.jpg', 'bytes')
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=2))
        self.assertEqual(state['history_last_id'], 2)
        self.assertEqual(messages[1]['name'], 'old.jpg')
        self.assertNotIn(2, messages)
        self.assertEqual(client.downloads, [1])
        matcher.match.return_value = (None, 'ambiguous')
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None))
        self.assertEqual(client.downloads, [1, 3])
        self.assertEqual(messages[100]['name'], 'known.jpg')
        self.assertEqual(messages[100]['dig'], 'existing')
        self.assertIsNone(messages[3]['name'])
        self.assertEqual(messages[3]['match_status'], 'ambiguous')
        self.assertEqual(json.loads(self.metadata_path.read_text()), {
            'old.jpg': [1, 7, 123, 0], 'known.jpg': [100, 7, 123, 0],
        })
        self.assertEqual(json.loads(self.state_path.read_text())['history_last_id'], 100)

    def test_download_failure_checkpoints_without_skipping_failed_message(self):
        client = FakeClient([message(1), message(2)])
        client.fail_id = 2
        state = {}
        messages = {}
        matcher = Mock()
        matcher.match.return_value = ('old.jpg', 'pixels')
        with self.assertRaisesRegex(RuntimeError, 'download failed'):
            asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None))
        self.assertEqual(state['history_last_id'], 1)
        self.assertEqual(set(json.loads(self.cache_path.read_text())), {'1'})
        self.assertEqual(json.loads(self.state_path.read_text())['history_last_id'], 1)

    def test_match_failure_checkpoints_without_creating_guessed_mapping(self):
        client = FakeClient([message(1)])
        matcher = Mock()
        matcher.match.side_effect = RuntimeError('archive unavailable')
        messages, state = {}, {}
        with self.assertRaisesRegex(RuntimeError, 'archive unavailable'):
            asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None))
        self.assertEqual(messages, {})
        self.assertNotIn('history_last_id', state)

    def test_retry_previously_unmatched_photo(self):
        client = FakeClient([message(1)])
        messages = {1: {'name': None, 'dig': 'legacy'}}
        matcher = Mock()
        matcher.match.return_value = ('recovered.jpg', 'bytes')
        asyncio.run(backfill.backfill_history(client, 'channel', messages, {}, matcher, limit=None))
        self.assertEqual(messages[1]['name'], 'recovered.jpg')

    def test_stats_only_batches_resume_and_preserve_deleted_totals(self):
        client = FakeClient([message(1), message(3)])
        messages = {key: {'name': f'{key}.jpg', 'trct': 9} for key in [1, 2, 3]}
        state = {}
        asyncio.run(backfill.refresh_stats(client, 'channel', messages, state, limit=2))
        self.assertEqual(state['stats_last_id'], 2)
        self.assertEqual(messages[1]['tcomments'], 0)
        self.assertEqual(messages[2], {'name': '2.jpg', 'trct': 9})
        asyncio.run(backfill.refresh_stats(client, 'channel', messages, state, limit=None))
        self.assertEqual(client.requests, [[1, 2], [3]])
        self.assertEqual(client.downloads, [])
        self.assertEqual(state['stats_last_id'], 0)
        self.assertEqual(messages[3]['trct'], 7)

    def test_missing_comment_and_view_counts_remain_unknown(self):
        msg = message(1)
        msg.views = msg.replies = msg.reactions = msg.forwards = None
        self.assertEqual(backfill.sync.telegram_stats(msg), {
            'tfwd': 0, 'trct': 0, 'tview': None, 'tcomments': None,
        })


if __name__ == '__main__':
    unittest.main()
