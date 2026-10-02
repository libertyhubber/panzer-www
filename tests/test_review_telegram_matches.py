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
from unittest.mock import AsyncMock, patch

from PIL import Image
from telethon.tl.types import MessageMediaPhoto

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import review_telegram_matches as review


def features(color='white', size=(160, 120)):
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, format='PNG')
    return review.image_features(output.getvalue())


def message(number, date, *, attachment=True):
    photo = SimpleNamespace(id=number, dc_id=2, sizes=[
        SimpleNamespace(type='x', w=160, h=120, size=1000)], video_sizes=[])
    return SimpleNamespace(id=number, date=dt.datetime.combine(date, dt.time(12), dt.timezone.utc),
                           photo=photo, media=MessageMediaPhoto(photo) if attachment else None)


class Client:
    def __init__(self, messages):
        self.messages = messages
        self.requests = []

    async def iter_messages(self, channel, *, reverse, offset_date):
        self.requests.append(offset_date)
        for msg in sorted(self.messages, key=lambda m: m.date, reverse=True):
            if offset_date is None or msg.date < offset_date:
                yield msg


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.options = SimpleNamespace(date_window=2, all_history=False,
                                       photo_cache_dir=self.root / 'photos',
                                       download_timeout=1, images=['2021-12-07_example.jpg'],
                                       unmatched=False, limit=None)
        self.channel = SimpleNamespace(id=123, username='ExampleChannel')
        self.progress = {'messages': 0, 'photos': 0, 'skipped_mapped': 0, 'failures': [], 'complete': False}
        self.date = dt.date(2021, 12, 7)
        self.target = review.Target('2021-12-07_example.jpg', self.date,
                                    'https://example.com/archive.jpg', features())
        images = self.root / 'panzer-www/images'
        images.mkdir(parents=True)
        (images / 'dir_index.json').write_text(json.dumps({'2021/12': 1}))
        month = self.root / 'panzer-archiv-00/images/2021/12'
        month.mkdir(parents=True)
        (month / 'entry_index.json').write_text(json.dumps([{'name': self.target.name}]))
        output = io.BytesIO()
        Image.new('RGB', (160, 120), 'white').save(output, format='JPEG')
        (month / self.target.name).write_bytes(output.getvalue())
        self.matcher = review.backfill.ArchiveMatcher(images, days=2, allow_visual=False)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.errors = io.StringIO()
        redirect_errors = contextlib.redirect_stderr(self.errors)
        redirect_errors.__enter__()
        self.addCleanup(redirect_errors.__exit__, None, None, None)

    def candidate(self, number, color='white'):
        return review.Candidate(number, self.date, f'https://t.me/ExampleChannel/{number}', None, features(color))

    def test_identical_previews_have_zero_distance(self):
        self.assertEqual(review.visual_distance(features(), features())['score'], 0)

    def test_changed_color_and_aspect_increase_distance_without_hard_rejection(self):
        target = features()
        changed = review.visual_distance(target, features('black'))
        stretched = review.visual_distance(target, features(size=(120, 160)))
        self.assertGreater(changed['score'], 100)
        self.assertGreater(stretched['score'], 0)
        self.assertGreater(stretched['aspect_log_difference'], 0)

    def test_keeps_five_distinct_posts_with_deterministic_ties(self):
        for number in reversed(range(1, 9)):
            self.target.consider(self.candidate(number))
        self.assertEqual([c.message_id for _, c in self.target.matches], [1, 2, 3, 4, 5])
        self.assertEqual(self.target.compared, 8)

    def test_ranking_replaces_worst_candidate(self):
        for number in range(1, 6):
            self.target.consider(self.candidate(number, 'black'))
        self.target.consider(self.candidate(10))
        self.assertEqual(self.target.matches[0][1].message_id, 10)
        self.assertEqual(len(self.target.matches), 5)
        self.assertNotIn(5, [c.message_id for _, c in self.target.matches])

    def test_real_pairs_rank_first_without_automatic_acceptance_gates(self):
        fixtures = sorted((Path(__file__).parent / 'fixtures').glob('telegram_*/archive.jpg'))
        originals = [(path, review.image_features(path.read_bytes())) for path in fixtures]
        for path in fixtures:
            with self.subTest(fixture=path.parent.name):
                target = review.image_features((path.parent / 'telegram.jpg').read_bytes())
                ranked = sorted(originals, key=lambda item: review.visual_distance(target, item[1])['score'])
                self.assertEqual(ranked[0][0], path)

    def test_archive_selection_accepts_urls_and_deduplicates(self):
        entry = self.matcher.by_date[self.date][0]
        self.assertEqual(review.select_entries(self.matcher, [entry[1], self.target.name],
                                               unmatched=False, messages={}, limit=None), [entry])

    def test_localhost_urls_resolve_to_the_canonical_archive_entry(self):
        entry = self.matcher.by_date[self.date][0]
        path = f'/images/2021/12/{self.target.name}'
        for host in ['localhost:8082', '127.0.0.1:8000', '[::1]:8082']:
            for scheme in ['http', 'https']:
                identifier = f'{scheme}://{host}{path}?v=1#preview'
                with self.subTest(identifier=identifier):
                    self.assertEqual(review.select_entries(self.matcher, [identifier], unmatched=False,
                                                           messages={}, limit=None), [entry])
        # Copy-pasted localhost URLs are identifiers, never download sources.
        with patch.object(review.backfill, 'download', side_effect=AssertionError('unexpected network')):
            selected = review.select_entries(self.matcher, [f'http://localhost:8082{path}'],
                                             unmatched=False, messages={}, limit=None)
            self.assertEqual(review.load_targets(self.matcher, selected)[0].url, entry[1])

    def test_unknown_image_and_invalid_urls_are_rejected(self):
        for identifier in ['unknown.jpg', f'https://evil.example/{self.target.name}',
                           f'ftp://localhost/images/2021/12/{self.target.name}',
                           f'http://localhost:8082/images/2022/12/{self.target.name}']:
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                review.select_entries(self.matcher, [identifier], unmatched=False, messages={}, limit=None)

    def test_unmatched_selection_excludes_linked_names_and_honors_limit(self):
        self.assertEqual(review.select_entries(self.matcher, [], unmatched=True,
                                               messages={1: {'name': self.target.name}}, limit=1), [])
        self.assertEqual(len(review.select_entries(self.matcher, [], unmatched=True,
                                                   messages={1: {'name': None}}, limit=1)), 1)

    def test_default_selection_limits_to_three_newest_unmatched_archive_images(self):
        names = ['2021-12-08_older.jpg', '2021-12-09_b.jpg', '2021-12-09_a.jpg', '2021-12-10_linked.jpg']
        for name in names:
            date = dt.date.fromisoformat(name[:10])
            self.matcher.image_dates[name] = date
            self.matcher.by_date[date].append((f'2021/12/{name}', f'https://example.com/{name}'))
        messages = {1: {'name': names[-1]}}
        selected = review.select_entries(self.matcher, [], unmatched=False, messages=messages, limit=3)
        self.assertEqual([Path(path).name for path, _ in selected],
                         ['2021-12-09_b.jpg', '2021-12-09_a.jpg', '2021-12-08_older.jpg'])
        self.assertEqual(selected, review.select_entries(self.matcher, [], unmatched=True,
                                                        messages=messages, limit=3))

    def test_targets_read_sibling_originals_without_downloading(self):
        with patch.object(review.backfill, 'download', side_effect=AssertionError('unexpected network')):
            targets = review.load_targets(self.matcher, self.matcher.by_date[self.date])
        self.assertEqual(targets[0].name, self.target.name)
        self.assertEqual(targets[0].features.aspect, 160 / 120)

    def test_scan_includes_date_boundaries_but_skips_already_linked_posts_and_previews(self):
        posts = [message(n, self.date + dt.timedelta(days=offset))
                 for n, offset in enumerate([-3, -2, 0, 2, 3], 1)]
        posts.append(message(6, self.date, attachment=False))
        client = Client(posts)
        with patch.object(review, 'photo_features', new=AsyncMock(return_value=features())) as load:
            asyncio.run(review.rank_history(client, self.channel, [self.target], self.matcher,
                                            {3: {'name': 'already-linked.jpg'}}, self.options, self.progress))
        self.assertEqual([call.args[1].id for call in load.await_args_list], [4, 2])
        self.assertEqual({c.message_id for _, c in self.target.matches}, {2, 4})
        self.assertEqual(self.progress['skipped_mapped'], 1)
        self.assertTrue(self.progress['complete'])

    def test_unmatched_and_ambiguous_records_remain_candidates(self):
        messages = {1: {'name': None, 'match_status': 'unmatched'},
                    2: {'name': None, 'match_status': 'ambiguous'},
                    3: {'name': 'mapped.jpg'}, 4: {'name': ''}}
        client = Client([message(number, self.date) for number in messages])
        with patch.object(review, 'photo_features', new=AsyncMock(return_value=features())) as load:
            asyncio.run(review.rank_history(client, self.channel, [self.target], self.matcher,
                                            messages, self.options, self.progress))
        self.assertEqual([call.args[1].id for call in load.await_args_list], [1, 2, 4])
        self.assertEqual({c.message_id for _, c in self.target.matches}, {1, 2, 4})
        self.assertEqual(self.progress['skipped_mapped'], 1)

    def test_all_mapped_posts_skip_photo_loading_entirely(self):
        client = Client([message(1, self.date), message(2, self.date)])
        with patch.object(review, 'photo_features', new=AsyncMock(side_effect=AssertionError('mapped photo loaded'))) as load:
            asyncio.run(review.rank_history(client, self.channel, [self.target], self.matcher,
                                            {1: {'name': 'a.jpg'}, 2: {'name': 'b.jpg'}},
                                            self.options, self.progress))
        load.assert_not_awaited()
        self.assertEqual(self.target.compared, 0)
        self.assertEqual(self.target.matches, [])
        self.assertEqual(self.progress['photos'], 0)
        self.assertEqual(self.progress['skipped_mapped'], 2)
        self.assertTrue(self.progress['complete'])

    def test_each_target_only_compares_its_own_window_in_merged_scan(self):
        later = review.Target('2021-12-11_later.jpg', self.date + dt.timedelta(days=4),
                               self.target.url, features())
        self.matcher.image_dates[later.name] = later.date
        client = Client([message(1, self.date), message(2, later.date),
                         message(3, self.date + dt.timedelta(days=2))])
        with patch.object(review, 'photo_features', new=AsyncMock(return_value=features())) as load:
            asyncio.run(review.rank_history(client, self.channel, [self.target, later], self.matcher,
                                            {}, self.options, self.progress))
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(load.await_count, 3)
        self.assertEqual({c.message_id for _, c in self.target.matches}, {1, 3})
        self.assertEqual({c.message_id for _, c in later.matches}, {2, 3})

    def test_all_history_ignores_date_windows(self):
        self.options.all_history = True
        client = Client([message(1, dt.date(2000, 1, 1)), message(2, dt.date(2026, 1, 1))])
        with patch.object(review, 'photo_features', new=AsyncMock(return_value=features())) as load:
            asyncio.run(review.rank_history(client, self.channel, [self.target], self.matcher,
                                            {2: {'name': 'already-linked.jpg'}}, self.options, self.progress))
        self.assertEqual(client.requests, [None])
        self.assertEqual(self.target.compared, 1)
        self.assertEqual(load.await_count, 1)
        self.assertEqual(self.progress['skipped_mapped'], 1)

    def test_failed_photo_is_reported_and_scan_continues_as_incomplete(self):
        client = Client([message(1, self.date), message(2, self.date)])
        with patch.object(review, 'photo_features', new=AsyncMock(side_effect=[ValueError('bad photo'), features()])):
            asyncio.run(review.rank_history(client, self.channel, [self.target], self.matcher,
                                            {}, self.options, self.progress))
        self.assertFalse(self.progress['complete'])
        self.assertEqual(len(self.progress['failures']), 1)
        self.assertEqual(self.target.compared, 1)

    def test_reuses_cached_photo_without_download(self):
        msg = message(1, self.date)
        path = review.backfill.photo_cache_path(self.options.photo_cache_dir, msg.photo, msg.photo.sizes[0])
        path.parent.mkdir(parents=True)
        output = io.BytesIO()
        Image.new('RGB', (160, 120), 'white').save(output, format='PNG')
        path.write_bytes(output.getvalue())
        with patch.object(review.backfill, 'download_photo', new=AsyncMock()) as download:
            result = asyncio.run(review.photo_features(None, msg, self.options))
        download.assert_not_awaited()
        self.assertEqual(result.aspect, 160 / 120)

    def test_corrupt_photo_cache_downloads_again(self):
        msg = message(1, self.date)
        path = review.backfill.photo_cache_path(self.options.photo_cache_dir, msg.photo, msg.photo.sizes[0])
        path.parent.mkdir(parents=True)
        path.write_bytes(b'broken')
        output = io.BytesIO()
        Image.new('RGB', (160, 120), 'white').save(output, format='PNG')
        with patch.object(review.backfill, 'download_photo', new=AsyncMock(return_value=output.getvalue())) as download:
            asyncio.run(review.photo_features(None, msg, self.options))
        download.assert_awaited_once()

    def test_cli_prints_five_ranked_links_without_writing_reports(self):
        for number in range(1, 7):
            candidate = self.candidate(number)
            candidate.linked_name = 'existing.jpg'
            self.target.consider(candidate)
        self.progress['complete'] = True
        with patch.object(Path, 'write_text', side_effect=AssertionError('unexpected report write')):
            review.print_results([self.target], self.options, self.progress)
        text = self.output.getvalue()
        self.assertIn(self.target.name, text)
        self.assertIn(f'Archive: {self.target.url}', text)
        self.assertEqual(text.count('https://t.me/ExampleChannel/'), 5)
        self.assertIn('1. https://t.me/ExampleChannel/1  score=0.000', text)
        self.assertIn('5. https://t.me/ExampleChannel/5', text)
        self.assertNotIn('https://t.me/ExampleChannel/6', text)
        self.assertIn('already linked to existing.jpg', text)
        self.assertIn('Complete scan. Scope: ±2', self.errors.getvalue())
        self.assertNotIn('Complete scan', text)

    def test_empty_and_partial_results_are_explicit(self):
        review.print_results([self.target], self.options, self.progress)
        self.assertIn('INCOMPLETE scan', self.errors.getvalue())
        self.assertIn('No Telegram photo candidates', self.output.getvalue())

    def test_private_channel_links(self):
        self.assertEqual(review.telegram_url(SimpleNamespace(id=123), 456), 'https://t.me/c/123/456')

    def test_run_never_changes_mappings_or_backfill_state_and_reports_interruptions(self):
        with patch.object(review.sync, 'load_last_messages', return_value={}), \
             patch.object(review.sync, 'dump_messages', side_effect=AssertionError('mapping write')), \
             patch.object(review.sync, 'dump_gallery_metadata', side_effect=AssertionError('metadata write')), \
             patch.object(review.backfill, 'checkpoint', side_effect=AssertionError('state write')), \
             patch.object(review.backfill, 'ArchiveMatcher', return_value=self.matcher), \
             patch.object(review.sync, 'init_telethon_client') as init:
            client = init.return_value.__aenter__.return_value
            client.get_entity = AsyncMock(return_value=self.channel)
            with patch.object(review, 'rank_history', new=AsyncMock(side_effect=RuntimeError('interrupted scan'))):
                with self.assertRaisesRegex(RuntimeError, 'interrupted scan'):
                    asyncio.run(review.run(self.options))
        self.assertIn('INCOMPLETE scan', self.errors.getvalue())
        self.assertIn('No Telegram photo candidates', self.output.getvalue())
        self.assertFalse(list(self.root.rglob('*.html')))
        self.assertFalse(list(self.root.rglob('review.json')))

    def test_no_unmatched_images_does_not_open_telegram(self):
        self.options.images = []
        self.options.unmatched = True
        with patch.object(review.sync, 'load_last_messages', return_value={1: {'name': self.target.name}}), \
             patch.object(review.backfill, 'ArchiveMatcher', return_value=self.matcher), \
             patch.object(review.sync, 'init_telethon_client', side_effect=AssertionError('unexpected login')):
            asyncio.run(review.run(self.options))
        self.assertIn('Complete scan', self.errors.getvalue())
        self.assertIn('No unmatched archive images selected', self.errors.getvalue())
        self.assertEqual(self.output.getvalue(), '')

    def test_limit_three_batch_loads_only_selected_archive_images_and_unmapped_posts(self):
        month = self.root / 'panzer-archiv-00/images/2021/12'
        original = (month / self.target.name).read_bytes()
        selected = ['2021-12-10_first.jpg', '2021-12-09_second.jpg', '2021-12-08_third.jpg']
        linked = '2021-12-11_linked.jpg'
        # Shuffle the index; newest-first selection must not depend on its order.
        (month / 'entry_index.json').write_text(json.dumps([
            {'name': name} for name in [selected[2], self.target.name, linked, selected[0], selected[1]]]))
        for name in selected:
            (month / name).write_bytes(original)
        # Unselected originals must not even be decoded or downloaded.
        (month / self.target.name).write_bytes(b'invalid image outside the limit')
        messages = {3: {'name': linked}}
        client_history = Client([message(n, self.date + dt.timedelta(days=2)) for n in (1, 2, 3)])
        with patch.object(review.sync, 'IMAGES_DIR', self.root / 'panzer-www/images'), \
             patch.object(review.sync, 'load_last_messages', return_value=messages), \
             patch.object(review.sync, 'init_telethon_client') as init, \
             patch.object(review.backfill, 'download', side_effect=AssertionError('unselected archive downloaded')), \
             patch.object(review, 'load_targets', wraps=review.load_targets) as load_targets, \
             patch.object(review, 'photo_features', new=AsyncMock(
                 side_effect=lambda client, msg, options: features('white' if msg.id == 1 else 'black'))) as load:
            client = init.return_value.__aenter__.return_value
            client.get_entity = AsyncMock(return_value=self.channel)
            client.iter_messages = client_history.iter_messages
            self.assertEqual(review.main(['--limit', '3', '--date-window', '2']), 0)
        self.assertEqual([Path(path).name for path, _ in load_targets.call_args.args[1]], selected)
        self.assertEqual([call.args[1].id for call in load.await_args_list], [1, 2])
        text = self.output.getvalue()
        self.assertEqual(text.count('Telegram photos compared'), 3)
        self.assertEqual(text.count('Archive: https://archiv0.derrosarotepanzer.com/'), 3)
        self.assertEqual(text.count('1. https://t.me/ExampleChannel/1'), 3)
        self.assertNotIn('https://t.me/ExampleChannel/3', text)
        self.assertNotIn(self.target.name, text)
        self.assertNotIn(linked, text)
        self.assertIn('1 mapped posts skipped', self.errors.getvalue())
        self.assertEqual(messages, {3: {'name': linked}})

    def test_cli_defaults_to_unmatched_images_and_accepts_limit_three(self):
        for args, limit in [([], None), (['--limit', '3'], 3), (['--unmatched', '--limit', '3'], 3)]:
            with self.subTest(args=args), patch.object(review, 'run', new=AsyncMock()) as run:
                self.assertEqual(review.main(args), 0)
                options = run.call_args.args[0]
                self.assertTrue(options.unmatched)
                self.assertEqual(options.images, [])
                self.assertEqual(options.limit, limit)

    def test_cli_requires_valid_options(self):
        for args in [['--unmatched', self.target.name], ['--unmatched', '--date-window', '-1'],
                     ['--unmatched', '--limit', '0'], ['--unmatched', '--output', 'report.html']]:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    review.main(args)
                self.assertEqual(error.exception.code, 2)
        with patch.object(review, 'run', new=AsyncMock()) as run:
            self.assertEqual(review.main(['--unmatched', '--limit', '25']), 0)
        self.assertEqual(run.call_args.args[0].limit, 25)
        self.assertEqual(run.call_args.args[0].date_window, 90)
        self.assertFalse(hasattr(run.call_args.args[0], 'output'))
        identifier = f'http://localhost:8082/images/2021/12/{self.target.name}'
        with patch.object(review, 'run', new=AsyncMock()) as run:
            self.assertEqual(review.main([identifier]), 0)
        self.assertEqual(run.call_args.args[0].images, [identifier])
        self.assertFalse(run.call_args.args[0].unmatched)


if __name__ == '__main__':
    unittest.main()
