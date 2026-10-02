import asyncio
import contextlib
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import backfill_telegram as backfill
from test_telegram_backfill import FakeClient, message

FIXTURES = Path(__file__).parent / 'fixtures/telegram_verified_batch'
ROWS = json.loads((FIXTURES / 'manifest.json').read_text())
OLD_SETTINGS = dict(
    VISUAL_ALGORITHM_VERSION=10, VISUAL_LUMA_MAX_RMS=2.25,
    VISUAL_LUMA_MAX_OUTLIER_FRACTION=0.0015,
    VISUAL_CHROMA_MAX_RMS=3.75, VISUAL_CHROMA_EDGE_MAX_OUTLIER_FRACTION=0.035,
    VISUAL_CHROMA_EDGE_MAX_FLAT_OUTLIER_FRACTION=0.0025,
    VISUAL_CHROMA_EDGE_MAX_BLURRED_RMS=3.25,
    VISUAL_CHROMA_EDGE_MAX_BLURRED_OUTLIER_FRACTION=0.01,
    VISUAL_MAX_CHANNEL_MEAN_SHIFT=1.5, VISUAL_MAX_LUMA_MEAN_SHIFT=1.75,
)


def blob(row, which):
    return (FIXTURES / str(row['message_id']) / f'{which}.jpg').read_bytes()


def make_archive(root, rows=ROWS):
    images = root / 'panzer-www/images'
    images.mkdir(parents=True)
    months = sorted({row['name'][:4] + '/' + row['name'][5:7] for row in rows})
    (images / 'dir_index.json').write_text(json.dumps({month: 1 for month in months}))
    for month in months:
        originals = root / 'panzer-archiv-00/images' / month
        originals.mkdir(parents=True)
        selected = [row for row in rows if row['name'].startswith(month.replace('/', '-'))]
        (originals / 'entry_index.json').write_text(json.dumps([{'name': row['name']} for row in selected]))
        for row in selected:
            (originals / row['name']).write_bytes(blob(row, 'archive'))
    return images


class VerifiedBatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.images = make_archive(self.root)
        channel = patch.object(backfill.sync, 'CHANNEL_NAME', '@RosaroterPanzerBackup')
        channel.start()
        self.addCleanup(channel.stop)

    def matcher(self, **kwargs):
        return backfill.ArchiveMatcher(self.images, days=kwargs.pop('days', 90),
                                       allow_visual=kwargs.pop('allow_visual', True), **kwargs)

    def test_fixture_provenance_and_manual_registry_are_consistent(self):
        self.assertEqual(len(ROWS), 43)
        approved = backfill.load_verified_matches()
        manual_ids = {row['message_id'] for row in ROWS if row['manual_confirmation']}
        self.assertEqual(len(manual_ids), 11)
        self.assertEqual(set(approved).intersection(row['message_id'] for row in ROWS), manual_ids)
        for row in ROWS:
            with self.subTest(message_id=row['message_id']):
                for which in ('archive', 'telegram'):
                    data = blob(row, which)
                    self.assertEqual(hashlib.sha256(data).hexdigest(), row[f'{which}_sha256'])
                    with Image.open(io.BytesIO(data)) as image:
                        self.assertEqual(list(image.size), row[f'{which}_size'])
                self.assertEqual(row['manual_confirmation'], row['message_id'] in approved)
                if row['manual_confirmation']:
                    for key in ('message_id', 'name', 'date', 'archive_sha256', 'telegram_sha256'):
                        self.assertEqual(approved[row['message_id']][key], row[key])

    def test_all_43_pairs_match_through_the_index_and_still_require_opt_in(self):
        matcher = self.matcher()
        exact = self.matcher(allow_visual=False)
        for row in ROWS:
            with self.subTest(message_id=row['message_id']):
                date = dt.datetime.fromisoformat(row['date']).date()
                data = blob(row, 'telegram')
                name, status = matcher.match(data, date, message_id=row['message_id'])
                self.assertEqual(name, row['name'])
                self.assertTrue(status.startswith('visual'), status)
                self.assertEqual('manually verified' in status, row['manual_confirmation'])
                self.assertEqual(exact.match(data, date, message_id=row['message_id']), (None, 'unmatched'))

    def test_32_ordinary_pairs_match_full_scan_uniquely_and_keep_duplicates_ambiguous(self):
        features = [(row['name'], backfill.image_features(blob(row, 'archive'))) for row in ROWS]
        by_name = dict(features)
        for row in ROWS:
            if row['manual_confirmation']:
                continue
            with self.subTest(message_id=row['message_id']):
                target = backfill.image_features(blob(row, 'telegram'))
                self.assertEqual(backfill.choose_match(target, features, allow_visual=True)[0], row['name'])
                original = by_name[row['name']]
                self.assertEqual(backfill.choose_match(target, [(row['name'], original), ('duplicate.jpg', original)],
                                                      allow_visual=True), (None, 'ambiguous'))

    def test_dense_edge_allowance_requires_shared_structure_and_bounded_blurred_error(self):
        for message_id in (3444, 2793, 3038):
            row = next(row for row in ROWS if row['message_id'] == message_id)
            target = backfill.image_features(blob(row, 'telegram'))[3]
            original = backfill.image_features(blob(row, 'archive'))[3]
            with self.subTest(message_id=message_id):
                self.assertIsNotNone(backfill.jpeg_chroma_match(target, original))
                with patch.object(backfill, 'VISUAL_CHROMA_EDGE_MIN_SHARED_OUTLIER_FRACTION', 1.0):
                    self.assertIsNone(backfill.jpeg_chroma_match(target, original))
                with patch.object(backfill, 'VISUAL_CHROMA_EDGE_MAX_BLURRED_RMS', 3.25):
                    self.assertIsNone(backfill.jpeg_chroma_match(target, original))
                if message_id in (3444, 2793):
                    with patch.object(backfill, 'VISUAL_CHROMA_EDGE_MAX_BLURRED_OUTLIER_FRACTION', 0.01):
                        self.assertIsNone(backfill.jpeg_chroma_match(target, original))

    def test_rendering_variants_do_not_pass_generic_thresholds_or_wrong_post_ids(self):
        matcher = self.matcher()
        for row in ROWS:
            if not row['manual_confirmation']:
                continue
            with self.subTest(message_id=row['message_id']):
                data = blob(row, 'telegram')
                target = backfill.image_features(data)
                original = backfill.image_features(blob(row, 'archive'))
                self.assertEqual(backfill.choose_match(target, [(row['name'], original)], allow_visual=True),
                                 (None, 'unmatched'))
                date = dt.datetime.fromisoformat(row['date']).date()
                self.assertNotIn('manually verified', matcher.match(data, date)[1])
                self.assertNotIn('manually verified', matcher.match(data, date, message_id=-1)[1])
                self.assertNotIn('manually verified', matcher.match(data, date + dt.timedelta(days=1),
                                                                    message_id=row['message_id'])[1])

    def test_manual_confirmations_reject_changed_telegram_bytes(self):
        matcher = self.matcher()
        for row in ROWS:
            if row['manual_confirmation']:
                with self.subTest(message_id=row['message_id']):
                    data = blob(row, 'telegram') + b'\nchanged metadata'
                    result = matcher.match(data, dt.datetime.fromisoformat(row['date']).date(),
                                           message_id=row['message_id'])
                    self.assertNotIn('manually verified', result[1])

    def test_manual_confirmations_reject_changed_archive_bytes(self):
        for row in ROWS:
            if not row['manual_confirmation']:
                continue
            with self.subTest(message_id=row['message_id']):
                name = row['name']
                path = self.root / 'panzer-archiv-00/images' / name[:4] / name[5:7] / name
                path.write_bytes(blob(row, 'archive') + b'\nchanged metadata')
                result = self.matcher().match(blob(row, 'telegram'), dt.datetime.fromisoformat(row['date']).date(),
                                               message_id=row['message_id'])
                self.assertNotIn('manually verified', result[1])
                path.write_bytes(blob(row, 'archive'))

    def test_confirmations_are_channel_scoped_and_part_of_the_search_signature(self):
        matcher = self.matcher()
        before = matcher.plan_signature()
        matcher.verified_matches = {}
        self.assertNotEqual(matcher.plan_signature(), before)
        with patch.object(backfill.sync, 'CHANNEL_NAME', '@OtherChannel'):
            self.assertEqual(backfill.load_verified_matches(), {})
            matcher = self.matcher()
            row = ROWS[0]
            self.assertEqual(matcher.match(blob(row, 'telegram'), dt.datetime.fromisoformat(row['date']).date(),
                                          message_id=row['message_id']), (None, 'unmatched'))

    def test_invalid_confirmation_records_fail_instead_of_silently_overwriting(self):
        record = dict(backfill.load_verified_matches()[13380])
        invalid = [dict(record, message_id='13380'), dict(record, date='not a date'),
                   dict(record, name='../image.jpg'), dict(record, telegram_sha256='bad')]
        path = self.root / 'approvals.json'
        for rows in [[record, record]] + [[item] for item in invalid]:
            with self.subTest(rows=rows):
                path.write_text(json.dumps({'channel': 'RosaroterPanzerBackup', 'matches': rows}))
                with patch.object(backfill, 'VERIFIED_MATCHES_PATH', path), self.assertRaises(ValueError):
                    backfill.load_verified_matches()

    def test_missing_confirmation_file_disables_only_manual_approvals(self):
        with patch.object(backfill, 'VERIFIED_MATCHES_PATH', self.root / 'missing.json'):
            self.assertEqual(backfill.load_verified_matches(), {})
            matcher = self.matcher()
        row = next(row for row in ROWS if not row['manual_confirmation'])
        self.assertEqual(matcher.match(blob(row, 'telegram'), dt.datetime.fromisoformat(row['date']).date())[0],
                         row['name'])

    def test_confirmations_do_not_override_date_windows_or_exact_match_precedence(self):
        row = next(row for row in ROWS if row['message_id'] == 3782)
        date = dt.datetime.fromisoformat(row['date']).date()
        self.assertEqual(self.matcher(days=14).match(blob(row, 'telegram'), date, message_id=3782), (None, 'unmatched'))
        self.assertEqual(self.matcher(days=15).match(blob(row, 'telegram'), date, message_id=3782)[0], row['name'])
        matcher = self.matcher()
        matcher.verified_matches[3782]['telegram_sha256'] = row['archive_sha256']
        self.assertEqual(matcher.match(blob(row, 'archive'), date, message_id=3782), (row['name'], 'bytes'))

    def test_changed_content_is_rejected_even_with_a_confirmed_message_id(self):
        matcher = self.matcher()
        for row in ROWS:
            with self.subTest(message_id=row['message_id']):
                with Image.open(io.BytesIO(blob(row, 'archive'))) as source:
                    edited = source.convert('RGB')
                box = (0, 0, edited.width - 1, max(1, edited.height // 5))
                mean = sum(ImageStat.Stat(edited.crop(box)).mean) / 3
                ImageDraw.Draw(edited).rectangle(box, fill='black' if mean >= 128 else 'white')
                output = io.BytesIO()
                edited.save(output, format='JPEG', quality=85)
                self.assertEqual(matcher.match(output.getvalue(), dt.datetime.fromisoformat(row['date']).date(),
                                               message_id=row['message_id']), (None, 'unmatched'))

    def test_all_43_gap_links_export_after_replanning_and_reuse_cached_photos(self):
        posts = []
        data = {row['message_id']: blob(row, 'telegram') for row in ROWS}
        for row in ROWS:
            msg = message(row['message_id'])
            msg.date = dt.datetime.fromisoformat(row['date'])
            width, height = row['telegram_size']
            msg.photo.sizes = [SimpleNamespace(type=row['variant'], w=width, h=height, size=len(data[msg.id]))]
            posts.append(msg)
        client = FakeClient(posts)
        client.download_media = AsyncMock(side_effect=lambda photo, *args, **kwargs: data[photo.id])
        known = {'name': 'existing.jpg', 'trct': 17, 'tview': 300}
        messages, state = {99999: dict(known)}, {}
        output = io.StringIO()
        with patch.object(backfill, 'STATE_PATH', self.root / 'state.json'), \
             patch.object(backfill.sync, 'MESSAGES_CACHE_PATH', self.root / 'messages.json'), \
             patch.object(backfill.sync, 'GALLERY_METADATA_PATH', self.root / 'metadata.json'), \
             patch.object(backfill, 'download', side_effect=AssertionError('unexpected HTTP download')), \
             contextlib.redirect_stdout(output):
            with patch.multiple(backfill, **OLD_SETTINGS), patch.object(backfill, 'load_verified_matches', return_value={}):
                old = self.matcher()
                asyncio.run(backfill.backfill_history(client, 'channel', messages, state, old,
                                                     limit=None, photo_cache_dir=self.root / 'photos'))
            self.assertTrue(all(messages[row['message_id']]['name'] is None for row in ROWS))
            self.assertEqual(state['history_plan']['window_index'], len(state['history_plan']['windows']))
            asyncio.run(backfill.backfill_history(client, 'channel', messages, state, self.matcher(),
                                                 limit=None, photo_cache_dir=self.root / 'photos'))
        self.assertEqual(client.download_media.await_count, 43)
        self.assertEqual(messages[99999], known)
        self.assertEqual(state['history_plan']['remaining'], [])
        self.assertEqual(output.getvalue().count('Telegram photo cache hit:'), 43)
        metadata = json.loads((self.root / 'metadata.json').read_text())
        for row in ROWS:
            record = messages[row['message_id']]
            self.assertEqual(record['name'], row['name'])
            self.assertEqual(record['date'], row['date'])
            self.assertEqual(metadata[row['name']][0], row['message_id'])


if __name__ == '__main__':
    unittest.main()
