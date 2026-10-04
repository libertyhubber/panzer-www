import asyncio
import datetime as dt
import io
import json
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image, ImageChops, ImageDraw, ImageStat
from telethon.tl.types import MessageMediaPhoto

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import backfill_telegram as backfill


def photo_blob(color='white', format='PNG', **options):
    image = Image.new('RGB', (160, 120), color)
    ImageDraw.Draw(image).rectangle((20, 30, 80, 90), fill='blue')
    output = io.BytesIO()
    image.save(output, format=format, **options)
    return output.getvalue()


def message(message_id, *, photo=True):
    if photo is True:
        photo = SimpleNamespace(id=message_id, dc_id=2, video_sizes=None, sizes=[
            SimpleNamespace(type='m', w=320, h=240, size=1000),
            SimpleNamespace(type='x', w=1280, h=960, size=200000),
        ])
    return SimpleNamespace(
        id=message_id, photo=photo, media=MessageMediaPhoto(photo) if photo else None,
        date=dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc),
        forwards=2, reactions=SimpleNamespace(results=[SimpleNamespace(count=3), SimpleNamespace(count=4)]),
        views=123, replies=SimpleNamespace(replies=0),
    )


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.downloads = []
        self.requests = []
        self.fail_id = None
        self.hang_id = None
        self.download_cancelled = False
        self.selected_thumbs = []
        self.history_requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get_entity(self, channel):
        return SimpleNamespace(id=1)

    async def iter_messages(self, channel, *, reverse, offset_id, offset_date, limit):
        assert not reverse
        self.history_requests.append((offset_id, offset_date, limit))
        selected = sorted((msg for msg in self.messages if (not offset_id or msg.id < offset_id)
                           and msg.date < offset_date),
                          key=lambda msg: msg.id, reverse=True)
        for msg in selected if limit is None else selected[:limit]:
            yield msg

    async def download_media(self, photo, result_type, *, thumb, progress_callback):
        self.downloads.append(photo.id)
        self.selected_thumbs.append(thumb)
        assert thumb in [size.type for size in photo.sizes]
        if photo.id == self.fail_id:
            raise RuntimeError('download failed')
        if photo.id == self.hang_id:
            try:
                await asyncio.Event().wait()
            finally:
                self.download_cancelled = True
        blob = photo_blob()
        progress_callback(len(blob), len(blob))
        return blob

    async def get_messages(self, channel, *, ids):
        self.requests.append(ids)
        lookup = {msg.id: msg for msg in self.messages}
        return [lookup.get(key) for key in ids]


class FakeMatcher(backfill.ArchiveMatcher):
    def __init__(self, targets=None, days=35):
        self.days = days
        self.allow_visual = True
        self.image_dates = targets if targets is not None else {
            name: dt.date(2022, 1, 1) for name in ['old.jpg', 'recovered.jpg', 'visual.jpg', 'pending.jpg', 'known.jpg']
        }
        self.by_date = backfill.defaultdict(list)
        for name, date in self.image_dates.items():
            self.by_date[date].append((name, 'unused'))
        self.match = Mock()


