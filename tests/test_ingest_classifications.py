import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from PIL import Image

from scripts import ingest_uploads as ingest
from scripts import panzer_imgsync as sync


def record(url, *, model=ingest.classifier.DEFAULT_MODEL, schema=ingest.classifier.SCHEMA_VERSION):
    return {
        'url': url, 'model': model, 'schema_version': schema,
        'reasoning_effort': ingest.classifier.DEFAULT_REASONING_EFFORT,
        'tags_call': {'model': ingest.classifier.TAGS_MODEL,
                      'reasoning_effort': ingest.classifier.TAGS_REASONING_EFFORT},
        'translation_call': None,
        'classification': {
            'text': 'Hallo!', 'languages': ['German'], 'tags': ['katze', 'meme'],
            'image_type': 'photograph', 'description': 'Eine Katze.',
            'meme_template': {'status': 'none', 'name': None, 'confidence': 1, 'evidence': ''},
        },
    }


class IngestClassificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / 'panzer-archiv-02'
        self.month = self.archive / 'images/2026/10'
        self.month.mkdir(parents=True)
        self.www = self.root / 'www'
        (self.www / 'images').mkdir(parents=True)
        self.output = self.www / 'images/classifications.jsonl'
        self.compact = self.www / 'images/classification_index.json'
        self.text = self.www / 'images/classification_text_index.json'
        self.first = self.original('2026-10-01T000000_1.jpg')
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}).start()
        patch('builtins.print').start()

    def original(self, name):
        path = self.month / name
        Image.new('RGB', (20, 30), 'red').save(path, 'JPEG')
        return path

    def url(self, path):
        from urllib.parse import quote
        return 'https://archiv2.derrosarotepanzer.com/images/2026/10/' + quote(path.name)

    def classify(self, **kwargs):
        return ingest.update_classifications(self.archive, self.www, concurrency=1, **kwargs)

    def test_local_originals_are_classified_and_both_indexes_exported(self):
        self.original('thumbnails.jpg')
        self.original('thumbnails-00.jpg')
        with patch.object(ingest.classifier, 'classify', side_effect=lambda url, *args, **kwargs: record(url)) as classify:
            self.assertEqual(self.classify(), 1)
        classify.assert_called_once_with(
            self.url(self.first), ingest.classifier.DEFAULT_MODEL, 'test-key',
            reasoning_effort=ingest.classifier.DEFAULT_REASONING_EFFORT,
            image_path=self.first, timeout=90, retries=3,
        )
        saved = json.loads(self.output.read_text())
        self.assertEqual(saved['url'], self.url(self.first))
        key = '2026/10/' + self.first.name
        self.assertEqual(json.loads(self.compact.read_text())[key], {'tags': ['katze'], 'template': None})
        self.assertEqual(json.loads(self.text.read_text())[key], {'text': 'Hallo!', 'description': 'Eine Katze.'})

    def test_default_classification_concurrency_is_ten(self):
        with patch.object(ingest.classifier, 'classify_many', return_value=(result for result in ())) as classify_many:
            self.assertEqual(ingest.update_classifications(self.archive, self.www), 0)
        self.assertEqual(classify_many.call_args.args[2], 10)

    def test_resume_skips_complete_images_without_requiring_api_key(self):
        self.output.write_text(json.dumps(record(self.url(self.first))) + '\n')
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}), patch.object(ingest.classifier, 'classify') as classify:
            self.assertEqual(self.classify(), 0)
        classify.assert_not_called()
        self.assertEqual(len(self.output.read_text().splitlines()), 1)
        self.assertTrue(self.compact.exists())
        self.assertTrue(self.text.exists())

    def test_old_schema_is_never_automatically_reclassified(self):
        for old in [record(self.url(self.first), schema=3), record(self.url(self.first), schema=7)]:
            with self.subTest(old=old):
                self.output.write_text(json.dumps(old) + '\n')
                before = self.output.read_bytes()
                with patch.dict(os.environ, {'OPENAI_API_KEY': ''}), \
                     patch.object(ingest.classifier, 'classify') as classify:
                    self.assertEqual(self.classify(), 0)
                classify.assert_not_called()
                self.assertEqual(self.output.read_bytes(), before)

    def test_current_schema_legacy_models_and_efforts_are_preserved(self):
        old = record(self.url(self.first), model='old-content-model')
        old['reasoning_effort'] = 'high'
        old['tags_call'] = {'model': 'gpt-6.1-sol', 'reasoning_effort': 'low'}
        old['translation_call'] = {'model': 'gpt-6-luna', 'reasoning_effort': 'low'}
        self.output.write_text(json.dumps(old) + '\n')
        before = self.output.read_bytes()
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}), patch.object(ingest.classifier, 'classify') as classify:
            self.assertEqual(self.classify(), 0)
        classify.assert_not_called()
        self.assertEqual(self.output.read_bytes(), before)
        self.assertTrue(self.compact.exists())
        self.assertTrue(self.text.exists())

    def test_partial_content_is_left_for_explicit_classification_runs(self):
        old = record(self.url(self.first), schema=0)
        old['stage_schema_versions'] = {'content': 8, 'tags': 0}
        old['tags_call'] = None
        old['classification']['tags'] = []
        self.output.write_text(json.dumps(old) + '\n')
        with patch.object(ingest.classifier, 'classify') as classify:
            self.assertEqual(self.classify(), 0)
        classify.assert_not_called()

    def test_scoped_batch_excludes_unclassified_archive_backlog(self):
        new = self.original('2026-10-02T000000_new.jpg')
        with patch.object(ingest.classifier, 'classify', return_value=record(self.url(new))) as classify:
            self.assertEqual(self.classify(classification_paths=[new, new]), 1)
        self.assertEqual(classify.call_args.args[0], self.url(new))
        self.assertEqual(classify.call_args.kwargs['image_path'], new)
        self.assertNotIn(self.url(self.first), self.output.read_text())
        # No new photos must never cause the old, unclassified original to run.
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}), \
             patch.object(ingest.classifier, 'classify') as classify:
            self.assertEqual(self.classify(classification_paths=[]), 0)
        classify.assert_not_called()

    def test_scoped_batch_failure_retries_only_failed_new_originals(self):
        new = self.original('2026-10-02T000000_new.jpg')
        other = self.original('2026-10-02T000001_other.jpg')
        paths = [new, other]
        with patch.object(ingest.classifier, 'classify', side_effect=[
            record(self.url(new)), ingest.classifier.ClassificationError('failed image'),
        ]):
            with self.assertRaisesRegex(ingest.classifier.ClassificationError, 'publishing aborted'):
                self.classify(classification_paths=paths)
        with patch.object(ingest.classifier, 'classify', return_value=record(self.url(other))) as classify:
            self.assertEqual(self.classify(classification_paths=paths), 1)
        classify.assert_called_once()
        self.assertEqual(classify.call_args.args[0], self.url(other))
        self.assertNotIn(self.url(self.first), self.output.read_text())

    def test_append_handles_missing_trailing_newline_and_preserves_other_archive_records(self):
        previous_url = 'https://archiv1.derrosarotepanzer.com/images/2025/01/old.jpg'
        self.output.write_text(json.dumps(record(previous_url)))
        with patch.object(ingest.classifier, 'classify', return_value=record(self.url(self.first))):
            self.classify()
        rows = [json.loads(line) for line in self.output.read_text().splitlines()]
        self.assertEqual([row['url'] for row in rows], [previous_url, self.url(self.first)])
        self.assertIn('2025/01/old.jpg', json.loads(self.compact.read_text()))

    def test_per_image_failure_keeps_successes_and_retry_only_classifies_failed_image(self):
        second = self.original('2026-10-01T000001_2.jpg')
        with patch.object(ingest.classifier, 'classify', side_effect=[
            record(self.url(self.first)), ingest.classifier.ClassificationError('failed image'),
        ]):
            with self.assertRaisesRegex(ingest.classifier.ClassificationError, 'publishing aborted'):
                self.classify()
        self.assertEqual(len(self.output.read_text().splitlines()), 1)
        self.assertEqual(set(json.loads(self.compact.read_text())), {'2026/10/' + self.first.name})
        with patch.object(ingest.classifier, 'classify', return_value=record(self.url(second))) as classify:
            self.assertEqual(self.classify(), 1)
        self.assertEqual(classify.call_args.args[0], self.url(second))
        self.assertEqual(len(self.output.read_text().splitlines()), 2)

    def test_fatal_api_error_stops_submission_and_preserves_finished_records(self):
        self.original('2026-10-01T000001_2.jpg')
        self.original('2026-10-01T000002_3.jpg')
        with patch.object(ingest.classifier, 'classify', side_effect=[
            record(self.url(self.first)), ingest.classifier.APIConfigurationError('invalid model'),
        ]) as classify:
            with self.assertRaisesRegex(ingest.classifier.APIConfigurationError, 'invalid model'):
                self.classify()
        self.assertEqual(classify.call_count, 2)
        self.assertEqual(len(self.output.read_text().splitlines()), 1)
        self.assertTrue(self.compact.exists())

    def test_missing_key_fails_before_calls_or_output_creation(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}), patch.object(ingest.classifier, 'classify') as classify:
            with self.assertRaisesRegex(ingest.classifier.APIConfigurationError, 'OPENAI_API_KEY'):
                self.classify()
        classify.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_ingest_pipeline_includes_classification(self):
        steps = Mock()
        with patch.object(ingest, 'update_indexes', steps.indexes), \
             patch.object(ingest, 'update_thumbnails', steps.thumbnails), \
             patch.object(ingest, 'update_classifications', steps.classify):
            ingest.ingest_archive(self.archive, self.www)
        self.assertEqual(steps.mock_calls, [
            call.indexes(self.archive), call.thumbnails(self.archive, self.www),
            call.classify(self.archive, self.www),
        ])

    def test_ingest_pipeline_passes_an_explicit_empty_classification_batch(self):
        with patch.object(ingest, 'update_indexes'), patch.object(ingest, 'update_thumbnails'), \
             patch.object(ingest, 'update_classifications') as classify:
            ingest.ingest_archive(self.archive, self.www, classification_paths=[])
        classify.assert_called_once_with(self.archive, self.www, classification_paths=[])

    def test_standalone_cli_runs_pipeline_and_returns_failure_for_classification_errors(self):
        with patch.object(ingest, 'ingest_archive') as pipeline:
            self.assertEqual(ingest.main([str(self.archive), '--www-repo', str(self.www)]), 0)
        pipeline.assert_called_once_with(self.archive.resolve(), self.www.resolve())
        with patch.object(ingest, 'ingest_archive', side_effect=ingest.classifier.ClassificationError('failed')):
            self.assertEqual(ingest.main([str(self.archive), '--www-repo', str(self.www)]), 1)

    def test_classification_failure_prevents_git_and_staging_cleanup(self):
        failure = ingest.classifier.ClassificationError('classification failed')
        module = SimpleNamespace(ingest_archive=Mock(side_effect=failure))
        staging = self.www / 'images/2026/10'
        staging.mkdir(parents=True)
        (staging / self.first.name).write_bytes(self.first.read_bytes())
        with patch.dict('sys.modules', {'ingest_uploads': module}), \
             patch.object(sync, '_update_images', return_value=[(staging, self.archive)]), \
             patch.object(sync, '_publish_generated') as publish, \
             patch.object(sync, '_update_dir_index') as cleanup, \
             patch.object(sync, '_commit_www') as www_publish:
            with self.assertRaisesRegex(ingest.classifier.ClassificationError, 'classification failed'):
                sync.main([])
        publish.assert_not_called()
        cleanup.assert_not_called()
        www_publish.assert_not_called()
        self.assertTrue((staging / self.first.name).exists())

    def test_classification_finishes_before_git_publish(self):
        steps = Mock()
        module = SimpleNamespace(ingest_archive=steps.ingest)
        with patch.dict('sys.modules', {'ingest_uploads': module}), \
             patch.object(sync, '_publish_generated', steps.publish):
            sync._commit_archive(self.archive)
        self.assertEqual(steps.mock_calls, [call.ingest(self.archive, sync.ROOT_DIR), call.publish(['images/'])])


if __name__ == '__main__':
    unittest.main()
