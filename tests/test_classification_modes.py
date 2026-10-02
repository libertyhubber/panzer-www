"""Offline tests for selective archive updates and spending previews."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import classify_images as cli
from tests.test_classify_images import RESULT, URL, record


def api_result(result, model):
    return json.dumps({
        "id": "response", "status": "completed", "model": model,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "output": [{"content": [{"type": "output_text", "text": json.dumps(result)}]}],
    }).encode()


class SelectiveClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "results.jsonl"
        self.image = self.root / "image.jpg"
        self.image.write_bytes(b"\xff\xd8\xffimage")
        self.options = ["--archive", "--output", str(self.output), "--concurrency", "1"]

    def save(self, *records):
        self.output.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def run_cli(self, options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli.main(options)
        return status, stdout.getvalue(), stderr.getvalue()

    def classify(self, mode, previous=None):
        return cli.classify(URL, cli.DEFAULT_MODEL, "secret", mode=mode, previous=previous,
                            image_path=self.image, timeout=10, retries=0)

    def test_sol_cost_cache_and_long_context(self):
        usage = {"input_tokens": 1000, "output_tokens": 500,
                 "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 100},
                 "output_tokens_details": {"reasoning_tokens": 300}}
        cost = cli.estimate_cost(usage, "gpt-6.1-sol")
        self.assertEqual(cost["rates"], (2, 0.1, 2.5, 10))
        self.assertAlmostEqual(cost["total_usd"], 0.00667)
        usage = {"input_tokens": 272001, "output_tokens": 100}
        cost = cli.estimate_cost(usage, "gpt-6.1-sol")
        self.assertEqual(cost["rates"], (4, 0.2, 5, 15))
        self.assertAlmostEqual(cost["total_usd"], 1.089504)

    def test_ocr_only_preserves_tags_and_metadata(self):
        previous = record()
        previous["schema_version"] = 4
        previous["tags_call"]["response_id"] = "old-tags"
        previous["translation_call"] = {"model": "gpt-6-luna", "reasoning_effort": "low",
                                        "response_id": "old-translations"}
        original = copy.deepcopy(previous)
        content = {key: value for key, value in RESULT.items() if key != "tags"}
        content["text"] = "New OCR\n@RosarotePanzer"
        with patch.object(cli, "request_bytes", return_value=api_result(content, cli.DEFAULT_MODEL)) as fetch:
            result = self.classify("ocr", previous)
        self.assertEqual(fetch.call_count, 1)
        payload = json.loads(fetch.call_args.args[0].data)
        self.assertEqual(payload["text"]["format"]["schema"], cli.CONTENT_SCHEMA)
        self.assertEqual(result["classification"]["text"], "New OCR")
        self.assertEqual(result["classification"]["tags"], RESULT["tags"])
        self.assertEqual(result["tags_call"], previous["tags_call"])
        self.assertEqual(result["translation_call"], previous["translation_call"])
        self.assertEqual(result["stage_schema_versions"], {"content": 8, "tags": 4})
        self.assertEqual(result["schema_version"], 4)
        self.assertEqual(previous, original, "never mutate saved records")
        self.assertTrue(cli.record_complete(result, cli.DEFAULT_MODEL, "low", mode="ocr"))
        self.assertFalse(cli.record_complete(result, cli.DEFAULT_MODEL, "low"))

    def test_new_ocr_only_record_is_exportable_but_not_fully_classified(self):
        content = {key: value for key, value in RESULT.items() if key != "tags"}
        with patch.object(cli, "request_bytes", return_value=api_result(content, cli.DEFAULT_MODEL)):
            result = self.classify("ocr")
        self.assertEqual(result["classification"]["tags"], [])
        self.assertIsNone(result["tags_call"])
        self.assertEqual(result["schema_version"], 0)
        self.save(result)
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, mode="ocr"), {URL})
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL), set())
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, min_schema=4), set())
        from scripts.export_classifications import build_index
        result["url"] = "https://archiv0.derrosarotepanzer.com/images/2021/01/image.jpg"
        self.save(result)
        self.assertEqual(build_index(self.output)["2021/01/image.jpg"]["tags"], [])

    def test_ocr_only_upgrades_legacy_records_with_missing_content_fields(self):
        previous = record()
        previous["schema_version"] = 1
        del previous["classification"]["description"]
        del previous["classification"]["tags"]
        self.save(previous)
        previous = cli.latest_records(self.output)[URL]
        self.assertEqual(cli.stage_versions(previous)["tags"], 0)
        content = {key: value for key, value in RESULT.items() if key != "tags"}
        with patch.object(cli, "request_bytes", return_value=api_result(content, cli.DEFAULT_MODEL)):
            result = self.classify("ocr", previous)
        self.assertEqual(result["classification"]["description"], RESULT["description"])
        self.assertEqual(result["classification"]["tags"], [])
        self.assertEqual(result["stage_schema_versions"], {"content": 8, "tags": 0})

    @patch.object(cli, "archive_urls")
    def test_partial_archive_appends_and_resumes_real_stage_results(self, discover):
        previous = record()
        previous["schema_version"] = 4
        self.save(previous)
        discover.side_effect = lambda *a, **kw: iter([URL])
        content = {key: value for key, value in RESULT.items() if key != "tags"}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch.object(cli, "request_bytes", side_effect=[
            b"\xff\xd8\xffimage", api_result(content, cli.DEFAULT_MODEL),
        ]) as fetch:
            status, _, _ = self.run_cli(self.options + ["--ocr-only"])
        self.assertEqual(status, 0)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(cli.latest_records(self.output)[URL]["stage_schema_versions"], {"content": 8, "tags": 4})
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch.object(cli, "request_bytes") as fetch:
            status, _, _ = self.run_cli(self.options + ["--ocr-only", "--min-schema", "8"])
        self.assertEqual(status, 0)
        fetch.assert_not_called()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch.object(cli, "request_bytes", side_effect=[
            b"\xff\xd8\xffimage", api_result({"tags": []}, cli.TAGS_MODEL),
        ]) as fetch:
            status, _, _ = self.run_cli(self.options + ["--tags-only"])
        self.assertEqual(status, 0)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL), {URL})
        self.assertEqual(len(self.output.read_text().splitlines()), 3)

    def test_tags_only_preserves_content_and_content_metadata(self):
        previous = record(model="old-content-model", reasoning_effort="high")
        previous.update(schema_version=4, response_id="old-content", usage={"input_tokens": 999, "output_tokens": 777})
        original = copy.deepcopy(previous)
        with patch.object(cli, "request_bytes", side_effect=[
            api_result({"tags": ["katze", "Katze", "cat"]}, cli.TAGS_MODEL),
        ]) as fetch:
            result = self.classify("tags", previous)
        self.assertEqual(fetch.call_count, 1)
        payload = json.loads(fetch.call_args_list[0].args[0].data)
        self.assertEqual(payload["model"], "gpt-6-luna")
        self.assertEqual(payload["reasoning"], {"effort": "medium"})
        self.assertIsNone(result["translation_call"])
        self.assertEqual(result["classification"], {**RESULT, "tags": ["katze", "cat"]})
        for key in ("model", "reasoning_effort", "response_id", "usage"):
            self.assertEqual(result[key], previous[key])
        self.assertEqual(result["stage_schema_versions"], {"content": 4, "tags": 8})
        self.assertEqual(result["schema_version"], 4)
        self.assertEqual(previous, original)
        self.assertTrue(cli.record_complete(result, "irrelevant", "none", mode="tags"))
        self.assertFalse(cli.record_complete(result, "irrelevant", "none"))

    def test_both_partial_updates_finish_schema_upgrade(self):
        previous = record()
        previous["schema_version"] = 4
        content = {key: value for key, value in RESULT.items() if key != "tags"}
        with patch.object(cli, "request_bytes", return_value=api_result(content, cli.DEFAULT_MODEL)):
            result = self.classify("ocr", previous)
        with patch.object(cli, "request_bytes", return_value=api_result({"tags": []}, cli.TAGS_MODEL)) as fetch:
            result = self.classify("tags", result)
        self.assertEqual(fetch.call_count, 1, "tags-only always uses one request")
        self.assertEqual(result["schema_version"], cli.SCHEMA_VERSION)
        self.assertTrue(cli.record_complete(result, cli.DEFAULT_MODEL, "low"))

    def test_tags_without_content_fails_before_any_requests(self):
        with patch.object(cli, "request_bytes") as fetch:
            with self.assertRaisesRegex(cli.ClassificationError, "requires existing OCR"):
                self.classify("tags")
        fetch.assert_not_called()

    def test_partial_failure_does_not_mutate_previous(self):
        previous = record()
        original = copy.deepcopy(previous)
        with patch.object(cli, "request_bytes", side_effect=cli.ClassificationError("bilingual tags failed")):
            with self.assertRaisesRegex(cli.ClassificationError, "bilingual tags failed"):
                self.classify("tags", previous)
        self.assertEqual(previous, original)

    def test_partial_diagnostics_exclude_preserved_calls(self):
        previous = record()
        previous.update(usage={"input_tokens": 123, "output_tokens": 456}, response_id="do-not-log-content")
        previous["tags_call"]["response_id"] = "do-not-log-tags"
        with patch.object(cli, "request_bytes", return_value=api_result({"tags": []}, cli.TAGS_MODEL)):
            result = self.classify("tags", previous)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            cli.log_api_usage(result, cli.DEFAULT_MODEL)
        self.assertIn("tags model=", stderr.getvalue())
        self.assertNotIn("do-not-log", stderr.getvalue())
        content = {key: value for key, value in RESULT.items() if key != "tags"}
        with patch.object(cli, "request_bytes", return_value=api_result(content, cli.DEFAULT_MODEL)):
            result = self.classify("ocr", previous)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            cli.log_api_usage(result, cli.DEFAULT_MODEL)
        self.assertNotIn("tags model=", stderr.getvalue())
        self.assertNotIn("do-not-log", stderr.getvalue())

    def test_minimum_schema_ignores_model_and_effort(self):
        old = record(model="old", reasoning_effort="high")
        old["schema_version"] = 4
        self.save(old)
        self.assertEqual(cli.completed_urls(self.output, "new", "none", min_schema=4), {URL})
        self.assertEqual(cli.completed_urls(self.output, "new", "none", min_schema=5), set())
        self.assertEqual(cli.completed_urls(self.output, "new", "none"), set())
        old["schema_version"] = 9
        self.save(old)
        self.assertEqual(cli.completed_urls(self.output, "new", "none", min_schema=8), {URL})

    def test_legacy_sol_tags_can_be_preserved_with_minimum_schema(self):
        previous = record()
        previous["tags_call"] = {"model": "gpt-6.1-sol", "reasoning_effort": "low"}
        previous["translation_call"] = {"model": "gpt-6-luna", "reasoning_effort": "low"}
        self.save(previous)
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL), set())
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, mode="tags"), set())
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, min_schema=8), {URL})
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, mode="tags", min_schema=8), {URL})
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, mode="ocr"), {URL})

    def test_last_record_wins_for_resume(self):
        current = record()
        older = record()
        older["schema_version"] = 4
        self.save(current, older)
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL), set())
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, min_schema=4), {URL})

    def test_invalid_stage_versions_rejected(self):
        for versions in ({"content": 8}, {"content": 8, "tags": -1}, {"content": 8, "tags": 4}):
            with self.subTest(versions=versions):
                self.save({**record(), "stage_schema_versions": versions})
                with self.assertRaisesRegex(cli.ClassificationError, "invalid resume record"):
                    cli.latest_records(self.output)

    @patch.object(cli, "archive_urls")
    @patch.object(cli, "classify")
    def test_dry_counts_same_selection_as_run_and_never_writes(self, classify, discover):
        old, current = record(), record(URL + "?current")
        old["schema_version"] = 4
        self.save(old, current)
        original = self.output.read_bytes()
        discover.side_effect = lambda *a, **kw: iter([URL, URL + "?current", URL + "?missing"])
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            status, stdout, _ = self.run_cli(self.options + ["--dry", "--min-schema", "4"])
        self.assertEqual(status, 0)
        self.assertIn("1 images affected; up to 2 API calls", stdout)
        self.assertIn("Approximate cost: $", stdout)
        self.assertIn("fallback:", stdout)
        self.assertEqual(self.output.read_bytes(), original)
        classify.assert_not_called()
        classify.side_effect = lambda url, *a, **kw: record(url)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            status, _, _ = self.run_cli(self.options + ["--min-schema", "4"])
        self.assertEqual(status, 0)
        self.assertEqual(classify.call_args.args[0], URL + "?missing")
        self.assertEqual(classify.call_count, 1)

    @patch.object(cli, "archive_urls")
    @patch.object(cli, "classify")
    def test_tags_archive_skips_missing_and_preserves_previous(self, classify, discover):
        old = record()
        old["schema_version"] = 4
        self.save(old)
        discover.side_effect = lambda *a, **kw: iter([URL + "?missing", URL])
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            status, stdout, _ = self.run_cli(self.options + ["--tags-only", "--dry"])
        self.assertEqual(status, 0)
        self.assertIn("1 images affected; up to 1 API calls", stdout)
        self.assertIn("1 encountered archive images skipped", stdout)
        self.assertNotIn("content (", stdout)
        classify.assert_not_called()
        classify.return_value = old
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            status, _, stderr = self.run_cli(self.options + ["--tags-only"])
        self.assertEqual(status, 0)
        self.assertIn("1 encountered archive images skipped", stderr)
        classify.assert_called_once()
        self.assertEqual(classify.call_args.kwargs["previous"], old)
        self.assertEqual(classify.call_args.kwargs["mode"], "tags")

    @patch.object(cli, "archive_urls")
    @patch.object(cli, "classify")
    def test_ocr_partial_run_resumes_by_selected_stage(self, classify, discover):
        old = record()
        old.update(schema_version=4, stage_schema_versions={"content": 8, "tags": 4})
        self.save(old)
        discover.side_effect = lambda *a, **kw: iter([URL])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            status, _, _ = self.run_cli(self.options + ["--ocr-only"])
        self.assertEqual(status, 0)
        classify.assert_not_called()
        classify.return_value = old
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            status, _, _ = self.run_cli(self.options + ["--ocr-only", "--force"])
        self.assertEqual(status, 0)
        self.assertEqual(classify.call_args.kwargs["mode"], "ocr")
        self.assertEqual(classify.call_args.kwargs["previous"], old)

    @patch.object(cli, "classify")
    def test_explicit_tags_only_missing_record_fails_preflight(self, classify):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            status, stdout, stderr = self.run_cli([URL, "--tags-only", "--output", str(self.output)])
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertIn("lack saved OCR", stderr)
        classify.assert_not_called()

    def test_forecast_uses_stage_usage_and_includes_reasoning_once(self):
        saved = record()
        saved["usage"] = {"input_tokens": 1000, "output_tokens": 500,
                          "output_tokens_details": {"reasoning_tokens": 300}}
        saved["tags_call"]["usage"] = {"input_tokens": 400, "output_tokens": 100}
        self.assertIsNone(saved["translation_call"])
        args = cli.build_parser().parse_args(["--archive", "--dry"])
        args.reasoning_effort = "low"
        result = cli.forecast(100, {URL: saved}, args)
        self.assertAlmostEqual(result["total"], 0.044)
        self.assertEqual(result["calls"], 200)
        self.assertTrue(all("historical mean" in stage["source"] for stage in result["stages"]))
        args.mode = "ocr"
        self.assertAlmostEqual(cli.forecast(100, {URL: saved}, args)["total"], 0.035)
        args.mode = "tags"
        self.assertAlmostEqual(cli.forecast(100, {URL: saved}, args)["total"], 0.009)

    def test_forecast_excludes_legacy_separate_translation_usage(self):
        saved = record()
        # Even matching model/effort samples must not estimate the old split call.
        saved['tags_call']['usage'] = {'input_tokens': 999999, 'output_tokens': 999999}
        saved['translation_call'] = {'model': 'gpt-6-luna', 'reasoning_effort': 'low',
                                     'usage': {'input_tokens': 999999, 'output_tokens': 999999}}
        args = cli.build_parser().parse_args(['--archive', '--tags-only', '--dry'])
        args.reasoning_effort = 'low'
        estimate = cli.forecast(1, {URL: saved}, args)
        self.assertEqual(estimate['calls'], 1)
        self.assertEqual([stage['stage'] for stage in estimate['stages']], ['tags'])
        self.assertIn('fallback:', estimate['stages'][0]['source'])
        self.assertAlmostEqual(estimate['total'], (799 * 0.1 + 281 * 0.5) / 1_000_000)

    @patch.object(cli, "archive_urls")
    @patch.object(cli, "classify")
    def test_dry_force_limit_zero_and_unknown_pricing(self, classify, discover):
        self.save(record())
        discover.side_effect = lambda *a, **kw: iter([URL, URL + "?2", URL + "?3"])
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            status, stdout, _ = self.run_cli(self.options + ["--dry", "--force", "--limit", "2", "--ocr-only"])
            self.assertEqual(status, 0)
            self.assertIn("2 images affected; up to 2 API calls", stdout)
            status, stdout, _ = self.run_cli(self.options + ["--dry", "--min-schema", "4", "--limit", "1", "--model", "unknown"])
            self.assertEqual(status, 0)
            self.assertIn("Approximate cost: unavailable", stdout)
            status, stdout, _ = self.run_cli(self.options + ["--dry", "--force", "--model", "unknown", "--pricing", "1,0.1,1.25,5"])
            self.assertEqual(status, 0)
            self.assertNotIn("cost unavailable", stdout)
            discover.side_effect = lambda *a, **kw: iter([URL])
            status, stdout, _ = self.run_cli(self.options + ["--dry"])
            self.assertEqual(status, 0)
            self.assertIn("0 images affected; up to 0 API calls", stdout)
            self.assertIn("Approximate cost: $0.00", stdout)
        classify.assert_not_called()

    @patch.object(cli, "archive_urls")
    @patch.object(cli, "classify")
    def test_progress_total_respects_schema_resume_and_limit(self, classify, discover):
        accepted = record()
        accepted["schema_version"] = 4
        self.save(accepted)
        discover.return_value = iter([URL, URL + "?new1", URL + "?new2", URL + "?new3"])
        classify.side_effect = lambda url, *a, **kw: record(url)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            status, stdout, stderr = self.run_cli(self.options + ["--min-schema", "4", "--limit", "2"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout, "")
        self.assertIn("Discovering pending archive images...", stderr)
        self.assertIn("Classifying 2 images", stderr)
        self.assertIn("[1/2] 50.0% | 1 classified, 0 failed", stderr)
        self.assertIn("[2/2] 100.0% | 2 classified, 0 failed", stderr)
        self.assertEqual(classify.call_count, 2)

    @patch.object(cli, "archive_urls")
    @patch.object(cli, "classify")
    def test_progress_failures_count_toward_completion(self, classify, discover):
        discover.return_value = iter([URL, URL + "?2"])
        classify.side_effect = [cli.ClassificationError("bad image"), record(URL + "?2")]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            status, _, stderr = self.run_cli(self.options)
        self.assertEqual(status, 1)
        self.assertIn("[1/2] 50.0% | 0 classified, 1 failed", stderr)
        self.assertIn("[2/2] 100.0% | 1 classified, 1 failed", stderr)

    @patch.object(cli, "archive_urls")
    @patch.object(cli, "classify")
    def test_progress_total_excludes_tag_only_skips(self, classify, discover):
        old = record()
        old["schema_version"] = 4
        self.save(old)
        discover.return_value = iter([URL + "?missing", URL])
        classify.return_value = old
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            status, _, stderr = self.run_cli(self.options + ["--tags-only"])
        self.assertEqual(status, 0)
        self.assertIn("Classifying 1 images", stderr)
        self.assertIn("[1/1] 100.0%", stderr)
        self.assertIn("1 encountered archive images skipped", stderr)

    @patch.object(cli, "archive_urls")
    @patch.object(cli, "classify")
    def test_empty_progress_run_does_not_write_or_call_api(self, classify, discover):
        self.save(record())
        original = self.output.read_bytes()
        discover.return_value = iter([URL])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            status, _, stderr = self.run_cli(self.options)
        self.assertEqual(status, 0)
        self.assertIn("Classifying 0 images", stderr)
        self.assertNotIn("[0/0]", stderr)
        self.assertEqual(self.output.read_bytes(), original)
        classify.assert_not_called()

    @patch.object(cli, "classify")
    def test_multiple_url_progress_keeps_stdout_jsonl(self, classify):
        classify.side_effect = lambda url, *a, **kw: record(url)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            status, stdout, stderr = self.run_cli([URL, URL + "?2", "--concurrency", "1"])
        self.assertEqual(status, 0)
        self.assertEqual([json.loads(line)["url"] for line in stdout.splitlines()], [URL, URL + "?2"])
        self.assertIn("[1/2] 50.0%", stderr)
        self.assertIn("[2/2] 100.0%", stderr)
        self.assertNotIn("Classifying", stdout)

    @patch.object(cli, "archive_urls")
    @patch.object(cli, "classify")
    def test_discovery_finishes_before_paid_work(self, classify, discover):
        def broken_indexes():
            yield URL
            raise cli.ClassificationError("bad monthly index")
        discover.return_value = broken_indexes()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            status, _, stderr = self.run_cli(self.options)
        self.assertEqual(status, 1)
        self.assertIn("bad monthly index", stderr)
        classify.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_cli_option_validation(self):
        for extra in (["--ocr-only", "--tags-only"], ["--dry", "--dry-run"],
                      ["--min-schema", "0"], ["--min-schema", "9"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(self.options + extra)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main([URL, "--min-schema", "4", "--dry"])


if __name__ == "__main__":
    unittest.main()