class MatchTests(unittest.TestCase):
    def real_13457_pair(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_13457'
        return (fixtures / 'telegram.jpg').read_bytes(), (fixtures / 'archive.jpg').read_bytes()

    def test_real_message_13457_matches_manually_verified_archive_only_with_visual_opt_in(self):
        telegram, archive = self.real_13457_pair()
        target = backfill.image_features(telegram)
        original = backfill.image_features(archive)
        name = '2024-08-10T140659_f134bf89152ff368zu2l2.jpg'
        # These must remain the actual differently encoded JPEGs, not synthetic
        # copies or exact pixel identities that bypass the visual algorithm.
        self.assertNotEqual(target[0], original[0])
        self.assertNotEqual(target[1], original[1])
        self.assertEqual(backfill.choose_match(target, [(name, original)]), (None, 'unmatched'))
        candidates = [('unrelated.jpg', backfill.image_features(photo_blob('black'))),
                      (name, original)]
        matched, status = backfill.choose_match(target, candidates, allow_visual=True)
        self.assertEqual(matched, name)
        self.assertTrue(status.startswith('visual (RMS='), status)

    def test_real_message_13457_rejects_duplicate_visual_candidates_as_ambiguous(self):
        telegram, archive = self.real_13457_pair()
        original = backfill.image_features(archive)
        self.assertEqual(backfill.choose_match(backfill.image_features(telegram),
                                              [('original.jpg', original), ('duplicate.jpg', original)],
                                              allow_visual=True), (None, 'ambiguous'))

    def test_real_message_13457_matches_via_archive_indexes_and_post_date(self):
        telegram, archive = self.real_13457_pair()
        name = '2024-08-10T140659_f134bf89152ff368zu2l2.jpg'
        post_date = dt.datetime.fromisoformat('2024-08-10T19:34:11+00:00').date()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2024/08': 2}))
            month = root / 'panzer-archiv-00/images/2024/08'
            month.mkdir(parents=True)
            entries = [{'name': '2024-08-10_unrelated.jpg'}, {'name': name}]
            (month / 'entry_index.json').write_text(json.dumps(entries))
            (month / name).write_bytes(archive)
            (month / '2024-08-10_unrelated.jpg').write_bytes(photo_blob('black'))
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                matcher = backfill.ArchiveMatcher(images, days=3, allow_visual=True)
                matched, status = matcher.match(telegram, post_date)
                self.assertEqual(matched, name)
                self.assertTrue(status.startswith('visual'), status)
                exact_matcher = backfill.ArchiveMatcher(images, days=3, allow_visual=False)
                self.assertEqual(exact_matcher.match(telegram, post_date), (None, 'unmatched'))

    def test_real_message_11986_matches_despite_jpeg_chroma_recompression(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_11986'
        target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())
        original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())
        name = '2024-04-22_01-31-55.jpg'
        self.assertNotEqual(target[0], original[0])
        self.assertNotEqual(target[1], original[1])
        difference = ImageChops.difference(target[3], original[3])
        # The old gates incorrectly rejected this genuine pair; retain the actual
        # recompression that exercises both relaxed gates rather than exact pixels.
        self.assertGreater(max(ImageStat.Stat(difference).rms), 2.5)
        histogram = difference.histogram()
        outliers = sum(sum(histogram[channel * 256 + 13:(channel + 1) * 256]) for channel in range(3))
        self.assertGreater(outliers / (128 * 128 * 3), 0.001)
        self.assertEqual(backfill.choose_match(target, [(name, original)]), (None, 'unmatched'))
        matched, status = backfill.choose_match(target, [(name, original)], allow_visual=True)
        self.assertEqual(matched, name)
        self.assertTrue(status.startswith('visual (RMS='), status)
        self.assertEqual(backfill.choose_match(target, [(name, original), ('duplicate.jpg', original)],
                                              allow_visual=True), (None, 'ambiguous'))

    def test_real_message_11986_matches_via_date_window_without_network(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_11986'
        telegram = (fixtures / 'telegram.jpg').read_bytes()
        name = '2024-04-22_01-31-55.jpg'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2024/04': 2}))
            month = root / 'panzer-archiv-00/images/2024/04'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(json.dumps([{'name': name}]))
            (month / name).write_bytes((fixtures / 'archive.jpg').read_bytes())
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                matcher = backfill.ArchiveMatcher(images, days=3, allow_visual=True)
                matched, status = matcher.match(telegram, dt.date(2024, 4, 22))
                self.assertEqual(matched, name)
                self.assertTrue(status.startswith('visual'), status)

    def test_visual_rms_gate_rejects_broad_difference_with_no_outliers(self):
        target = ('target bytes', 'target pixels', 1.0, Image.new('RGB', (128, 128), (100, 100, 100)))
        features = ('other bytes', 'other pixels', 1.0, Image.new('RGB', (128, 128), (104, 100, 100)))
        self.assertEqual(backfill.choose_match(target, [('changed.jpg', features)], allow_visual=True),
                         (None, 'unmatched'))

    def test_visual_outlier_gate_rejects_local_edits_with_low_average_rms(self):
        preview = Image.new('RGB', (128, 128), (100, 100, 100))
        target = ('target bytes', 'target pixels', 1.0, preview)
        changed = preview.copy()
        # 40 strongly changed pixels have low overall RMS but exceed 0.2% of
        # channel samples. This must not pass just because the average is close.
        ImageDraw.Draw(changed).rectangle((0, 0, 7, 4), fill=(116, 116, 116))
        features = ('other bytes', 'other pixels', 1.0, changed)
        self.assertLess(max(ImageStat.Stat(ImageChops.difference(preview, changed)).rms), 3.0)
        self.assertEqual(backfill.choose_match(target, [('edited.jpg', features)], allow_visual=True),
                         (None, 'unmatched'))

    def test_real_message_11444_matches_jpeg_chroma_edges_with_visual_opt_in(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_11444'
        target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())
        original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())
        name = '2024-03-07_C3275E5B.jpg'
        self.assertNotEqual(target[0], original[0])
        self.assertNotEqual(target[1], original[1])
        self.assertGreater(max(ImageStat.Stat(ImageChops.difference(target[3], original[3])).rms),
                           backfill.VISUAL_MAX_RMS)
        self.assertEqual(backfill.choose_match(target, [(name, original)]), (None, 'unmatched'))
        matched, status = backfill.choose_match(target, [(name, original)], allow_visual=True)
        self.assertEqual(matched, name)
        self.assertIn('; Y=', status)
        self.assertIn('; chroma=', status)
        self.assertEqual(backfill.choose_match(target, [(name, original), ('duplicate.jpg', original)],
                                              allow_visual=True), (None, 'ambiguous'))

    def test_real_message_11444_does_not_match_same_template_with_removed_caption(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_11444'
        target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())
        with Image.open(fixtures / 'archive.jpg') as source:
            edited = source.convert('RGB')
        ImageDraw.Draw(edited).rectangle((0, 0, edited.width - 1, 80), fill='white')
        output = io.BytesIO()
        edited.save(output, format='JPEG', quality=85)
        altered = backfill.image_features(output.getvalue())
        self.assertEqual(backfill.choose_match(target, [('wrong-caption.jpg', altered)], allow_visual=True),
                         (None, 'unmatched'))

    def test_real_message_11444_gap_plan_and_match_use_actual_post_date(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_11444'
        name = '2024-03-07_C3275E5B.jpg'
        post_date = dt.date(2024, 3, 5)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2024/03': 1}))
            month = root / 'panzer-archiv-00/images/2024/03'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(json.dumps([{'name': name}]))
            (month / name).write_bytes((fixtures / 'archive.jpg').read_bytes())
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                matcher = backfill.ArchiveMatcher(images, days=3, allow_visual=True)
                pending = matcher.unlinked_names({11444: {'name': None, 'match_status': 'unmatched'}})
                self.assertEqual(pending, {name})
                self.assertEqual(matcher.search_windows(pending), [(dt.date(2024, 3, 4), dt.date(2024, 3, 10))])
                self.assertTrue(matcher.has_pending(post_date, pending))
                matched, status = matcher.match((fixtures / 'telegram.jpg').read_bytes(), post_date)
                self.assertEqual(matched, name)
                self.assertIn('; chroma=', status)
                too_narrow = backfill.ArchiveMatcher(images, days=1, allow_visual=True)
                self.assertFalse(too_narrow.has_pending(post_date, pending))
                self.assertEqual(too_narrow.match((fixtures / 'telegram.jpg').read_bytes(), post_date),
                                 (None, 'unmatched'))

    def test_real_message_9571_matches_with_small_jpeg_rounding_bias(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_9571'
        telegram = (fixtures / 'telegram.jpg').read_bytes()
        target = backfill.image_features(telegram)
        original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())
        name = '2023-09-26_34ECB615.jpg'
        self.assertEqual(backfill.sync.digest_img(telegram), '05a4f56f4063')
        self.assertNotEqual(target[0], original[0])
        self.assertNotEqual(target[1], original[1])
        self.assertGreater(max(ImageStat.Stat(ImageChops.difference(target[3], original[3])).rms),
                           backfill.VISUAL_MAX_RMS)
        target_mean = ImageStat.Stat(target[3].convert('YCbCr')).mean
        original_mean = ImageStat.Stat(original[3].convert('YCbCr')).mean
        self.assertGreater(max(abs(a - b) for a, b in zip(target_mean, original_mean)), 0.5)
        with patch.object(backfill, 'VISUAL_MAX_CHANNEL_MEAN_SHIFT', 0.5):
            self.assertEqual(backfill.choose_match(target, [(name, original)], allow_visual=True),
                             (None, 'unmatched'))
        self.assertEqual(backfill.choose_match(target, [(name, original)]), (None, 'unmatched'))
        matched, status = backfill.choose_match(target, [(name, original)], allow_visual=True)
        self.assertEqual(matched, name)
        self.assertIn('; Y=', status)
        self.assertIn('; chroma=', status)
        self.assertEqual(backfill.choose_match(target, [(name, original), ('duplicate.jpg', original)],
                                              allow_visual=True), (None, 'ambiguous'))

    def test_real_message_9571_gap_match_handles_one_day_archive_offset(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_9571'
        name = '2023-09-26_34ECB615.jpg'
        post_date = dt.date(2023, 9, 25)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2023/09': 1}))
            month = root / 'panzer-archiv-00/images/2023/09'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(json.dumps([{'name': name}]))
            (month / name).write_bytes((fixtures / 'archive.jpg').read_bytes())
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                matcher = backfill.ArchiveMatcher(images, days=3, allow_visual=True)
                pending = matcher.unlinked_names({9571: {'name': None, 'match_status': 'unmatched'}})
                self.assertEqual(pending, {name})
                self.assertTrue(matcher.has_pending(post_date, pending))
                matched, status = matcher.match((fixtures / 'telegram.jpg').read_bytes(), post_date)
                self.assertEqual(matched, name)
                self.assertIn('; chroma=', status)

    def test_real_message_8513_already_passes_visual_comparison(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_8513'
        target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())
        original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())
        name = '2023-06-27_4B728817.jpg'
        self.assertNotEqual(target[0], original[0])
        self.assertNotEqual(target[1], original[1])
        self.assertEqual(backfill.choose_match(target, [(name, original)]), (None, 'unmatched'))
        matched, status = backfill.choose_match(target, [(name, original)], allow_visual=True)
        self.assertEqual(matched, name)
        self.assertTrue(status.startswith('visual (RMS='), status)
        self.assertNotIn('; chroma=', status)

    def test_real_message_8513_default_window_handles_four_day_archive_lag(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_8513'
        telegram = (fixtures / 'telegram.jpg').read_bytes()
        name = '2023-06-27_4B728817.jpg'
        post_date = dt.date(2023, 6, 23)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2023/06': 1}))
            month = root / 'panzer-archiv-00/images/2023/06'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(json.dumps([{'name': name}]))
            (month / name).write_bytes((fixtures / 'archive.jpg').read_bytes())
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                matcher = backfill.ArchiveMatcher(images, allow_visual=True)
                pending = matcher.unlinked_names({8513: {'name': None, 'match_status': 'unmatched'}})
                self.assertEqual(matcher.days, 7)
                self.assertEqual(matcher.search_windows(pending), [(dt.date(2023, 6, 20), dt.date(2023, 7, 4))])
                self.assertTrue(matcher.has_pending(post_date, pending))
                self.assertEqual(matcher.match(telegram, post_date)[0], name)
                narrow = backfill.ArchiveMatcher(images, days=3, allow_visual=True)
                self.assertFalse(narrow.has_pending(post_date, pending))
                self.assertEqual(narrow.match(telegram, post_date), (None, 'unmatched'))
                minimum = backfill.ArchiveMatcher(images, days=4, allow_visual=True)
                self.assertEqual(minimum.match(telegram, post_date)[0], name)

    def test_real_message_8347_matches_modest_chroma_recompression(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_8347'
        target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())
        original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())
        name = '2023-06-13_31E2BD1B.jpg'
        self.assertNotEqual(target[0], original[0])
        self.assertNotEqual(target[1], original[1])
        difference = ImageStat.Stat(ImageChops.difference(target[3].convert('YCbCr'),
                                                        original[3].convert('YCbCr')))
        self.assertLess(difference.rms[0], backfill.VISUAL_LUMA_MAX_RMS)
        self.assertGreater(max(difference.rms[1:]), 2.5)
        with patch.object(backfill, 'VISUAL_CHROMA_MAX_RMS', 2.5):
            self.assertEqual(backfill.choose_match(target, [(name, original)], allow_visual=True),
                             (None, 'unmatched'))
        self.assertEqual(backfill.choose_match(target, [(name, original)]), (None, 'unmatched'))
        matched, status = backfill.choose_match(target, [(name, original)], allow_visual=True)
        self.assertEqual(matched, name)
        self.assertIn('; Y=', status)
        self.assertIn('; chroma=', status)
        self.assertEqual(backfill.choose_match(target, [(name, original), ('duplicate.jpg', original)],
                                              allow_visual=True), (None, 'ambiguous'))

    def test_real_message_8347_default_gap_window_handles_five_day_archive_lag(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_8347'
        telegram = (fixtures / 'telegram.jpg').read_bytes()
        name = '2023-06-13_31E2BD1B.jpg'
        post_date = dt.date(2023, 6, 8)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2023/06': 1}))
            month = root / 'panzer-archiv-00/images/2023/06'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(json.dumps([{'name': name}]))
            (month / name).write_bytes((fixtures / 'archive.jpg').read_bytes())
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                matcher = backfill.ArchiveMatcher(images, allow_visual=True)
                pending = matcher.unlinked_names({8347: {'name': None, 'match_status': 'unmatched'}})
                self.assertEqual(matcher.search_windows(pending), [(dt.date(2023, 6, 6), dt.date(2023, 6, 20))])
                self.assertTrue(matcher.has_pending(post_date, pending))
                self.assertEqual(matcher.match(telegram, post_date)[0], name)
                narrow = backfill.ArchiveMatcher(images, days=4, allow_visual=True)
                self.assertFalse(narrow.has_pending(post_date, pending))
                self.assertEqual(narrow.match(telegram, post_date), (None, 'unmatched'))
                minimum = backfill.ArchiveMatcher(images, days=5, allow_visual=True)
                self.assertEqual(minimum.match(telegram, post_date)[0], name)

    def test_real_message_8852_full_resolution_matches_but_larger_byte_thumbnail_does_not(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_8852'
        full_blob = (fixtures / 'telegram.jpg').read_bytes()
        thumbnail_blob = (fixtures / 'thumbnail.jpg').read_bytes()
        original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())
        full, thumbnail = backfill.image_features(full_blob), backfill.image_features(thumbnail_blob)
        name = '2023-07-24_B6866461.jpg'
        self.assertGreater(len(thumbnail_blob), len(full_blob))
        self.assertNotEqual(full[0], original[0])
        self.assertNotEqual(full[1], original[1])
        self.assertEqual(backfill.choose_match(full, [(name, original)]), (None, 'unmatched'))
        self.assertEqual(backfill.choose_match(thumbnail, [(name, original)], allow_visual=True),
                         (None, 'unmatched'))
        matched, status = backfill.choose_match(full, [(name, original)], allow_visual=True)
        self.assertEqual(matched, name)
        self.assertTrue(status.startswith('visual (RMS='), status)
        self.assertNotIn('; chroma=', status)
        self.assertEqual(backfill.choose_match(full, [(name, original), ('duplicate.jpg', original)],
                                              allow_visual=True), (None, 'ambiguous'))

    def test_real_message_10029_matches_jpeg_recompression_with_visual_opt_in(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_10029'
        telegram = (fixtures / 'telegram.jpg').read_bytes()
        target = backfill.image_features(telegram)
        original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())
        name = '2023-11-06_9CFC1942.jpg'
        self.assertEqual(backfill.sync.digest_img(telegram), '26d890cd08f4')
        self.assertNotEqual(target[0], original[0])
        self.assertNotEqual(target[1], original[1])
        self.assertEqual(target[2], original[2])
        difference = ImageChops.difference(target[3].convert('YCbCr'), original[3].convert('YCbCr'))
        rms = ImageStat.Stat(difference).rms
        # Both former gates reject these actual JPEGs, independently. Other
        # gates (aspect ratio, outliers and channel means) must remain intact.
        self.assertGreater(rms[0], 1.5)
        self.assertGreater(max(rms[1:]), 3.0)
        with patch.object(backfill, 'VISUAL_LUMA_MAX_RMS', 1.5):
            self.assertEqual(backfill.choose_match(target, [(name, original)], allow_visual=True),
                             (None, 'unmatched'))
        with patch.object(backfill, 'VISUAL_CHROMA_MAX_RMS', 3.0):
            self.assertEqual(backfill.choose_match(target, [(name, original)], allow_visual=True),
                             (None, 'unmatched'))
        self.assertEqual(backfill.choose_match(target, [(name, original)]), (None, 'unmatched'))
        matched, status = backfill.choose_match(target, [(name, original)], allow_visual=True)
        self.assertEqual(matched, name)
        self.assertIn('; Y=', status)
        self.assertIn('; chroma=', status)
        self.assertEqual(backfill.choose_match(target, [(name, original), ('duplicate.jpg', original)],
                                              allow_visual=True), (None, 'ambiguous'))

    def test_real_message_10029_rejects_same_image_with_removed_caption(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_10029'
        target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())
        with Image.open(fixtures / 'archive.jpg') as source:
            edited = source.convert('RGB')
        ImageDraw.Draw(edited).rectangle((270, 20, 623, 110), fill='white')
        output = io.BytesIO()
        edited.save(output, format='JPEG', quality=85)
        self.assertEqual(backfill.choose_match(target, [('wrong-caption.jpg',
                                                        backfill.image_features(output.getvalue()))],
                                              allow_visual=True), (None, 'unmatched'))

    def test_real_message_1848_matches_with_bounded_luminance_bias(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_1848'
        telegram = (fixtures / 'telegram.jpg').read_bytes()
        target = backfill.image_features(telegram)
        original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())
        name = '2021-11-26_8766DFD4.jpg'
        self.assertEqual(backfill.sync.digest_img(telegram), 'd6455a088290')
        self.assertNotEqual(target[0], original[0])
        self.assertNotEqual(target[1], original[1])
        self.assertEqual(target[2], original[2])
        target_ycc, original_ycc = target[3].convert('YCbCr'), original[3].convert('YCbCr')
        shifts = [abs(a - b) for a, b in zip(ImageStat.Stat(target_ycc).mean,
                                            ImageStat.Stat(original_ycc).mean)]
        self.assertGreater(shifts[0], 1.0)
        self.assertLess(shifts[0], backfill.VISUAL_MAX_LUMA_MEAN_SHIFT)
        self.assertLess(max(shifts[1:]), backfill.VISUAL_MAX_CHANNEL_MEAN_SHIFT)
        rms = ImageStat.Stat(ImageChops.difference(target_ycc, original_ycc)).rms
        self.assertLess(rms[0], backfill.VISUAL_LUMA_MAX_RMS)
        self.assertLess(max(rms[1:]), backfill.VISUAL_CHROMA_MAX_RMS)
        with patch.object(backfill, 'VISUAL_MAX_LUMA_MEAN_SHIFT', 1.0):
            self.assertEqual(backfill.choose_match(target, [(name, original)], allow_visual=True),
                             (None, 'unmatched'))
        self.assertEqual(backfill.choose_match(target, [(name, original)]), (None, 'unmatched'))
        matched, status = backfill.choose_match(target, [(name, original)], allow_visual=True)
        self.assertEqual(matched, name)
        self.assertIn('; Y=', status)
        self.assertEqual(backfill.choose_match(target, [(name, original), ('duplicate.jpg', original)],
                                              allow_visual=True), (None, 'ambiguous'))

    def test_real_message_1848_requires_57_day_window_and_survives_indexing(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_1848'
        name = '2021-11-26_8766DFD4.jpg'
        telegram = (fixtures / 'telegram.jpg').read_bytes()
        date = dt.date(2022, 1, 22)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2021/11': 1}))
            month = root / 'panzer-archiv-00/images/2021/11'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(json.dumps([{'name': name}]))
            (month / name).write_bytes((fixtures / 'archive.jpg').read_bytes())
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                for days in (56, 57, 90):
                    with self.subTest(days=days):
                        matcher = backfill.ArchiveMatcher(images, days=days, allow_visual=True)
                        matched, status = matcher.match(telegram, date)
                        if days == 56:
                            self.assertEqual((matched, status), (None, 'unmatched'))
                        else:
                            self.assertEqual(matched, name)
                            self.assertIn('; Y=', status)

    def test_real_message_1848_rejects_removed_caption(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_1848'
        target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())
        with Image.open(fixtures / 'archive.jpg') as source:
            edited = source.convert('RGB')
        ImageDraw.Draw(edited).rectangle((4, 1178, 562, 1279), fill='black')
        output = io.BytesIO()
        edited.save(output, format='JPEG', quality=85)
        self.assertEqual(backfill.choose_match(target, [('wrong-caption.jpg',
                                                        backfill.image_features(output.getvalue()))],
                                              allow_visual=True), (None, 'unmatched'))

    def test_real_message_1853_matches_with_bounded_chroma_bias(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_1853'
        telegram = (fixtures / 'telegram.jpg').read_bytes()
        target = backfill.image_features(telegram)
        original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())
        name = '2021-11-27_3B64D3C0.jpg'
        self.assertEqual(backfill.sync.digest_img(telegram), '4b2b25492000')
        self.assertNotEqual(target[0], original[0])
        self.assertNotEqual(target[1], original[1])
        self.assertEqual(target[2], original[2])
        target_ycc, original_ycc = target[3].convert('YCbCr'), original[3].convert('YCbCr')
        shifts = [abs(a - b) for a, b in zip(ImageStat.Stat(target_ycc).mean,
                                            ImageStat.Stat(original_ycc).mean)]
        self.assertLess(shifts[0], backfill.VISUAL_MAX_LUMA_MEAN_SHIFT)
        self.assertGreater(shifts[1], 1.0)
        self.assertLess(max(shifts[1:]), backfill.VISUAL_MAX_CHANNEL_MEAN_SHIFT)
        rms = ImageStat.Stat(ImageChops.difference(target_ycc, original_ycc)).rms
        self.assertLess(rms[0], backfill.VISUAL_LUMA_MAX_RMS)
        self.assertLess(max(rms[1:]), backfill.VISUAL_CHROMA_MAX_RMS)
        with patch.object(backfill, 'VISUAL_MAX_CHANNEL_MEAN_SHIFT', 1.0):
            self.assertEqual(backfill.choose_match(target, [(name, original)], allow_visual=True),
                             (None, 'unmatched'))
        self.assertEqual(backfill.choose_match(target, [(name, original)]), (None, 'unmatched'))
        matched, status = backfill.choose_match(target, [(name, original)], allow_visual=True)
        self.assertEqual(matched, name)
        self.assertIn('; chroma=', status)
        self.assertEqual(backfill.choose_match(target, [(name, original), ('duplicate.jpg', original)],
                                              allow_visual=True), (None, 'ambiguous'))

    def test_real_message_1853_requires_57_day_window_and_survives_indexing(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_1853'
        name = '2021-11-27_3B64D3C0.jpg'
        telegram = (fixtures / 'telegram.jpg').read_bytes()
        date = dt.date(2022, 1, 23)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2021/11': 1}))
            month = root / 'panzer-archiv-00/images/2021/11'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(json.dumps([{'name': name}]))
            (month / name).write_bytes((fixtures / 'archive.jpg').read_bytes())
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                for days in (56, 57, 90):
                    with self.subTest(days=days):
                        matcher = backfill.ArchiveMatcher(images, days=days, allow_visual=True)
                        matched, status = matcher.match(telegram, date)
                        if days == 56:
                            self.assertEqual((matched, status), (None, 'unmatched'))
                        else:
                            self.assertEqual(matched, name)
                            self.assertIn('; chroma=', status)

    def test_real_message_1853_rejects_removed_caption(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_1853'
        target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())
        with Image.open(fixtures / 'archive.jpg') as source:
            edited = source.convert('RGB')
        ImageDraw.Draw(edited).rectangle((0, 413, 679, 605), fill='white')
        output = io.BytesIO()
        edited.save(output, format='JPEG', quality=85)
        self.assertEqual(backfill.choose_match(target, [('wrong-caption.jpg',
                                                        backfill.image_features(output.getvalue()))],
                                              allow_visual=True), (None, 'unmatched'))

    def test_jpeg_chroma_fallback_bounds_chroma_bias_independently(self):
        target = Image.new('RGB', (128, 128), (100, 100, 100))
        # RGB colors round-trip to YCbCr (99, 129, 128) and (99, 131, 128).
        # Seven sixteenths of the latter gives a Cb mean shift of exactly 1.875.
        changed = Image.new('RGB', (128, 128), (100, 99, 103))
        ImageDraw.Draw(changed).rectangle((0, 0, 127, 55), fill=(100, 98, 105))
        self.assertIsNotNone(backfill.jpeg_chroma_match(target, changed))
        # Just above the chroma mean limit; RMS/outliers and luma still pass.
        ImageDraw.Draw(changed).rectangle((0, 56, 127, 56), fill=(100, 98, 105))
        target_ycc, changed_ycc = target.convert('YCbCr'), changed.convert('YCbCr')
        difference = ImageStat.Stat(ImageChops.difference(target_ycc, changed_ycc))
        self.assertLess(difference.rms[0], backfill.VISUAL_LUMA_MAX_RMS)
        self.assertLess(max(difference.rms[1:]), backfill.VISUAL_CHROMA_MAX_RMS)
        self.assertIsNone(backfill.jpeg_chroma_match(target, changed))

    def test_jpeg_chroma_fallback_bounds_luminance_bias_independently(self):
        target = Image.new('RGB', (128, 128), (100, 100, 100))
        changed = Image.new('RGB', (128, 128), (102, 102, 102))
        self.assertIsNotNone(backfill.jpeg_chroma_match(target, changed))
        # Just over 2.0 mean shift, but still below the RMS/outlier limits.
        changed.putpixel((0, 0), (103, 103, 103))
        rms = ImageStat.Stat(ImageChops.difference(target, changed)).rms[0]
        self.assertLess(rms, backfill.VISUAL_LUMA_MAX_RMS)
        self.assertIsNone(backfill.jpeg_chroma_match(target, changed))

    def test_jpeg_chroma_fallback_rejects_larger_balanced_color_changes(self):
        target = Image.new('RGB', (128, 128), (100, 100, 100))
        changed_ycc = target.convert('YCbCr')
        draw = ImageDraw.Draw(changed_ycc)
        draw.rectangle((0, 0, 63, 127), fill=(100, 134, 128))
        draw.rectangle((64, 0, 127, 127), fill=(100, 122, 128))
        changed = changed_ycc.convert('RGB')
        target_ycc, candidate_ycc = target.convert('YCbCr'), changed.convert('YCbCr')
        difference = ImageStat.Stat(ImageChops.difference(target_ycc, candidate_ycc))
        self.assertLess(difference.rms[0], backfill.VISUAL_LUMA_MAX_RMS)
        self.assertGreater(max(difference.rms[1:]), backfill.VISUAL_CHROMA_MAX_RMS)
        means = zip(ImageStat.Stat(target_ycc).mean, ImageStat.Stat(candidate_ycc).mean)
        self.assertLessEqual(max(abs(a - b) for a, b in means), backfill.VISUAL_MAX_CHANNEL_MEAN_SHIFT)
        self.assertIsNone(backfill.jpeg_chroma_match(target, changed))

    def test_jpeg_chroma_fallback_rejects_broad_color_shift(self):
        target = Image.new('RGB', (128, 128), (100, 100, 100))
        changed = Image.new('RGB', (128, 128), (104, 100, 100))
        difference = ImageStat.Stat(ImageChops.difference(target.convert('YCbCr'), changed.convert('YCbCr')))
        self.assertLessEqual(difference.rms[0], backfill.VISUAL_LUMA_MAX_RMS)
        self.assertLessEqual(max(difference.rms[1:]), backfill.VISUAL_CHROMA_MAX_RMS)
        self.assertIsNone(backfill.jpeg_chroma_match(target, changed))

    def test_jpeg_chroma_fallback_rejects_local_content_edits_despite_low_rms(self):
        target = Image.new('RGB', (128, 128), (100, 100, 100))
        changed = target.copy()
        ImageDraw.Draw(changed).rectangle((0, 0, 7, 4), fill=(116, 116, 116))
        luma_rms = ImageStat.Stat(ImageChops.difference(target.convert('YCbCr'), changed.convert('YCbCr'))).rms[0]
        self.assertLess(luma_rms, backfill.VISUAL_LUMA_MAX_RMS)
        self.assertIsNone(backfill.jpeg_chroma_match(target, changed))

    def test_jpeg_chroma_fallback_rejects_local_recoloring_despite_low_rms(self):
        target = Image.new('RGB', (128, 128), (100, 100, 100))
        changed_ycc = target.convert('YCbCr')
        draw = ImageDraw.Draw(changed_ycc)
        draw.rectangle((0, 0, 9, 9), fill=(100, 144, 128))
        draw.rectangle((10, 0, 19, 9), fill=(100, 112, 128))
        changed = changed_ycc.convert('RGB')
        difference = ImageStat.Stat(ImageChops.difference(target.convert('YCbCr'), changed.convert('YCbCr')))
        self.assertLess(difference.rms[0], backfill.VISUAL_LUMA_MAX_RMS)
        self.assertLess(max(difference.rms[1:]), backfill.VISUAL_CHROMA_MAX_RMS)
        self.assertIsNone(backfill.jpeg_chroma_match(target, changed))

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

    def archive_fixture(self, root, year=2022):
        images = root / 'panzer-www/images'
        month = f'{year}/01'
        website_month = images / month
        website_month.mkdir(parents=True)
        (images / 'dir_index.json').write_text(json.dumps({month: 1}))
        name = f'{year}-01-01_original.jpg'
        entries = json.dumps([{'name': name}, {'name': 'thumbnails.jpg'}])
        return images, website_month, name, entries

    def test_archive_matching_automatically_uses_originals_and_date_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images, _, name, entries = self.archive_fixture(root)
            month = root / 'panzer-archiv-00/images/2022/01'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(entries)
            (month / name).write_bytes(photo_blob())
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                matcher = backfill.ArchiveMatcher(images, days=3, allow_visual=False)
                self.assertEqual(matcher.match(photo_blob(), dt.date(2022, 1, 4)), (name, 'bytes'))
                self.assertEqual(matcher.match(photo_blob(), dt.date(2022, 1, 5)), (None, 'unmatched'))

    def test_archive_index_is_preferred_over_stale_website_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images, website_month, name, entries = self.archive_fixture(root)
            (website_month / 'entry_index.json').write_text('[]')
            month = root / 'panzer-archiv-00/images/2022/01'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(entries)
            (month / name).write_bytes(photo_blob())
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                matcher = backfill.ArchiveMatcher(images, days=0, allow_visual=False)
                self.assertEqual(matcher.match(photo_blob(), dt.date(2022, 1, 1)), (name, 'bytes'))

    def test_missing_original_in_existing_checkout_falls_back_to_http(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images, _, name, entries = self.archive_fixture(root)
            month = root / 'panzer-archiv-00/images/2022/01'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(entries)
            with patch.object(backfill, 'download', return_value=photo_blob()) as download:
                matcher = backfill.ArchiveMatcher(images, days=0, allow_visual=False)
                self.assertEqual(matcher.match(photo_blob(), dt.date(2022, 1, 1)), (name, 'bytes'))
            download.assert_called_once_with(
                f'https://archiv0.derrosarotepanzer.com/images/2022/01/{name}', timeout=30, retries=3)

    def test_missing_checkout_uses_website_index_and_correct_remote_host(self):
        for year, number in [(2021, 0), (2024, 0), (2025, 1), (2026, 2), (2033, 9)]:
            with self.subTest(year=year), tempfile.TemporaryDirectory() as directory:
                images, website_month, name, entries = self.archive_fixture(Path(directory), year)
                (website_month / 'entry_index.json').write_text(entries)
                with patch.object(backfill, 'download', return_value=photo_blob()) as download:
                    matcher = backfill.ArchiveMatcher(images, days=0, allow_visual=False)
                    self.assertEqual(matcher.match(photo_blob(), dt.date(year, 1, 1)), (name, 'bytes'))
                download.assert_called_once_with(
                    f'https://archiv{number}.derrosarotepanzer.com/images/{year}/01/{name}',
                    timeout=30, retries=3)

    def test_missing_local_indexes_and_checkout_downloads_index_and_original(self):
        with tempfile.TemporaryDirectory() as directory:
            images, _, name, entries = self.archive_fixture(Path(directory), 2025)
            with patch.object(backfill, 'download', side_effect=[entries.encode(), photo_blob()]) as download:
                matcher = backfill.ArchiveMatcher(images, days=0, allow_visual=False)
                self.assertEqual(matcher.match(photo_blob(), dt.date(2025, 1, 1)), (name, 'bytes'))
            self.assertEqual([call.args[0] for call in download.call_args_list], [
                'https://archiv1.derrosarotepanzer.com/images/2025/01/entry_index.json',
                f'https://archiv1.derrosarotepanzer.com/images/2025/01/{name}',
            ])

    def test_year_mapping_selects_correct_sibling_checkout(self):
        for year, repo in [(2022, '00'), (2024, '00'), (2025, '01'), (2026, '02'), (2033, '09')]:
            with self.subTest(year=year), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                images, _, name, entries = self.archive_fixture(root, year)
                month = root / f'panzer-archiv-{repo}/images/{year}/01'
                month.mkdir(parents=True)
                (month / 'entry_index.json').write_text(entries)
                (month / name).write_bytes(photo_blob())
                with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                    matcher = backfill.ArchiveMatcher(images, days=0, allow_visual=False)
                    self.assertEqual(matcher.match(photo_blob(), dt.date(year, 1, 1)), (name, 'bytes'))


class VerifiedJanuary2022Tests(unittest.TestCase):
    PAIRS = (
        (2374, '2022-01-12_F26ACB26.jpg', '2022-03-04T18:24:34+00:00', (72, 102, 358, 260)),
        (2483, '2022-01-22_4C737E9E.jpg', '2022-03-11T21:06:20+00:00', (20, 445, 550, 486)),
        (2485, '2022-01-22_2B98D2AB.jpg', '2022-03-11T21:06:21+00:00', (12, 570, 498, 640)),
        (2499, '2022-01-23_E612B5E2.jpg', '2022-03-12T18:09:19+00:00', (0, 817, 499, 874)),
    )
    OLD_SETTINGS = dict(VISUAL_ALGORITHM_VERSION=8, VISUAL_LUMA_MAX_RMS=2.0,
                        VISUAL_CHROMA_MAX_RMS=3.25, VISUAL_MAX_CHANNEL_MEAN_SHIFT=1.25,
                        VISUAL_MAX_LUMA_MEAN_SHIFT=1.5)

    @staticmethod
    def fixtures(message_id):
        return Path(__file__).parent / f'fixtures/telegram_{message_id}'

    @classmethod
    def make_archive(cls, root):
        images, _, _, _ = MatchTests().archive_fixture(root)
        month = root / 'panzer-archiv-00/images/2022/01'
        month.mkdir(parents=True)
        (month / 'entry_index.json').write_text(json.dumps([{'name': name} for _, name, _, _ in cls.PAIRS]))
        for message_id, name, _, _ in cls.PAIRS:
            (month / name).write_bytes((cls.fixtures(message_id) / 'archive.jpg').read_bytes())
        return images

    def test_all_four_real_pairs_match_only_with_visual_opt_in_and_reject_duplicates(self):
        for message_id, name, _, _ in self.PAIRS:
            with self.subTest(message_id=message_id):
                fixtures = self.fixtures(message_id)
                target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())
                original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())
                self.assertNotEqual(target[0], original[0])
                self.assertNotEqual(target[1], original[1])
                self.assertEqual(target[2], original[2])
                self.assertEqual(backfill.choose_match(target, [(name, original)]), (None, 'unmatched'))
                with patch.multiple(backfill, **self.OLD_SETTINGS), \
                     patch.object(backfill, 'jpeg_chroma_edges_match', return_value=False):
                    self.assertEqual(backfill.choose_match(target, [(name, original)], allow_visual=True),
                                     (None, 'unmatched'))
                matched, status = backfill.choose_match(target, [(name, original)], allow_visual=True)
                self.assertEqual(matched, name)
                self.assertIn('; Y=', status)
                self.assertIn('; chroma=', status)
                self.assertEqual(backfill.choose_match(target, [(name, original), ('duplicate.jpg', original)],
                                                      allow_visual=True), (None, 'ambiguous'))

    def test_color_text_edge_pairs_require_the_edge_check(self):
        for message_id in (2483, 2485):
            with self.subTest(message_id=message_id):
                fixtures = self.fixtures(message_id)
                target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())[3]
                original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())[3]
                self.assertTrue(backfill.jpeg_chroma_edges_match(target.convert('YCbCr'), original.convert('YCbCr')))
                with patch.object(backfill, 'jpeg_chroma_edges_match', return_value=False):
                    self.assertIsNone(backfill.jpeg_chroma_match(target, original))
                self.assertIsNotNone(backfill.jpeg_chroma_match(target, original))
                with patch.object(backfill, 'VISUAL_CHROMA_EDGE_MAX_OUTLIER_FRACTION', 0.01):
                    self.assertIsNone(backfill.jpeg_chroma_match(target, original))

    def test_indexed_pairs_respect_minimum_date_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            images = self.make_archive(Path(directory))
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected network')):
                for message_id, name, date, _ in self.PAIRS:
                    date = dt.datetime.fromisoformat(date).date()
                    minimum = (date - dt.date.fromisoformat(name[:10])).days
                    blob = (self.fixtures(message_id) / 'telegram.jpg').read_bytes()
                    for days in (backfill.DEFAULT_DATE_WINDOW, minimum - 1, minimum, 90):
                        with self.subTest(message_id=message_id, days=days):
                            matcher = backfill.ArchiveMatcher(images, days=days, allow_visual=True)
                            matched, status = matcher.match(blob, date)
                            if days < minimum:
                                self.assertEqual((matched, status), (None, 'unmatched'))
                            else:
                                self.assertEqual(matched, name)
                                self.assertTrue(status.startswith('visual'), status)

    def test_indexed_pairs_do_not_hide_visual_duplicates(self):
        for message_id, name, date, _ in self.PAIRS:
            with self.subTest(message_id=message_id), tempfile.TemporaryDirectory() as directory:
                images = self.make_archive(Path(directory))
                month = Path(directory) / 'panzer-archiv-00/images/2022/01'
                duplicate = name[:10] + '_duplicate.jpg'
                (month / duplicate).write_bytes((month / name).read_bytes())
                index = month / 'entry_index.json'
                index.write_text(json.dumps(json.loads(index.read_text()) + [{'name': duplicate}]))
                matcher = backfill.ArchiveMatcher(images, days=90, allow_visual=True)
                blob = (self.fixtures(message_id) / 'telegram.jpg').read_bytes()
                self.assertEqual(matcher.match(blob, dt.datetime.fromisoformat(date).date()), (None, 'ambiguous'))

    def test_all_four_pairs_reject_removed_captions(self):
        for message_id, _, _, rectangle in self.PAIRS:
            with self.subTest(message_id=message_id):
                fixtures = self.fixtures(message_id)
                target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())
                with Image.open(fixtures / 'archive.jpg') as source:
                    edited = source.convert('RGB')
                ImageDraw.Draw(edited).rectangle(rectangle, fill='black')
                output = io.BytesIO()
                edited.save(output, format='JPEG', quality=85)
                self.assertEqual(backfill.choose_match(target, [('wrong-caption.jpg',
                                                                backfill.image_features(output.getvalue()))],
                                                      allow_visual=True), (None, 'unmatched'))

    def test_smoothing_alone_cannot_accept_flat_local_recoloring(self):
        target = Image.new('YCbCr', (128, 128), (100, 128, 128))
        changed = target.copy()
        draw = ImageDraw.Draw(changed)
        draw.rectangle((0, 0, 9, 9), fill=(100, 144, 128))
        draw.rectangle((10, 0, 19, 9), fill=(100, 112, 128))
        self.assertFalse(backfill.jpeg_chroma_edges_match(target, changed))
        # All the other edge-branch checks pass: shared original structure is
        # essential to distinguish this edit from JPEG text-edge artifacts.
        with patch.object(backfill, 'VISUAL_CHROMA_EDGE_MAX_FLAT_OUTLIER_FRACTION', 1.0):
            self.assertTrue(backfill.jpeg_chroma_edges_match(target, changed))

    def test_edge_branch_rejects_broad_balanced_color_changes_within_raw_rms_ceiling(self):
        target = Image.new('YCbCr', (128, 128), (100, 128, 128))
        changed = target.copy()
        draw = ImageDraw.Draw(changed)
        draw.rectangle((0, 0, 63, 127), fill=(100, 131, 128))
        draw.rectangle((64, 0, 127, 127), fill=(100, 124, 128))
        rms = ImageStat.Stat(ImageChops.difference(target, changed)).rms[1]
        self.assertGreater(rms, backfill.VISUAL_CHROMA_EDGE_RMS)
        self.assertLess(rms, backfill.VISUAL_CHROMA_MAX_RMS)
        self.assertFalse(backfill.jpeg_chroma_edges_match(target, changed))


class VerifiedEarly2022Tests(unittest.TestCase):
    PAIRS = (
        (2025, '2021-12-12_38EE955A.jpg', '2022-02-06T11:11:51+00:00', (0, 0, 210, 113)),
        (2288, '2022-01-04_10D94086.jpg', '2022-02-26T09:35:27+00:00', (415, 172, 612, 431)),
        (2738, '2022-02-14_0FC8C158.jpg', '2022-03-28T21:29:04+00:00', (43, 350, 591, 412)),
    )
    OLD_SETTINGS = dict(VISUAL_ALGORITHM_VERSION=9, VISUAL_LUMA_MAX_OUTLIER_FRACTION=0.001,
                        VISUAL_CHROMA_EDGE_MAX_FLAT_OUTLIER_FRACTION=0.002)
    fixtures = staticmethod(VerifiedJanuary2022Tests.fixtures)

    @classmethod
    def make_archive(cls, root):
        images = root / 'panzer-www/images'
        images.mkdir(parents=True)
        months = {name[:4] + '/' + name[5:7] for _, name, _, _ in cls.PAIRS}
        (images / 'dir_index.json').write_text(json.dumps({month: 1 for month in sorted(months)}))
        for month in months:
            originals = root / 'panzer-archiv-00/images' / month
            originals.mkdir(parents=True)
            entries = []
            for message_id, name, _, _ in cls.PAIRS:
                if name.startswith(month.replace('/', '-')):
                    entries.append({'name': name})
                    (originals / name).write_bytes((cls.fixtures(message_id) / 'archive.jpg').read_bytes())
            (originals / 'entry_index.json').write_text(json.dumps(entries))
        return images

    def test_real_pairs_match_only_with_visual_opt_in_and_reject_duplicates(self):
        for message_id, name, _, _ in self.PAIRS:
            with self.subTest(message_id=message_id):
                fixtures = self.fixtures(message_id)
                target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())
                original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())
                self.assertNotEqual(target[0], original[0])
                self.assertNotEqual(target[1], original[1])
                self.assertEqual(target[2], original[2])
                self.assertEqual(backfill.choose_match(target, [(name, original)]), (None, 'unmatched'))
                with patch.multiple(backfill, **self.OLD_SETTINGS):
                    matched, _ = backfill.choose_match(target, [(name, original)], allow_visual=True)
                    # 2288 already passed version 9; do not loosen its RGB path.
                    self.assertEqual(matched, name if message_id == 2288 else None)
                matched, status = backfill.choose_match(target, [(name, original)], allow_visual=True)
                self.assertEqual(matched, name)
                self.assertTrue(status.startswith('visual'), status)
                self.assertEqual(backfill.choose_match(target, [(name, original), ('duplicate.jpg', original)],
                                                      allow_visual=True), (None, 'ambiguous'))

    def test_each_new_pair_is_rejected_by_its_old_outlier_limit_independently(self):
        for message_id, setting, old_limit in [
            (2025, 'VISUAL_LUMA_MAX_OUTLIER_FRACTION', 0.001),
            (2738, 'VISUAL_CHROMA_EDGE_MAX_FLAT_OUTLIER_FRACTION', 0.002),
        ]:
            with self.subTest(message_id=message_id):
                fixtures = self.fixtures(message_id)
                target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())[3]
                original = backfill.image_features((fixtures / 'archive.jpg').read_bytes())[3]
                self.assertIsNotNone(backfill.jpeg_chroma_match(target, original))
                with patch.object(backfill, setting, old_limit):
                    self.assertIsNone(backfill.jpeg_chroma_match(target, original))

    def test_indexed_pairs_respect_minimum_date_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            images = self.make_archive(Path(directory))
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected network')):
                for message_id, name, date, _ in self.PAIRS:
                    date = dt.datetime.fromisoformat(date).date()
                    minimum = (date - dt.date.fromisoformat(name[:10])).days
                    blob = (self.fixtures(message_id) / 'telegram.jpg').read_bytes()
                    for days in (backfill.DEFAULT_DATE_WINDOW, minimum - 1, minimum, 90):
                        with self.subTest(message_id=message_id, days=days):
                            matcher = backfill.ArchiveMatcher(images, days=days, allow_visual=True)
                            matched, status = matcher.match(blob, date)
                            if days < minimum:
                                self.assertEqual((matched, status), (None, 'unmatched'))
                            else:
                                self.assertEqual(matched, name)
                                self.assertTrue(status.startswith('visual'), status)

    def test_indexed_pairs_keep_duplicates_ambiguous(self):
        for message_id, name, date, _ in self.PAIRS:
            with self.subTest(message_id=message_id), tempfile.TemporaryDirectory() as directory:
                images = self.make_archive(Path(directory))
                month = Path(directory) / 'panzer-archiv-00/images' / name[:4] / name[5:7]
                duplicate = name[:10] + '_duplicate.jpg'
                (month / duplicate).write_bytes((month / name).read_bytes())
                index = month / 'entry_index.json'
                index.write_text(json.dumps(json.loads(index.read_text()) + [{'name': duplicate}]))
                matcher = backfill.ArchiveMatcher(images, days=90, allow_visual=True)
                blob = (self.fixtures(message_id) / 'telegram.jpg').read_bytes()
                self.assertEqual(matcher.match(blob, dt.datetime.fromisoformat(date).date()), (None, 'ambiguous'))

    def test_all_three_pairs_reject_removed_captions(self):
        for message_id, _, _, rectangle in self.PAIRS:
            with self.subTest(message_id=message_id):
                fixtures = self.fixtures(message_id)
                target = backfill.image_features((fixtures / 'telegram.jpg').read_bytes())
                with Image.open(fixtures / 'archive.jpg') as source:
                    edited = source.convert('RGB')
                ImageDraw.Draw(edited).rectangle(rectangle, fill='white')
                output = io.BytesIO()
                edited.save(output, format='JPEG', quality=85)
                self.assertEqual(backfill.choose_match(target, [('wrong-caption.jpg',
                                                                backfill.image_features(output.getvalue()))],
                                                      allow_visual=True), (None, 'unmatched'))

    def test_luminance_outlier_limit_still_rejects_one_pixel_over_the_budget(self):
        target = Image.new('RGB', (128, 128), (100, 100, 100))
        changed = target.copy()
        ImageDraw.Draw(changed).rectangle((0, 0, 5, 5), fill=(109, 109, 109))
        self.assertIsNotNone(backfill.jpeg_chroma_match(target, changed))  # 36 outliers
        changed.putpixel((6, 0), (109, 109, 109))
        self.assertIsNone(backfill.jpeg_chroma_match(target, changed))  # 37 outliers

    def test_flat_chroma_outlier_limit_still_rejects_one_pixel_over_the_budget(self):
        target = Image.new('YCbCr', (128, 128), (100, 128, 128))
        changed = target.copy()
        ImageDraw.Draw(changed).rectangle((0, 0, 10, 10), fill=(100, 144, 128))
        for y in range(10):
            changed.putpixel((11, y), (100, 144, 128))
        self.assertTrue(backfill.jpeg_chroma_edges_match(target, changed))  # 131 outliers
        changed.putpixel((11, 10), (100, 144, 128))
        self.assertFalse(backfill.jpeg_chroma_edges_match(target, changed))  # 132 outliers


class CandidateIndexTests(unittest.TestCase):
    def test_neighboring_buckets_and_concentrated_changes_are_not_missed(self):
        target = Image.new('RGB', (128, 128), (15, 15, 15))
        changed = target.copy()
        # RMS=3: one of sixteen blocks differs by 12, crossing a bucket boundary.
        ImageDraw.Draw(changed).rectangle((0, 0, 31, 31), fill=(27, 27, 27))
        index = backfill.CoarseImageIndex()
        index.add('changed', changed)
        index.add('duplicate', changed)
        index.add('unrelated', Image.new('RGB', (128, 128), 'white'))
        self.assertEqual(index.candidates(target, {'changed', 'duplicate', 'unrelated'}),
                         {'changed', 'duplicate'})
        self.assertEqual(index.candidates(target, {'duplicate'}), {'duplicate'})

    def test_coarse_bounds_follow_current_thresholds(self):
        index = backfill.CoarseImageIndex()
        target = Image.new('RGB', (128, 128), (15, 15, 15))
        index.add('brighter', Image.new('RGB', (128, 128), (30, 30, 30)))
        self.assertEqual(index.candidates(target, {'brighter'}), set())
        with patch.object(backfill, 'VISUAL_MAX_RMS', 15):
            self.assertEqual(index.candidates(target, {'brighter'}), {'brighter'})
        with patch.object(backfill, 'VISUAL_LUMA_MAX_RMS', 15):
            self.assertEqual(index.candidates(target, {'brighter'}), {'brighter'})

    def indexed_matcher(self, root, features, *, days=0):
        images, website_month, _, _ = MatchTests().archive_fixture(root)
        (website_month / 'entry_index.json').write_text(json.dumps([{'name': name} for name in features]))
        matcher = backfill.ArchiveMatcher(images, days=days, allow_visual=True)
        # Exercise real LRU eviction without creating hundreds of image files.
        matcher.features = backfill.lru_cache(maxsize=512)(
            Mock(side_effect=lambda path, url: features[Path(path).name]))
        return matcher

    def test_wide_window_indexes_once_and_only_verifies_a_small_subset(self):
        rng = random.Random(42)
        features = {}
        for number in range(600):
            coarse = Image.frombytes('L', (4, 4), rng.randbytes(16))
            preview = coarse.resize((128, 128), Image.Resampling.NEAREST).convert('RGB')
            features[f'2022-01-01_{number:04}.jpg'] = (f'bytes-{number}', f'pixels-{number}', 1.0, preview)
        name = next(iter(features))
        target = ('telegram bytes', 'telegram pixels', 1.0, features[name][3])
        date = dt.date(2022, 1, 1)
        with tempfile.TemporaryDirectory() as directory:
            matcher = self.indexed_matcher(Path(directory), features, days=30)
            with patch.object(backfill, 'image_features', return_value=target), \
                 patch.object(backfill, 'choose_match', wraps=backfill.choose_match) as choose:
                matched, status = matcher.match(b'photo', date)
                self.assertEqual(matched, name)
                self.assertTrue(status.startswith('visual'))
                self.assertEqual(len(choose.call_args.args[1]), 1)
                # The first preview was evicted while indexing; only it is reloaded.
                self.assertEqual(matcher.features.__wrapped__.call_count, 601)
                before = matcher.features.__wrapped__.call_count
                self.assertEqual(matcher.match(b'photo', date), (matched, status))
                self.assertEqual(matcher.features.__wrapped__.call_count, before)
                # Exact lookup does not need a preview, even after LRU eviction.
                matcher.features.cache_clear()
                with patch.object(backfill, 'image_features', return_value=features[name]):
                    self.assertEqual(matcher.match(b'photo', date), (name, 'bytes'))
                self.assertEqual(matcher.features.__wrapped__.call_count, before)
                # Global lookup must not leak candidates outside the date window.
                self.assertEqual(matcher.match(b'photo', date + dt.timedelta(days=31)), (None, 'unmatched'))

    def test_indexed_results_equal_full_scan_for_real_jpeg_pairs(self):
        fixtures = sorted((Path(__file__).parent / 'fixtures').glob('telegram_*/archive.jpg'))
        self.assertGreaterEqual(len(fixtures), 8)
        features = {f'2022-01-01_{path.parent.name}.jpg': backfill.image_features(path.read_bytes())
                    for path in fixtures}
        with tempfile.TemporaryDirectory() as directory:
            matcher = self.indexed_matcher(Path(directory), features)
            for path in fixtures:
                with self.subTest(fixture=path.parent.name):
                    blob = (path.parent / 'telegram.jpg').read_bytes()
                    expected = backfill.choose_match(backfill.image_features(blob), list(features.items()),
                                                     allow_visual=True)
                    self.assertIsNotNone(expected[0])
                    self.assertEqual(matcher.match(blob, dt.date(2022, 1, 1)), expected)

    def test_coarse_collision_still_requires_strict_comparison(self):
        target = ('telegram bytes', 'telegram pixels', 1.0, Image.new('RGB', (128, 128), 'black'))
        changed = target[3].copy()
        ImageDraw.Draw(changed).rectangle((0, 0, 3, 3), fill='white')
        features = {'2022-01-01_edited.jpg': ('archive bytes', 'archive pixels', 1.0, changed)}
        with tempfile.TemporaryDirectory() as directory:
            matcher = self.indexed_matcher(Path(directory), features)
            with patch.object(backfill, 'image_features', return_value=target), \
                 patch.object(backfill, 'choose_match', wraps=backfill.choose_match) as choose:
                self.assertEqual(matcher.match(b'photo', dt.date(2022, 1, 1)), (None, 'unmatched'))
                self.assertEqual(len(choose.call_args.args[1]), 1)

    def test_duplicate_visual_and_exact_precedence_survive_indexing(self):
        target = backfill.image_features(photo_blob())
        features = {
            '2022-01-01_visual-a.jpg': ('other-a', 'pixels-a', target[2], target[3]),
            '2022-01-01_visual-b.jpg': ('other-b', 'pixels-b', target[2], target[3]),
            '2022-01-02_exact.jpg': target,
            '2022-01-02_pixels.jpg': ('other encoding', target[1], target[2], target[3]),
        }
        with tempfile.TemporaryDirectory() as directory:
            matcher = self.indexed_matcher(Path(directory), features)
            self.assertEqual(matcher.match(photo_blob(), dt.date(2022, 1, 1)), (None, 'ambiguous'))
            self.assertEqual(matcher.match(photo_blob(), dt.date(2022, 1, 2)), ('2022-01-02_exact.jpg', 'bytes'))
            # Previously indexed exact originals remain outside this date window.
            self.assertEqual(matcher.match(photo_blob(), dt.date(2022, 1, 1)), (None, 'ambiguous'))


class BackfillTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.state_path = root / 'state.json'
        self.cache_path = root / 'messages.json'
        self.metadata_path = root / 'metadata.json'
        self.photo_cache_dir = root / 'photos'
        # These matcher fixtures use non-archive names; chunk layout is covered
        # separately by the metadata export tests.
        from functools import partial
        patcher = patch.object(backfill.sync, 'dump_gallery_metadata',
                               partial(backfill.sync.dump_gallery_metadata, monthly=False))
        patcher.start()
        self.addCleanup(patcher.stop)
        for module, name, value in [
            (backfill, 'STATE_PATH', self.state_path),
            (backfill.sync, 'MESSAGES_CACHE_PATH', self.cache_path),
            (backfill.sync, 'GALLERY_METADATA_PATH', self.metadata_path),
        ]:
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        output = patch('builtins.print')
        self.output = output.start()
        self.addCleanup(output.stop)

    def output_text(self):
        return '\n'.join(call.args[0] for call in self.output.call_args_list)

    def test_history_fills_old_links_preserves_existing_and_resumes(self):
        client = FakeClient([message(1), message(2, photo=False), message(3), message(100)])
        messages = {100: {'name': 'known.jpg', 'dig': 'existing', 'trct': 1}}
        state = {}
        matcher = FakeMatcher()
        matcher.match.return_value = ('old.jpg', 'bytes')
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=2))
        self.assertEqual(state['history_before_id'], 3)
        self.assertEqual(messages[3]['name'], 'old.jpg')
        self.assertNotIn(2, messages)
        self.assertNotIn(1, messages)
        self.assertEqual(client.downloads, [3])
        self.assertNotIn('photo already mapped', self.output_text())
        self.assertIn('Processed 2 messages: 1 already mapped, 1 new exact match.', self.output_text())
        self.assertIn('photo not yet mapped; downloading from Telegram...', self.output_text())
        self.assertIn('Telegram photo download | completed in', self.output_text())
        self.assertIn('photo bytes; matching archive originals...', self.output_text())
        self.assertIn('archive image matching | completed in', self.output_text())
        self.assertIn('new exact match (bytes): old.jpg', self.output_text())
        matcher.match.return_value = (None, 'ambiguous')
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None))
        self.assertEqual(client.downloads, [3, 1])
        self.assertEqual(messages[100]['name'], 'known.jpg')
        self.assertEqual(messages[100]['dig'], 'existing')
        self.assertIsNone(messages[1]['name'])
        self.assertEqual(messages[1]['match_status'], 'ambiguous')
        self.assertEqual(json.loads(self.metadata_path.read_text()), {
            'old.jpg': [3, 7, 123, 0], 'known.jpg': [100, 1, None, None],
        })
        self.assertEqual(json.loads(self.state_path.read_text())['history_before_id'], 1)
        self.assertIn('Telegram ID 2 | no photo', self.output_text())
        self.assertIn('ambiguous: no image link added', self.output_text())
        self.assertIn('Latest processed date: 2022-01-01 00:00:00+00:00.', self.output_text())

    def test_download_selects_still_image_even_when_photo_has_video_variants(self):
        msg = message(1)
        msg.photo.video_sizes = [SimpleNamespace(type='v', size=1000000000)]
        client = FakeClient([msg])
        blob = asyncio.run(backfill.download_photo(client, msg, prefix='test', timeout=1))
        self.assertEqual(blob, photo_blob())
        self.assertEqual(client.selected_thumbs, ['x'])
        output = self.output_text()
        self.assertIn('selected still image x: 200,000 bytes; 1 video variants ignored', output)
        self.assertIn('Telegram transfer:', output)
        self.assertIn('(100.0%)', output)

    def test_photo_disk_cache_reuses_bytes_across_clients_and_expired_references(self):
        msg = message(1)
        client = FakeClient([msg])
        blob = asyncio.run(backfill.download_photo(client, msg, prefix='test', timeout=1,
                                                  cache_dir=self.photo_cache_dir))
        entries = list(self.photo_cache_dir.iterdir())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].read_bytes(), blob)
        fresh_msg = message(99)
        fresh_msg.photo.id = msg.photo.id  # Same photo in a different post.
        fresh_msg.photo.access_hash = 456
        fresh_msg.photo.file_reference = b'refreshed'
        fresh_client = FakeClient([fresh_msg])
        fresh_client.fail_id = fresh_msg.photo.id
        cached = asyncio.run(backfill.download_photo(fresh_client, fresh_msg, prefix='test', timeout=1,
                                                    cache_dir=self.photo_cache_dir))
        self.assertEqual(cached, blob)
        self.assertEqual(client.downloads, [1])
        self.assertEqual(fresh_client.downloads, [])
        self.assertIn('Telegram photo cache hit:', self.output_text())

    def test_photo_cache_identity_tracks_photo_dc_and_selected_still_variant(self):
        msg = message(1)
        size = backfill.select_still_photo_size(msg.photo)
        original = backfill.photo_cache_path(self.photo_cache_dir, msg.photo, size)
        for obj, field, value in [(msg.photo, 'id', 2), (msg.photo, 'dc_id', 3),
                                  (size, 'type', 'y'), (size, 'w', 1920),
                                  (size, 'h', 1440), (size, 'size', 300000)]:
            with self.subTest(field=field), patch.object(obj, field, value):
                self.assertNotEqual(backfill.photo_cache_path(self.photo_cache_dir, msg.photo, size), original)

    def test_photo_cache_redownloads_when_highest_resolution_variant_changes(self):
        msg = message(1)
        client = FakeClient([msg])
        asyncio.run(backfill.download_photo(client, msg, prefix='test', timeout=1,
                                           cache_dir=self.photo_cache_dir))
        msg.photo.sizes.append(SimpleNamespace(type='y', w=1920, h=1440, size=300000))
        asyncio.run(backfill.download_photo(client, msg, prefix='test', timeout=1,
                                           cache_dir=self.photo_cache_dir))
        self.assertEqual(client.selected_thumbs, ['x', 'y'])
        self.assertEqual(len(list(self.photo_cache_dir.iterdir())), 2)

    def test_corrupt_photo_cache_is_redownloaded_and_replaced(self):
        msg = message(1)
        path = backfill.photo_cache_path(self.photo_cache_dir, msg.photo,
                                        backfill.select_still_photo_size(msg.photo))
        path.parent.mkdir()
        for invalid in [b'', b'not an image', photo_blob()[:100]]:
            with self.subTest(invalid=invalid):
                path.write_bytes(invalid)
                client = FakeClient([msg])
                blob = asyncio.run(backfill.download_photo(client, msg, prefix='test', timeout=1,
                                                          cache_dir=self.photo_cache_dir))
                self.assertEqual(client.downloads, [1])
                self.assertEqual(path.read_bytes(), blob)
                self.assertEqual(list(self.photo_cache_dir.iterdir()), [path])
        self.assertIn('unusable Telegram photo cache; downloading again', self.output_text())

    def test_failed_timed_out_empty_or_invalid_download_is_not_cached(self):
        msg = message(1)
        for failure in ['failure', 'timeout', 'empty', 'invalid']:
            with self.subTest(failure=failure):
                client = FakeClient([msg])
                if failure == 'failure':
                    client.fail_id = 1
                elif failure == 'timeout':
                    client.hang_id = 1
                else:
                    client.download_media = AsyncMock(return_value=b'' if failure == 'empty' else b'not an image')
                with self.assertRaises((RuntimeError, TimeoutError, OSError)):
                    asyncio.run(backfill.download_photo(client, msg, prefix='test', timeout=0.01,
                                                       cache_dir=self.photo_cache_dir))
                self.assertFalse(self.photo_cache_dir.exists())

    def test_photo_cache_write_failure_does_not_block_matching(self):
        self.photo_cache_dir.write_text('not a directory')
        client = FakeClient([message(1)])
        with self.assertLogs(level='WARNING'):
            blob = asyncio.run(backfill.download_photo(client, client.messages[0], prefix='test', timeout=1,
                                                      cache_dir=self.photo_cache_dir))
        self.assertEqual(blob, photo_blob())
        self.assertEqual(client.downloads, [1])

    def test_atomic_photo_cache_write_failure_cleans_up_temporary_file(self):
        path = self.photo_cache_dir / 'photo.img'
        with patch.object(Path, 'replace', side_effect=OSError('disk error')), self.assertLogs(level='WARNING'):
            backfill.cache_photo(path, photo_blob())
        self.assertEqual(list(self.photo_cache_dir.iterdir()), [])

    def test_uncached_downloads_and_probes_bypass_existing_photo_cache(self):
        msg = message(1)
        client = FakeClient([msg])
        asyncio.run(backfill.download_photo(client, msg, prefix='test', timeout=1,
                                           cache_dir=self.photo_cache_dir))
        path = next(self.photo_cache_dir.iterdir())
        before = path.stat().st_mtime_ns
        asyncio.run(backfill.download_photo(client, msg, prefix='test', timeout=1))
        asyncio.run(backfill.probe_messages(client, 'channel', [1], timeout=1))
        self.assertEqual(client.downloads, [1, 1, 1])
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_run_enables_photo_cache_by_default_and_honors_bypass(self):
        options = SimpleNamespace(probe_message=[], download_timeout=1, stats_only=False,
                                  restart=False, date_window=7, allow_visual_matches=True,
                                  limit=1, photo_cache_dir=self.photo_cache_dir, no_photo_cache=False)
        for disabled in [False, True]:
            with self.subTest(disabled=disabled):
                options.no_photo_cache = disabled
                client = FakeClient([])
                with patch.object(backfill.sync, 'init_telethon_client', return_value=client), \
                     patch.object(backfill, 'ArchiveMatcher'), \
                     patch.object(backfill, 'backfill_history', new_callable=AsyncMock) as history:
                    asyncio.run(backfill.run(options))
                self.assertEqual(history.await_args.kwargs['photo_cache_dir'],
                                 None if disabled else self.photo_cache_dir)

    def test_photo_cache_cli_defaults_and_overrides(self):
        with patch.object(backfill, 'run', new_callable=AsyncMock) as run:
            self.assertEqual(backfill.main([]), 0)
            options = run.await_args.args[0]
            self.assertEqual(options.photo_cache_dir, backfill.DEFAULT_PHOTO_CACHE_DIR)
            self.assertFalse(options.no_photo_cache)
            self.assertEqual(backfill.main(['--photo-cache-dir', str(self.photo_cache_dir), '--no-photo-cache']), 0)
            options = run.await_args.args[0]
            self.assertEqual(options.photo_cache_dir, self.photo_cache_dir)
            self.assertTrue(options.no_photo_cache)

    def test_threshold_change_replans_and_rematches_disk_cached_photo(self):
        client = FakeClient([message(1)])
        matcher = FakeMatcher(targets={'old.jpg': dt.date(2022, 1, 1)})
        matcher.match.return_value = (None, 'unmatched')
        messages, state = {}, {}
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None,
                                             photo_cache_dir=self.photo_cache_dir))
        self.assertEqual(client.downloads, [1])
        self.assertIsNone(messages[1]['name'])
        self.assertEqual(state['history_plan']['window_index'], 1)
        # Simulate a new invocation after tweaking thresholds, retaining disk
        # cache and checkpointed state but no client/matcher in-memory caches.
        new_client = FakeClient([message(1)])
        new_matcher = FakeMatcher(targets=matcher.image_dates)
        new_matcher.match.return_value = ('old.jpg', 'visual (RMS=2.0; review)')
        state = json.loads(self.state_path.read_text())
        messages = backfill.sync.load_last_messages()
        with patch.object(backfill, 'VISUAL_MAX_RMS', backfill.VISUAL_MAX_RMS + 0.1):
            asyncio.run(backfill.backfill_history(new_client, 'channel', messages, state, new_matcher, limit=None,
                                                 photo_cache_dir=self.photo_cache_dir))
        self.assertEqual(new_client.downloads, [])
        new_matcher.match.assert_called_once_with(photo_blob(), dt.date(2022, 1, 1), message_id=1)
        self.assertEqual(messages[1]['name'], 'old.jpg')
        self.assertEqual(state['history_plan']['remaining'], [])
        self.assertIn('Telegram photo cache hit:', self.output_text())

    def test_still_variant_selection_prioritizes_pixel_area_then_byte_count(self):
        smaller = SimpleNamespace(type='x', w=493, h=800, size=74149)
        original = SimpleNamespace(type='y', w=500, h=812, sizes=[10000, 73910])
        stripped = SimpleNamespace(type='i', bytes=b'x' * 100000)
        photo = SimpleNamespace(sizes=[stripped, smaller, original],
                                video_sizes=[SimpleNamespace(type='v', w=1920, h=1080, size=999999)])
        self.assertIs(backfill.select_still_photo_size(photo), original)
        same_resolution = SimpleNamespace(type='z', w=500, h=812, size=75000)
        photo.sizes.append(same_resolution)
        self.assertIs(backfill.select_still_photo_size(photo), same_resolution)

    def test_real_message_8852_highest_resolution_download_fills_and_exports_gap(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_8852'
        name = '2023-07-24_B6866461.jpg'
        msg = message(8852)
        msg.date = dt.datetime(2023, 7, 23, 17, 24, 32, tzinfo=dt.timezone.utc)
        msg.photo.sizes = [SimpleNamespace(type='x', w=493, h=800, size=74149),
                           SimpleNamespace(type='y', w=500, h=812, sizes=[10000, 73910])]
        msg.photo.video_sizes = [SimpleNamespace(type='v', w=1920, h=1080, size=999999)]
        client = FakeClient([msg])
        async def download(photo, file, *, thumb, progress_callback):
            client.downloads.append(photo.id)
            client.selected_thumbs.append(thumb)
            blob = (fixtures / {'x': 'thumbnail.jpg', 'y': 'telegram.jpg'}[thumb]).read_bytes()
            progress_callback(len(blob), len(blob))
            return blob
        client.download_media = download
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2023/07': 1}))
            month = root / 'panzer-archiv-00/images/2023/07'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(json.dumps([{'name': name}]))
            (month / name).write_bytes((fixtures / 'archive.jpg').read_bytes())
            matcher = backfill.ArchiveMatcher(images, allow_visual=True)
            messages = {8852: {'name': None, 'match_status': 'unmatched'}}
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected download')):
                asyncio.run(backfill.backfill_history(client, 'channel', messages, {}, matcher, limit=None))
            self.assertEqual(client.selected_thumbs, ['y'])
            self.assertEqual(messages[8852]['name'], name)
            self.assertEqual(json.loads(self.metadata_path.read_text())[name][0], 8852)
            self.assertIn('1 video variants ignored', self.output_text())

    def test_real_message_10029_replans_and_matches_cached_photo_after_threshold_change(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_10029'
        name = '2023-11-06_9CFC1942.jpg'
        telegram = (fixtures / 'telegram.jpg').read_bytes()
        msg = message(10029)
        msg.date = dt.datetime.fromisoformat('2023-11-04T16:02:40+00:00')
        msg.photo.sizes = [SimpleNamespace(type='x', w=624, h=482, size=len(telegram))]
        client = FakeClient([msg])
        client.download_media = AsyncMock(return_value=telegram)
        known = {'name': 'existing.jpg', 'trct': 17}
        messages = {9999: dict(known)}
        state = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2023/11': 1}))
            month = root / 'panzer-archiv-00/images/2023/11'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(json.dumps([{'name': name}]))
            (month / name).write_bytes((fixtures / 'archive.jpg').read_bytes())
            matcher = backfill.ArchiveMatcher(images, allow_visual=True)
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected HTTP download')):
                with patch.object(backfill, 'VISUAL_LUMA_MAX_RMS', 1.5), \
                     patch.object(backfill, 'VISUAL_CHROMA_MAX_RMS', 3.0):
                    asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                         limit=None, photo_cache_dir=self.photo_cache_dir))
                self.assertIsNone(messages[10029]['name'])
                self.assertEqual(state['history_plan']['window_index'], 1)
                self.assertEqual(state['history_plan']['remaining'], [name])
                # The new thresholds must retry an exhausted plan automatically
                # and reuse the real photo's disk cache without another download.
                asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                     limit=None, photo_cache_dir=self.photo_cache_dir))
        self.assertEqual(client.download_media.await_count, 1)
        self.assertEqual(messages[10029]['name'], name)
        self.assertEqual(messages[10029]['date'], msg.date.isoformat())
        self.assertIn('; Y=', messages[10029]['match_status'])
        self.assertEqual(messages[9999], known)
        self.assertEqual(state['history_plan']['remaining'], [])
        self.assertEqual(json.loads(self.metadata_path.read_text())[name][0], 10029)
        self.assertIn('Telegram photo cache hit:', self.output_text())

    def test_real_message_1848_replans_exhausted_90_day_scan_and_reuses_cached_photo(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_1848'
        name = '2021-11-26_8766DFD4.jpg'
        telegram = (fixtures / 'telegram.jpg').read_bytes()
        msg = message(1848)
        msg.date = dt.datetime.fromisoformat('2022-01-22T02:23:36+00:00')
        msg.photo.sizes = [SimpleNamespace(type='y', w=578, h=1280, size=len(telegram))]
        client = FakeClient([msg])
        client.download_media = AsyncMock(return_value=telegram)
        messages, state = {}, {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2021/11': 1}))
            month = root / 'panzer-archiv-00/images/2021/11'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(json.dumps([{'name': name}]))
            (month / name).write_bytes((fixtures / 'archive.jpg').read_bytes())
            matcher = backfill.ArchiveMatcher(images, days=90, allow_visual=True)
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected HTTP download')):
                with patch.object(backfill, 'VISUAL_MAX_LUMA_MEAN_SHIFT', 1.0):
                    asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                         limit=None, photo_cache_dir=self.photo_cache_dir))
                self.assertIsNone(messages[1848]['name'])
                self.assertEqual(state['history_plan']['window_index'], 1)
                self.assertEqual(state['history_plan']['remaining'], [name])
                asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                     limit=None, photo_cache_dir=self.photo_cache_dir))
        self.assertEqual(client.download_media.await_count, 1)
        self.assertEqual(messages[1848]['name'], name)
        self.assertEqual(messages[1848]['date'], msg.date.isoformat())
        self.assertIn('; Y=', messages[1848]['match_status'])
        self.assertEqual(state['history_plan']['remaining'], [])
        self.assertEqual(json.loads(self.metadata_path.read_text())[name][0], 1848)
        self.assertIn('Telegram photo cache hit:', self.output_text())

    def test_real_message_1853_replans_exhausted_90_day_scan_and_reuses_cached_photo(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_1853'
        name = '2021-11-27_3B64D3C0.jpg'
        telegram = (fixtures / 'telegram.jpg').read_bytes()
        msg = message(1853)
        msg.date = dt.datetime.fromisoformat('2022-01-23T01:12:55+00:00')
        msg.photo.sizes = [SimpleNamespace(type='x', w=680, h=606, size=len(telegram))]
        client = FakeClient([msg])
        client.download_media = AsyncMock(return_value=telegram)
        messages, state = {}, {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2021/11': 1}))
            month = root / 'panzer-archiv-00/images/2021/11'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(json.dumps([{'name': name}]))
            (month / name).write_bytes((fixtures / 'archive.jpg').read_bytes())
            matcher = backfill.ArchiveMatcher(images, days=90, allow_visual=True)
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected HTTP download')):
                with patch.object(backfill, 'VISUAL_MAX_CHANNEL_MEAN_SHIFT', 1.0):
                    asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                         limit=None, photo_cache_dir=self.photo_cache_dir))
                self.assertIsNone(messages[1853]['name'])
                self.assertEqual(state['history_plan']['window_index'], 1)
                self.assertEqual(state['history_plan']['remaining'], [name])
                asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                     limit=None, photo_cache_dir=self.photo_cache_dir))
        self.assertEqual(client.download_media.await_count, 1)
        self.assertEqual(messages[1853]['name'], name)
        self.assertEqual(messages[1853]['date'], msg.date.isoformat())
        self.assertIn('; chroma=', messages[1853]['match_status'])
        self.assertEqual(state['history_plan']['remaining'], [])
        self.assertEqual(json.loads(self.metadata_path.read_text())[name][0], 1853)
        self.assertIn('Telegram photo cache hit:', self.output_text())

    def test_verified_january2022_pairs_replan_and_match_cached_photos(self):
        pairs = VerifiedJanuary2022Tests.PAIRS
        blobs = {message_id: (VerifiedJanuary2022Tests.fixtures(message_id) / 'telegram.jpg').read_bytes()
                 for message_id, _, _, _ in pairs}
        posts = []
        for message_id, _, date, _ in pairs:
            msg = message(message_id)
            msg.date = dt.datetime.fromisoformat(date)
            with Image.open(io.BytesIO(blobs[message_id])) as image:
                width, height = image.size
            msg.photo.sizes = [SimpleNamespace(type='y' if message_id == 2499 else 'x',
                                              w=width, h=height, size=len(blobs[message_id]))]
            posts.append(msg)
        client = FakeClient(posts)
        client.download_media = AsyncMock(side_effect=lambda photo, *args, **kwargs: blobs[photo.id])
        known = {'name': 'existing.jpg', 'trct': 17}
        messages, state = {9999: dict(known)}, {}
        with tempfile.TemporaryDirectory() as directory:
            images = VerifiedJanuary2022Tests.make_archive(Path(directory))
            matcher = backfill.ArchiveMatcher(images, days=90, allow_visual=True)
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected HTTP download')):
                with patch.multiple(backfill, **VerifiedJanuary2022Tests.OLD_SETTINGS), \
                     patch.object(backfill, 'jpeg_chroma_edges_match', return_value=False):
                    asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                         limit=None, photo_cache_dir=self.photo_cache_dir))
                self.assertEqual(state['history_plan']['window_index'], 1)
                for message_id, _, _, _ in pairs:
                    self.assertIsNone(messages[message_id]['name'])
                asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                     limit=None, photo_cache_dir=self.photo_cache_dir))
        self.assertEqual(client.download_media.await_count, 4)
        self.assertEqual(messages[9999], known)
        self.assertEqual(state['history_plan']['remaining'], [])
        metadata = json.loads(self.metadata_path.read_text())
        for message_id, name, date, _ in pairs:
            self.assertEqual(messages[message_id]['name'], name)
            self.assertEqual(messages[message_id]['date'], date)
            self.assertTrue(messages[message_id]['match_status'].startswith('visual'))
            self.assertEqual(metadata[name][0], message_id)
        self.assertEqual(self.output_text().count('Telegram photo cache hit:'), 4)

    def test_new_outlier_limits_retry_cached_gaps_and_preserve_already_linked_2288(self):
        pairs = VerifiedEarly2022Tests.PAIRS
        blobs = {message_id: (VerifiedEarly2022Tests.fixtures(message_id) / 'telegram.jpg').read_bytes()
                 for message_id, _, _, _ in pairs}
        posts = []
        for message_id, _, date, _ in pairs:
            msg = message(message_id)
            msg.date = dt.datetime.fromisoformat(date)
            with Image.open(io.BytesIO(blobs[message_id])) as image:
                width, height = image.size
            msg.photo.sizes = [SimpleNamespace(type='x', w=width, h=height, size=len(blobs[message_id]))]
            posts.append(msg)
        client = FakeClient(posts)
        client.download_media = AsyncMock(side_effect=lambda photo, *args, **kwargs: blobs[photo.id])
        known = {'name': pairs[1][1], 'date': pairs[1][2], 'match_status': 'visual (RMS=2.705; review)',
                 'tview': 936, 'trct': 0}
        messages, state = {2288: dict(known)}, {}
        with tempfile.TemporaryDirectory() as directory:
            images = VerifiedEarly2022Tests.make_archive(Path(directory))
            matcher = backfill.ArchiveMatcher(images, days=90, allow_visual=True)
            with patch.object(backfill, 'download', side_effect=AssertionError('unexpected HTTP download')):
                with patch.multiple(backfill, **VerifiedEarly2022Tests.OLD_SETTINGS):
                    asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                         limit=None, photo_cache_dir=self.photo_cache_dir))
                self.assertEqual(state['history_plan']['window_index'], 1)
                self.assertIsNone(messages[2025]['name'])
                self.assertIsNone(messages[2738]['name'])
                asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                     limit=None, photo_cache_dir=self.photo_cache_dir))
        self.assertEqual(client.download_media.await_count, 2)
        self.assertEqual(messages[2288], known)
        self.assertEqual(state['history_plan']['remaining'], [])
        metadata = json.loads(self.metadata_path.read_text())
        for message_id, name, date, _ in pairs:
            self.assertEqual(messages[message_id]['name'], name)
            self.assertEqual(messages[message_id]['date'], date)
            self.assertTrue(messages[message_id]['match_status'].startswith('visual'))
            self.assertEqual(metadata[name][0], message_id)
        self.assertEqual(self.output_text().count('Telegram photo cache hit:'), 2)

    def test_history_skips_video_link_preview_photos_and_resumes(self):
        from telethon.tl.types import (
            Message, MessageMediaWebPage, PeerChannel, Photo, PhotoSizeProgressive, WebPage,
        )
        dates = {
            14046: '2024-09-28T18:23:18+00:00',
            14079: '2024-10-02T12:16:44+00:00',
            14090: '2024-10-02T16:30:35+00:00',
        }
        previews = []
        for message_id, date_string in dates.items():
            date = dt.datetime.fromisoformat(date_string)
            photo = Photo(id=message_id, access_hash=456, file_reference=b'test', date=date, dc_id=2,
                          sizes=[PhotoSizeProgressive(type='y', w=1280, h=720, sizes=[10000, 70000])])
            webpage = WebPage(id=message_id, url='https://example.com/video',
                              display_url='example.com', hash=0, type='video', photo=photo)
            msg = Message(id=message_id, peer_id=PeerChannel(1), date=date, message='video link',
                          media=MessageMediaWebPage(webpage))
            # Exercise Telethon's real msg.photo property: a non-photo post can
            # still expose a downloadable preview photo.
            self.assertIs(msg.photo, photo)
            previews.append(msg)

        for cached in [False, True]:
            with self.subTest(cached=cached):
                messages = {key: {'name': None, 'dig': 'legacy', 'match_status': 'unmatched'}
                            for key in dates} if cached else {}
                expected_messages = {key: dict(record) for key, record in messages.items()}
                client = FakeClient(previews)
                matcher = FakeMatcher({'missing.jpg': dt.date(2024, 10, 2)}, days=7)
                state = {}
                with patch.object(backfill, 'download_photo', new_callable=AsyncMock) as download:
                    asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                         limit=2, photo_cache_dir=self.photo_cache_dir))
                    self.assertEqual(state['history_before_id'], 14079)
                    asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                         limit=None, photo_cache_dir=self.photo_cache_dir))
                    download.assert_not_awaited()
                matcher.match.assert_not_called()
                self.assertEqual(client.downloads, [])
                self.assertFalse(self.photo_cache_dir.exists())
                self.assertEqual(messages, expected_messages)
                self.assertEqual(json.loads(self.cache_path.read_text()),
                                 {str(key): record for key, record in expected_messages.items()})
                self.assertEqual(state['history_before_id'], 14046)
                self.assertEqual(state['history_plan']['remaining'], ['missing.jpg'])
                self.assertEqual(state['history_plan']['window_index'], 1)
        self.assertIn('Processed 2 messages: 2 non-photo media.', self.output_text())
        self.assertNotIn('non-photo media (MessageMediaWebPage); skipping', self.output_text())

    def test_webpage_with_empty_document_downloads_photo_not_document(self):
        from telethon.client.downloads import DownloadMethods
        from telethon.tl.types import (
            DocumentEmpty, Message, MessageMediaWebPage, PeerChannel,
            Photo, PhotoSizeProgressive, WebPage,
        )
        date = dt.datetime(2024, 11, 17, tzinfo=dt.timezone.utc)
        photo = Photo(id=123, access_hash=456, file_reference=b'test', date=date, dc_id=4,
                      sizes=[PhotoSizeProgressive(type='y', w=1280, h=720, sizes=[11191, 71421])])
        webpage = WebPage(id=1, url='https://example.com/video', display_url='example.com',
                          hash=0, photo=photo, document=DocumentEmpty(id=789))
        msg = Message(id=14609, peer_id=PeerChannel(1), date=date, message='link',
                      media=MessageMediaWebPage(webpage))
        # Exercise Telethon's actual dispatch instead of mocking download_media:
        # whole-message dispatch chooses an unsupported DocumentEmpty and returns
        # None without downloading anything; passing msg.photo downloads the still.
        client = SimpleNamespace(_download_photo=AsyncMock(return_value=photo_blob()),
                                 _download_document=AsyncMock(return_value=None))
        async def download(media, file, **kwargs):
            return await DownloadMethods.download_media(client, media, file, **kwargs)
        client.download_media = download
        self.assertIsNone(asyncio.run(client.download_media(msg, bytes, thumb='y')))
        client._download_document.assert_not_awaited()
        client._download_photo.assert_not_awaited()
        blob = asyncio.run(backfill.download_photo(client, msg, prefix='test', timeout=1))
        self.assertEqual(blob, photo_blob())
        client._download_document.assert_not_awaited()
        self.assertIs(client._download_photo.await_args.args[0], photo)
        self.assertEqual(client._download_photo.await_args.args[3], 'y')
        self.assertIn('message media: MessageMediaWebPage', self.output_text())

    def test_photo_without_still_sizes_is_not_downloaded(self):
        msg = message(1)
        msg.photo.sizes = []
        msg.photo.video_sizes = [SimpleNamespace(type='v', size=1000000000)]
        client = FakeClient([msg])
        with self.assertRaisesRegex(RuntimeError, 'no downloadable still-image variant'):
            asyncio.run(backfill.download_photo(client, msg, prefix='test', timeout=1))
        self.assertEqual(client.downloads, [])

    def test_progressive_image_size_is_selected_and_metadata_excludes_video_sizes(self):
        msg = message(1)
        msg.photo.sizes.append(SimpleNamespace(type='y', w=1920, h=1440, sizes=[10000, 300000]))
        msg.photo.video_sizes = [SimpleNamespace(type='v', size=1000000000)]
        client = FakeClient([msg])
        asyncio.run(backfill.download_photo(client, msg, prefix='test', timeout=1))
        self.assertEqual(client.selected_thumbs, ['y'])
        self.assertIn('y 1920x1440: 300,000 bytes', self.output_text())
        self.assertNotIn('1,000,000,000', self.output_text())

    def test_still_selector_works_with_real_telethon_photo_and_video_types(self):
        from telethon.client.downloads import DownloadMethods
        from telethon.tl.types import PhotoSizeProgressive, VideoSize
        photo = SimpleNamespace(sizes=[PhotoSizeProgressive(type='y', w=1920, h=1440, sizes=[100, 300000])],
                                video_sizes=[VideoSize(type='v', w=1920, h=1440, size=1000000000)])
        # The default would pick the video. Our explicit type picks the still.
        self.assertIs(DownloadMethods._get_thumb(photo.sizes + photo.video_sizes, None), photo.video_sizes[0])
        selected = backfill.select_still_photo_size(photo)
        self.assertIs(DownloadMethods._get_thumb(photo.sizes + photo.video_sizes, selected.type), photo.sizes[0])

    def test_probe_run_does_not_read_or_write_backfill_files(self):
        self.state_path.write_text('not JSON: diagnostic mode must not even read this')
        client = FakeClient([message(1), message(2, photo=False)])
        options = SimpleNamespace(probe_message=[1, 2], download_timeout=1)
        with patch.object(backfill.sync, 'init_telethon_client', return_value=client), \
             patch.object(backfill.sync, 'load_last_messages', side_effect=AssertionError('read cache')), \
             patch.object(backfill, 'ArchiveMatcher', side_effect=AssertionError('read archive')):
            asyncio.run(backfill.run(options))
        self.assertEqual(self.state_path.read_text(), 'not JSON: diagnostic mode must not even read this')
        self.assertFalse(self.cache_path.exists())
        self.assertFalse(self.metadata_path.exists())
        self.assertEqual(client.downloads, [1])
        self.assertIn('message fetched; downloading photo even if already mapped', self.output_text())
        self.assertIn('downloaded', self.output_text())
        self.assertIn('not saved', self.output_text())
        self.assertIn('Telegram ID 2', self.output_text())
        self.assertIn('no photo', self.output_text())

    def test_probe_continues_after_failed_download_or_missing_message(self):
        client = FakeClient([message(1), message(2)])
        client.fail_id = 1
        with self.assertRaisesRegex(RuntimeError, 'Probe failed for Telegram message IDs: 1, 3'):
            asyncio.run(backfill.probe_messages(client, 'channel', [1, 2, 3], timeout=1))
        self.assertEqual(client.downloads, [1, 2])
        self.assertIn('failed: RuntimeError: download failed', self.output_text())
        self.assertIn('message deleted/unavailable', self.output_text())
        self.assertIn('downloaded', self.output_text())
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.cache_path.exists())
        self.assertFalse(self.metadata_path.exists())

    def test_download_failure_checkpoints_without_skipping_failed_message(self):
        client = FakeClient([message(1), message(2)])
        client.fail_id = 1
        state = {}
        messages = {}
        matcher = FakeMatcher()
        matcher.match.return_value = ('old.jpg', 'pixels')
        with self.assertRaisesRegex(RuntimeError, 'download failed'):
            asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None))
        self.assertEqual(state['history_before_id'], 2)
        self.assertEqual(set(json.loads(self.cache_path.read_text())), {'2'})
        self.assertEqual(json.loads(self.state_path.read_text())['history_before_id'], 2)
        client.fail_id = None
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None))
        self.assertEqual(client.downloads, [2, 1, 1])
        self.assertEqual(state['history_before_id'], 1)

    def test_download_timeout_preserves_cursor_cancels_download_and_retries(self):
        client = FakeClient([message(14609)])
        client.hang_id = 14609
        state = {}
        messages = {}
        matcher = FakeMatcher()
        matcher.match.return_value = ('recovered.jpg', 'bytes')
        with self.assertRaisesRegex(TimeoutError, 'Telegram ID 14609.*Telegram photo download timed out'):
            asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher,
                                                 limit=None, download_timeout=0.01))
        self.assertTrue(client.download_cancelled)
        self.assertEqual(messages, {})
        self.assertEqual(json.loads(self.state_path.read_text())['history_before_id'], 0)
        matcher.match.assert_not_called()
        self.assertIn('selected still image x: 200,000 bytes', self.output_text())
        client.hang_id = None
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None))
        self.assertEqual(client.downloads, [14609, 14609])
        self.assertEqual(state['history_before_id'], 14609)
        self.assertEqual(messages[14609]['name'], 'recovered.jpg')

    def test_slow_operations_report_heartbeat_and_return_result(self):
        async def slow():
            await asyncio.sleep(0.03)
            return 'result'
        result = asyncio.run(backfill.wait_with_progress(slow(), label='archive image matching',
                                                        interval=0.005))
        self.assertEqual(result, 'result')
        self.assertIn('archive image matching | still working after', self.output_text())
        self.assertIn('archive image matching | completed in', self.output_text())

    def test_cancelling_progress_wait_cancels_underlying_operation(self):
        async def exercise():
            started = asyncio.Event()
            cancelled = asyncio.Event()
            async def hanging():
                try:
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            task = asyncio.create_task(backfill.wait_with_progress(hanging(), label='download'))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cancelled.is_set())
        asyncio.run(exercise())

    def test_match_failure_checkpoints_without_creating_guessed_mapping(self):
        client = FakeClient([message(1)])
        matcher = FakeMatcher()
        matcher.match.side_effect = RuntimeError('archive unavailable')
        messages, state = {}, {}
        with self.assertRaisesRegex(RuntimeError, 'archive unavailable'):
            asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None))
        self.assertEqual(messages, {})
        self.assertEqual(state['history_before_id'], 0)

    def test_retry_previously_unmatched_photo(self):
        client = FakeClient([message(1)])
        messages = {1: {'name': None, 'dig': 'legacy'}}
        matcher = FakeMatcher()
        matcher.match.return_value = ('recovered.jpg', 'bytes')
        asyncio.run(backfill.backfill_history(client, 'channel', messages, {}, matcher, limit=None))
        self.assertEqual(messages[1]['name'], 'recovered.jpg')

    def test_legacy_ascending_cursor_restarts_at_newest_preserving_stats_cursor(self):
        client = FakeClient([message(1), message(10), message(100)])
        state = {'history_last_id': 10, 'stats_last_id': 42}
        messages = {100: {'name': 'known.jpg'}}
        matcher = FakeMatcher()
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        self.assertNotIn('history_last_id', state)
        self.assertEqual(state['history_before_id'], 100)
        self.assertEqual(state['stats_last_id'], 42)
        matcher.match.assert_not_called()
        self.assertIn('Planning missing-image date windows, newest to oldest', self.output_text())

    def test_output_reports_visual_and_unmatched_photos_with_dates(self):
        newer = message(2)
        newer.date = dt.datetime(2022, 2, 3, 12, 34, tzinfo=dt.timezone.utc)
        client = FakeClient([message(1), newer])
        matcher = FakeMatcher()
        matcher.match.side_effect = [('visual.jpg', 'visual (RMS=1.234; review)'),
                                     (None, 'unmatched')]
        asyncio.run(backfill.backfill_history(client, 'channel', {}, {}, matcher, limit=None))
        output = self.output_text()
        self.assertIn('[1] 2022-02-03 12:34:00+00:00 | Telegram ID 2 | visual (RMS=1.234; review): visual.jpg',
                      output)
        self.assertIn('[2] 2022-01-01 00:00:00+00:00 | Telegram ID 1 | unmatched: no image link added',
                      output)
        self.assertIn('Processed 2 messages: 1 visual match, 1 unmatched.', output)

    def test_checkpoint_progress_includes_date_and_resumes_before_last_id(self):
        client = FakeClient([message(key, photo=False) for key in range(1, 52)])
        state = {}
        asyncio.run(backfill.backfill_history(client, 'channel', {}, state, FakeMatcher(), limit=50))
        self.assertEqual(state['history_before_id'], 2)
        self.assertIn('Checkpoint: 50 messages; latest processed date 2022-01-01 00:00:00+00:00; '
                      'Telegram ID 2', self.output_text())
        asyncio.run(backfill.backfill_history(client, 'channel', {}, state, FakeMatcher(), limit=None))
        self.assertEqual(state['history_before_id'], 1)
        self.assertIn('starting before Telegram ID 2', self.output_text())

    def test_stats_only_batches_resume_and_preserve_deleted_totals(self):
        client = FakeClient([message(1), message(3)])
        messages = {key: {'name': f'{key}.jpg', 'trct': 9, 'date': '2022-01-01T00:00:00+00:00'}
                    for key in [1, 2, 3]}
        state = {}
        asyncio.run(backfill.refresh_stats(client, 'channel', messages, state, limit=2,
                                          today=dt.date(2022, 1, 10)))
        self.assertEqual(state['stats_last_id'], 2)
        self.assertEqual(messages[1]['tcomments'], 0)
        self.assertEqual(messages[2], {'name': '2.jpg', 'trct': 9, 'date': '2022-01-01T00:00:00+00:00'})
        asyncio.run(backfill.refresh_stats(client, 'channel', messages, state, limit=None,
                                          today=dt.date(2022, 1, 10)))
        self.assertEqual(client.requests, [[1, 2], [3]])
        self.assertEqual(client.downloads, [])
        self.assertEqual(state['stats_last_id'], 0)
        self.assertEqual(messages[3]['trct'], 7)

    def test_gap_scan_does_not_request_history_when_every_image_is_linked(self):
        client = FakeClient([message(1)])
        matcher = FakeMatcher({'known.jpg': dt.date(2022, 1, 1)}, days=3)
        messages = {1: {'name': 'known.jpg', 'trct': 9}}
        state = {'history_before_id': 42}
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None))
        self.assertEqual(client.history_requests, [])
        self.assertEqual(client.downloads, [])
        self.assertEqual(messages[1]['trct'], 9)
        self.assertEqual(state['history_before_id'], 42)
        self.assertIn('No missing associations', self.output_text())

    def test_gap_scan_jumps_between_missing_dates_and_resumes_after_filling_a_window(self):
        newer, covered, older = message(3), message(2), message(1)
        newer.date = dt.datetime(2022, 2, 10, tzinfo=dt.timezone.utc)
        covered.date = dt.datetime(2022, 1, 20, tzinfo=dt.timezone.utc)
        matcher = FakeMatcher({'new.jpg': newer.date.date(), 'covered.jpg': covered.date.date(),
                               'old.jpg': older.date.date()}, days=0)
        client = FakeClient([newer, covered, older])
        messages = {2: {'name': 'covered.jpg', 'trct': 99}}
        state = {}
        matcher.match.return_value = ('new.jpg', 'bytes')
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        self.assertEqual(state['history_before_id'], 3)
        matcher.match.return_value = ('old.jpg', 'bytes')
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        self.assertEqual(client.downloads, [3, 1])
        self.assertEqual([request[1].date() for request in client.history_requests],
                         [dt.date(2022, 2, 11), dt.date(2022, 1, 2)])
        self.assertEqual(messages[2], {'name': 'covered.jpg', 'trct': 99})
        self.assertIn('All archive images linked; stopping', self.output_text())

    def test_gap_scan_stops_immediately_after_last_missing_image_is_linked(self):
        matcher = FakeMatcher({'only.jpg': dt.date(2022, 1, 1)}, days=3)
        matcher.match.return_value = ('only.jpg', 'bytes')
        client = FakeClient([message(1), message(2), message(3)])
        messages = {}
        asyncio.run(backfill.backfill_history(client, 'channel', messages, {}, matcher, limit=None))
        self.assertEqual(client.downloads, [3])
        self.assertEqual(set(messages), {3})
        # A newly filled gap is exported immediately, not delayed until 50 posts.
        self.assertIn('only.jpg', json.loads(self.metadata_path.read_text()))

    def test_gap_scan_skips_download_after_nearby_gaps_are_filled(self):
        newer, nearby, older = message(3), message(2), message(1)
        newer.date = dt.datetime(2022, 1, 5, tzinfo=dt.timezone.utc)
        nearby.date = dt.datetime(2022, 1, 4, tzinfo=dt.timezone.utc)
        matcher = FakeMatcher({'new.jpg': newer.date.date(), 'old.jpg': older.date.date()}, days=1)
        matcher.match.side_effect = [('new.jpg', 'bytes'), ('old.jpg', 'bytes')]
        client = FakeClient([newer, nearby, older])
        messages = {}
        asyncio.run(backfill.backfill_history(client, 'channel', messages, {}, matcher, limit=None))
        self.assertEqual(client.downloads, [3, 1])
        self.assertNotIn(2, messages)
        self.assertIn('no missing archive images in date window; download skipped', self.output_text())

    def test_gap_scan_does_not_add_duplicate_association_to_already_linked_image(self):
        matcher = FakeMatcher({'known.jpg': dt.date(2022, 1, 1), 'missing.jpg': dt.date(2022, 1, 1)})
        matcher.match.return_value = ('known.jpg', 'pixels')
        client = FakeClient([message(2)])
        messages = {1: {'name': 'known.jpg', 'trct': 88}}
        state = {}
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None))
        self.assertEqual(messages, {1: {'name': 'known.jpg', 'trct': 88}})
        self.assertIn('no gap filled', self.output_text())
        self.assertEqual(state['history_plan']['window_index'], 1)
        requests = len(client.history_requests)
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None))
        self.assertEqual(len(client.history_requests), requests)

    def test_changed_match_settings_restart_gap_search_without_losing_mappings(self):
        matcher = FakeMatcher({'missing.jpg': dt.date(2022, 1, 1)}, days=3)
        matcher.allow_visual = False
        matcher.match.return_value = (None, 'unmatched')
        client = FakeClient([message(2), message(1)])
        messages, state = {}, {}
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        self.assertEqual(state['history_before_id'], 2)
        matcher.allow_visual = True
        matcher.match.return_value = ('missing.jpg', 'visual (RMS=1.234; review)')
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        self.assertEqual(client.downloads, [2, 2])
        self.assertEqual(messages[2]['name'], 'missing.jpg')

    def test_changed_visual_algorithm_retries_previously_searched_message(self):
        matcher = FakeMatcher({'missing.jpg': dt.date(2022, 1, 1)}, days=3)
        matcher.match.return_value = (None, 'unmatched')
        client = FakeClient([message(2), message(1)])
        messages, state = {}, {}
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        self.assertEqual(state['history_before_id'], 2)
        matcher.match.return_value = ('missing.jpg', 'visual (RMS=4.151; Y=0.859; chroma=2.225; review)')
        with patch.object(backfill, 'VISUAL_ALGORITHM_VERSION', backfill.VISUAL_ALGORITHM_VERSION + 1):
            asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        self.assertEqual(client.downloads, [2, 2])
        self.assertEqual(messages[2]['name'], 'missing.jpg')

    def test_changed_date_window_replans_instead_of_skipping_prior_messages(self):
        matcher = FakeMatcher({'missing.jpg': dt.date(2022, 1, 1)}, days=3)
        matcher.match.return_value = (None, 'unmatched')
        client = FakeClient([message(2), message(1)])
        messages, state = {}, {}
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        self.assertEqual(state['history_before_id'], 2)
        matcher.days = 7
        matcher.match.return_value = ('missing.jpg', 'visual (RMS=2.363; review)')
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        self.assertEqual(client.downloads, [2, 2])
        self.assertEqual(messages[2]['name'], 'missing.jpg')

    def test_real_message_8513_gap_backfill_exports_metadata_with_default_window(self):
        fixtures = Path(__file__).parent / 'fixtures/telegram_8513'
        name = '2023-06-27_4B728817.jpg'
        msg = message(8513)
        msg.date = dt.datetime(2023, 6, 23, 20, 13, 5, tzinfo=dt.timezone.utc)
        client = FakeClient([msg])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'panzer-www/images'
            images.mkdir(parents=True)
            (images / 'dir_index.json').write_text(json.dumps({'2023/06': 1}))
            month = root / 'panzer-archiv-00/images/2023/06'
            month.mkdir(parents=True)
            (month / 'entry_index.json').write_text(json.dumps([{'name': name}]))
            (month / name).write_bytes((fixtures / 'archive.jpg').read_bytes())
            matcher = backfill.ArchiveMatcher(images, allow_visual=True)
            messages = {8513: {'name': None, 'match_status': 'unmatched'}}
            with patch.object(backfill, 'download_photo', new=AsyncMock(return_value=(fixtures / 'telegram.jpg').read_bytes())):
                asyncio.run(backfill.backfill_history(client, 'channel', messages, {}, matcher, limit=None))
            self.assertEqual(messages[8513]['name'], name)
            self.assertEqual(json.loads(self.metadata_path.read_text())[name][0], 8513)

    def test_cli_default_date_window_is_one_week(self):
        with patch.object(backfill, 'run', new=AsyncMock()) as run:
            self.assertEqual(backfill.main(['--allow-visual-matches', '--limit', '1']), 0)
        self.assertEqual(run.await_args.args[0].date_window, 7)

    def test_newly_unlinked_image_replans_even_when_catalog_is_unchanged(self):
        matcher = FakeMatcher({'known.jpg': dt.date(2022, 1, 1), 'missing.jpg': dt.date(2022, 1, 1)})
        matcher.match.return_value = (None, 'unmatched')
        client = FakeClient([message(2), message(1)])
        messages = {100: {'name': 'known.jpg'}}
        state = {}
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        del messages[100]
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        self.assertEqual(client.downloads, [2, 2])
        self.assertEqual(set(state['history_plan']['targets']), {'known.jpg', 'missing.jpg'})

    def test_removing_a_previously_filled_gap_restarts_its_searched_window(self):
        newer, older = message(2), message(1)
        newer.date = dt.datetime(2022, 2, 10, tzinfo=dt.timezone.utc)
        matcher = FakeMatcher({'new.jpg': newer.date.date(), 'old.jpg': older.date.date()}, days=0)
        matcher.match.return_value = ('new.jpg', 'bytes')
        client = FakeClient([newer, older])
        messages, state = {}, {}
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        self.assertEqual(state['history_plan']['remaining'], ['old.jpg'])
        del messages[2]
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=1))
        self.assertEqual(client.downloads, [2, 2])
        self.assertEqual(messages[2]['name'], 'new.jpg')

    def test_added_archive_image_replans_an_exhausted_search(self):
        client = FakeClient([message(2), message(1)])
        matcher = FakeMatcher({'old.jpg': dt.date(2022, 1, 1)}, days=0)
        matcher.match.return_value = (None, 'unmatched')
        messages, state = {}, {}
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, matcher, limit=None))
        self.assertEqual(state['history_plan']['window_index'], 1)
        new_matcher = FakeMatcher({'old.jpg': dt.date(2022, 1, 1), 'added.jpg': dt.date(2022, 1, 1)}, days=0)
        new_matcher.match.return_value = ('added.jpg', 'bytes')
        asyncio.run(backfill.backfill_history(client, 'channel', messages, state, new_matcher, limit=1))
        self.assertEqual(client.downloads, [2, 1, 2])
        self.assertEqual(messages[2]['name'], 'added.jpg')

    def test_linked_identical_archive_candidate_still_causes_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory:
            images, website_month, name, _ = MatchTests().archive_fixture(Path(directory))
            second = '2022-01-01_duplicate.jpg'
            (website_month / 'entry_index.json').write_text(json.dumps([{'name': name}, {'name': second}]))
            matcher = backfill.ArchiveMatcher(images, days=3, allow_visual=True)
            pending = matcher.unlinked_names({1: {'name': second}})
            self.assertEqual(pending, {name})
            with patch.object(matcher, 'features', return_value=backfill.image_features(photo_blob())):
                self.assertEqual(matcher.match(photo_blob(), dt.date(2022, 1, 1)), (None, 'ambiguous'))

    def test_gap_windows_merge_adjacent_ranges_and_are_newest_first(self):
        matcher = FakeMatcher({'a.jpg': dt.date(2022, 1, 1), 'b.jpg': dt.date(2022, 1, 4),
                               'c.jpg': dt.date(2022, 2, 1)}, days=1)
        self.assertEqual(matcher.search_windows(set(matcher.image_dates)),
                         [(dt.date(2022, 1, 31), dt.date(2022, 2, 2)),
                          (dt.date(2021, 12, 31), dt.date(2022, 1, 5))])

    def test_stats_refresh_only_requests_recent_mapped_posts(self):
        recent, old, uncertain = message(3), message(1), message(4)
        recent.date = dt.datetime(2022, 1, 10, tzinfo=dt.timezone.utc)
        old.date = uncertain.date = dt.datetime(2021, 12, 25, tzinfo=dt.timezone.utc)
        messages = {
            1: {'name': '2021-12-25_old.jpg', 'trct': 88},
            2: {'name': None, 'date': '2022-01-10T00:00:00+00:00', 'trct': 22},
            3: {'name': '2022-01-10_recent.jpg', 'trct': 1},
            4: {'name': '2022-01-10_uncertain_archive_date.jpg', 'trct': 99},
            5: {'name': 'unknown-date.jpg', 'trct': 77},
        }
        client = FakeClient([recent, old, uncertain])
        asyncio.run(backfill.refresh_stats(client, 'channel', messages, {}, limit=None,
                                          recent_days=10, today=dt.date(2022, 1, 10)))
        self.assertEqual(client.requests, [[3, 4]])
        self.assertEqual(messages[1]['trct'], 88)
        self.assertEqual(messages[2]['trct'], 22)
        self.assertEqual(messages[3]['trct'], 7)
        self.assertEqual(messages[4]['trct'], 99)
        self.assertEqual(messages[5]['trct'], 77)
        self.assertEqual(client.downloads, [])

    def test_changing_recent_window_resets_only_stats_cursor(self):
        client = FakeClient([message(1), message(2)])
        messages = {key: {'name': f'{key}.jpg', 'date': '2022-01-01T00:00:00+00:00'} for key in [1, 2]}
        state = {'history_before_id': 42}
        asyncio.run(backfill.refresh_stats(client, 'channel', messages, state, limit=1,
                                          recent_days=30, today=dt.date(2022, 1, 10)))
        self.assertEqual(state['stats_last_id'], 1)
        asyncio.run(backfill.refresh_stats(client, 'channel', messages, state, limit=1,
                                          recent_days=10, today=dt.date(2022, 1, 10)))
        self.assertEqual(client.requests, [[1], [1]])
        self.assertEqual(state['history_before_id'], 42)

    def test_stats_refresh_can_be_disabled_and_actual_date_overrides_filename(self):
        messages = {1: {'name': '2022-01-10.jpg', 'date': '2021-12-25T00:00:00+00:00', 'trct': 99}}
        client = FakeClient([message(1)])
        self.assertEqual(backfill.cached_post_date(messages[1]), dt.date(2021, 12, 25))
        asyncio.run(backfill.refresh_stats(client, 'channel', messages, {}, limit=None,
                                          recent_days=10, today=dt.date(2022, 1, 10)))
        asyncio.run(backfill.refresh_stats(client, 'channel', messages, {}, limit=None,
                                          recent_days=0, today=dt.date(2022, 1, 10)))
        self.assertEqual(client.requests, [])
        self.assertEqual(messages[1]['trct'], 99)

    def test_missing_comment_and_view_counts_remain_unknown(self):
        msg = message(1)
        msg.views = msg.replies = msg.reactions = msg.forwards = None
        self.assertEqual(backfill.sync.telegram_stats(msg), {
            'tfwd': 0, 'trct': 0, 'tview': None, 'tcomments': None,
        })


if __name__ == '__main__':
    unittest.main()
