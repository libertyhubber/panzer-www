"""Offline tests for text-only language labeling and append-only migration."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import backfill_tag_languages as migration
from scripts import classify_images as classifier
from functools import partial
from scripts.export_classifications import export_index as _export_index

export_index = partial(_export_index, monthly=False)
from tests.test_classify_images import record, URL


def legacy_record(url=URL, tags=None):
    value = record(url)
    value.pop("tag_format_version")
    value["classification"].pop("tags_de")
    value["classification"].pop("tags_en")
    value["classification"]["tags"] = tags if tags is not None else ["Katze", "cat", "Bitcoin"]
    value.update(schema_version=4, stage_schema_versions={"content": 4, "tags": 8},
                 classified_at="original-time", response_id="original-content")
    return value


def response(rows):
    return json.dumps({
        "id": "resp_languages", "model": "gpt-6-luna", "status": "completed",
        "usage": {"input_tokens": 200, "output_tokens": 100},
        "output": [{"content": [{"type": "output_text", "text": json.dumps({"labels": rows})}]}],
    }).encode()


class TagLanguageBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "classifications.jsonl"
        self.cache = self.root / "cache.jsonl"
        self.overrides = self.root / "overrides.json"
        self.report = self.root / "report.json"
        self.options = ["--input", str(self.source), "--cache", str(self.cache)]
        self.write_records(legacy_record())

    def write_records(self, *records):
        self.source.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

    def cache_labels(self, labels):
        self.cache.write_text(json.dumps({"labels": labels}), encoding="utf-8")

    def run_cli(self, *options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = migration.main(self.options + list(options))
        return status, output.getvalue()

    def test_default_is_read_only_without_key_or_network(self):
        before = self.source.read_bytes()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch.object(classifier, "request_bytes") as request:
            status, output = self.run_cli()
        self.assertEqual(status, 0)
        self.assertIn("3 tags / 1 text calls", output)
        self.assertIn("no API calls", output)
        request.assert_not_called()
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.source.read_bytes(), before)

    def test_parser_defaults_and_positive_bounds(self):
        parser = migration.build_parser()
        args = parser.parse_args([])
        self.assertEqual(args.batch_size, 40)
        self.assertEqual(args.concurrency, 8)
        for option in ("--batch-size", "--concurrency"):
            for value in ("0", "-1"):
                with self.subTest(option=option, value=value), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args([option, value])

    def test_parallel_batches_are_bounded_cached_in_completion_order_by_one_writer(self):
        tags = [f"tag{n}" for n in range(7)]
        self.write_records(legacy_record(tags=tags))
        barrier = threading.Barrier(3)
        release = threading.Event()
        lock = threading.Lock()
        active = maximum = 0
        writer_threads = []
        main_thread = threading.get_ident()
        append = migration.append_jsonl

        def work(batch, args, api_key):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                if batch[0] in tags[:3]:
                    barrier.wait(timeout=3)
                    if batch[0] != "tag2" and not release.wait(3):
                        raise AssertionError("later batch was not cached promptly")
                return {tag: "de" for tag in batch}, json.loads(response([]))
            finally:
                with lock:
                    active -= 1

        def save(path, rows):
            writer_threads.append(threading.get_ident())
            count = append(path, rows)
            if "tag2" in rows[0]["labels"]:
                release.set()
            return count

        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                    migration, "label_batch", side_effect=work), patch.object(
                    migration, "append_jsonl", side_effect=save):
                status, output = self.run_cli("--label", "--batch-size", "1", "--concurrency", "3")
        finally:
            release.set()
        self.assertEqual(status, 0)
        self.assertEqual(maximum, 3)
        self.assertEqual(writer_threads, [main_thread] * 7)
        rows = [json.loads(line) for line in self.cache.read_text().splitlines()]
        self.assertEqual(rows[0]["labels"], {"tag2": "de"}, "slow earlier batches must not block saving")
        self.assertEqual(migration.load_labels(self.cache), {tag: "de" for tag in tags})
        progress = [line for line in output.splitlines() if line.startswith("Cached ")]
        self.assertEqual([line.split(";")[0] for line in progress], [f"Cached {n}/7 labels" for n in range(1, 8)])
        self.assertIn("run total ~$0.000490", progress[-1])

    def test_parallel_failure_continues_submission_and_saves_other_batches(self):
        self.write_records(legacy_record(tags=["bad", "good", "later"]))
        barrier = threading.Barrier(2)
        release = threading.Event()
        called = []
        failed = threading.Event()
        original_wait = classifier.wait

        def work(batch, args, api_key):
            called.extend(batch)
            if batch[0] in {"bad", "good"}:
                barrier.wait(timeout=3)
            if batch == ["bad"]:
                raise classifier.ClassificationError("failed batch")
            if batch == ["good"] and not release.wait(3):
                raise AssertionError("in-flight success was not drained")
            return {batch[0]: "de"}, json.loads(response([]))

        def wait(futures, **kwargs):
            if failed.is_set():
                release.set()
            done, pending = original_wait(futures, **kwargs)
            if any(isinstance(future.exception(), classifier.ClassificationError) for future in done):
                failed.set()
            return done, pending

        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                    migration, "label_batch", side_effect=work), patch.object(
                    classifier, "wait", side_effect=wait), contextlib.redirect_stderr(io.StringIO()):
                status, output = self.run_cli("--label", "--batch-size", "1", "--concurrency", "2")
        finally:
            release.set()
        self.assertEqual(status, 1, "report failure only after processing all batches")
        self.assertCountEqual(called, ["bad", "good", "later"])
        self.assertEqual(migration.load_labels(self.cache), {"good": "de", "later": "de"})
        self.assertIn("attempted 3 tags; cached 2 labels; 1 incomplete/failed batches", output)
        self.assertIn("run total (known costs) ~$0.000140", output)

    def test_configuration_error_still_stops_submission_and_drains_success(self):
        self.write_records(legacy_record(tags=["bad", "good", "never-started"]))
        barrier = threading.Barrier(2)
        release = threading.Event()
        observed = threading.Event()
        called = []
        original_wait = classifier.wait

        def work(batch, args, api_key):
            called.extend(batch)
            barrier.wait(timeout=3)
            if batch == ["bad"]:
                raise classifier.APIConfigurationError("invalid model")
            if not release.wait(3):
                raise AssertionError("success was not drained")
            return {"good": "de"}, json.loads(response([]))

        def wait(futures, **kwargs):
            if observed.is_set():
                release.set()
            done, pending = original_wait(futures, **kwargs)
            if any(isinstance(future.exception(), classifier.APIConfigurationError) for future in done):
                observed.set()
            return done, pending

        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                    migration, "label_batch", side_effect=work), patch.object(classifier, "wait", side_effect=wait):
                with self.assertRaisesRegex(classifier.APIConfigurationError, "invalid model"):
                    self.run_cli("--label", "--batch-size", "1", "--concurrency", "2")
        finally:
            release.set()
        self.assertCountEqual(called, ["bad", "good"])
        self.assertEqual(migration.load_labels(self.cache), {"good": "de"})

    def test_interrupt_drains_success_to_cache_before_propagating(self):
        self.write_records(legacy_record(tags=["first", "never-started"]))
        started = threading.Event()
        release = threading.Event()
        called = []
        original_wait = classifier.wait
        interrupted = False

        def work(batch, args, api_key):
            called.extend(batch)
            started.set()
            if not release.wait(3):
                raise AssertionError("interrupted worker was not drained")
            return {"first": "de"}, json.loads(response([]))

        def wait(futures, **kwargs):
            nonlocal interrupted
            if not interrupted:
                self.assertTrue(started.wait(3))
                interrupted = True
                raise KeyboardInterrupt()
            release.set()
            return original_wait(futures, **kwargs)

        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                    migration, "label_batch", side_effect=work), patch.object(
                    classifier, "wait", side_effect=wait):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_cli("--label", "--batch-size", "1", "--concurrency", "1")
        finally:
            release.set()
        self.assertEqual(called, ["first"])
        self.assertEqual(migration.load_labels(self.cache), {"first": "de"})

    def test_default_batch_size_respects_tag_limit_and_existing_cache(self):
        tags = [f"tag{n:03d}" for n in range(402)]
        self.write_records(legacy_record(tags=tags))
        self.cache_labels({tags[0]: "de"})
        batches = []

        def work(batch, args, api_key):
            batches.append(batch)
            return {tag: "de" for tag in batch}, json.loads(response([]))

        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                migration, "label_batch", side_effect=work):
            _, output = self.run_cli("--label", "--limit", "301", "--concurrency", "1")
        self.assertEqual([len(batch) for batch in batches], [40] * 7 + [21])
        self.assertEqual([tag for batch in batches for tag in batch], tags[1:302])
        self.assertIn("Cached 301/301 labels", output)
        self.assertEqual(len(migration.load_labels(self.cache)), 302)

    def test_deduplicates_across_images_and_uses_latest_records(self):
        self.write_records(legacy_record(tags=["obsolete"]), legacy_record(),
                           legacy_record(URL + "?2", [" CAT ", "KATZE"]))
        records = classifier.latest_records(self.source)
        self.assertEqual(migration.pending_tags(records), {"cat", "katze", "bitcoin"})
        preview = migration.preview(records, {}, 2)
        self.assertEqual(preview["text_calls"], 2)
        self.assertEqual(preview["unresolved_records"], 0)
        self.assertEqual(preview["ready_to_apply"], 2)

    def test_shared_names_and_case_variants_preserve_originals(self):
        tags = ["Katze", "KATZE", "cat", "Bitcoin", "bitcoin"]
        self.assertEqual(migration.split_tags(tags, {"katze": "de", "cat": "en", "bitcoin": "both"}), {
            "tags": ["Bitcoin"], "tags_de": ["Katze"], "tags_en": ["cat"],
        })
        self.assertEqual(migration.split_tags(tags, {}), {
            "tags": ["Katze", "cat", "Bitcoin"], "tags_de": [], "tags_en": [],
        })
        self.assertEqual(migration.split_tags(tags, {"katze": "de", "cat": "en", "bitcoin": "unknown"}), {
            "tags": ["Bitcoin"], "tags_de": ["Katze"], "tags_en": ["cat"],
        })

    def test_apply_preserves_history_ocr_tags_versions_and_diagnostics(self):
        original = classifier.latest_records(self.source)[URL]
        self.cache_labels({"katze": "de", "cat": "en", "bitcoin": "both"})
        with patch.object(classifier, "request_bytes") as request:
            status, output = self.run_cli("--apply")
        self.assertEqual(status, 0)
        self.assertIn("Appended 1", output)
        request.assert_not_called()
        rows = [json.loads(line) for line in self.source.read_text().splitlines()]
        self.assertEqual(rows[0], original)
        updated = classifier.latest_records(self.source)[URL]
        self.assertEqual(updated["classification"]["tags_de"], ["Katze"])
        self.assertEqual(updated["classification"]["tags_en"], ["cat"])
        self.assertEqual(updated["classification"]["tags"], ["Bitcoin"])
        for field in original:
            if field != "classification":
                self.assertEqual(updated[field], original[field], field)
        for field in original["classification"]:
            if field not in classifier.TAG_FIELDS:
                self.assertEqual(updated["classification"][field], original["classification"][field], field)
        self.assertEqual(set(classifier.all_tags(updated["classification"])), set(original["classification"]["tags"]))
        self.assertEqual(updated["tag_format_version"], classifier.TAG_FORMAT_VERSION)
        before = self.source.read_bytes()
        self.run_cli("--apply")
        self.assertEqual(self.source.read_bytes(), before, "apply is idempotent")

    def test_unresolved_records_migrate_to_neutral_without_review(self):
        self.cache_labels({"katze": "de", "cat": "en", "bitcoin": "unknown"})
        self.run_cli("--dry", "--report", str(self.report))
        report = json.loads(self.report.read_text())
        self.assertEqual(report["uncertain_tags"], ["bitcoin"])
        self.assertEqual(report["ready_to_apply"], 1)
        self.assertEqual(report["unresolved_records"], 0)
        status, _ = self.run_cli("--apply")
        self.assertEqual(status, 0)
        result = classifier.latest_records(self.source)[URL]["classification"]
        self.assertEqual(result["tags"], ["Bitcoin"])
        self.assertEqual(result["tags_de"], ["Katze"])
        self.assertEqual(result["tags_en"], ["cat"])

    def test_apply_preserves_uncached_tags_in_neutral_list(self):
        resolved = legacy_record(URL, ["Katze", "cat"])
        unresolved = legacy_record(URL + "?2", ["Bitcoin"])
        self.write_records(resolved, unresolved)
        self.cache_labels({"katze": "de", "cat": "en"})
        status, _ = self.run_cli("--apply")
        self.assertEqual(status, 0)
        latest = classifier.latest_records(self.source)
        self.assertEqual(latest[URL + "?2"]["classification"]["tags"], ["Bitcoin"])
        self.assertEqual(latest[URL + "?2"]["classification"]["tags_de"], [])
        self.assertEqual(latest[URL + "?2"]["classification"]["tags_en"], [])
        self.assertIn("tags_de", latest[URL]["classification"])

    def test_existing_split_records_and_manual_corrections_are_preserved(self):
        split = record()
        manual = legacy_record(URL + "?manual")
        manual["model"] = "manual-correction"
        self.write_records(split, manual)
        original = copy.deepcopy(manual)
        self.cache_labels({"katze": "de", "cat": "en", "bitcoin": "both"})
        self.run_cli("--apply")
        latest = classifier.latest_records(self.source)
        self.assertEqual(latest[URL], split)
        self.assertEqual(latest[manual["url"]]["model"], "manual-correction")
        self.assertEqual(latest[manual["url"]]["classification"]["text"], original["classification"]["text"])

    def test_version_one_combined_lists_upgrade_to_neutral_partition(self):
        value = legacy_record(tags=["Katze", "cat", "Bitcoin", "Mystery"])
        value["classification"].update(tags_de=["Katze", "Bitcoin"], tags_en=["cat", "Bitcoin"])
        value["tag_format_version"] = 1
        self.write_records(value)
        self.cache_labels({"katze": "de", "cat": "en", "bitcoin": "both", "mystery": "unknown"})
        status, _ = self.run_cli("--apply")
        self.assertEqual(status, 0)
        updated = classifier.latest_records(self.source)[URL]
        self.assertEqual(updated["tag_format_version"], 2)
        self.assertEqual(updated["classification"]["tags"], ["Bitcoin", "Mystery"])
        self.assertEqual(updated["classification"]["tags_de"], ["Katze"])
        self.assertEqual(updated["classification"]["tags_en"], ["cat"])
        self.assertEqual(set(classifier.all_tags(updated["classification"])), set(value["classification"]["tags"]))
        self.assertEqual(updated["stage_schema_versions"], value["stage_schema_versions"])

    def test_empty_tags_migrate_without_any_language_calls(self):
        self.write_records(legacy_record(tags=[]))
        status, _ = self.run_cli("--apply")
        self.assertEqual(status, 0)
        saved = classifier.latest_records(self.source)[URL]
        self.assertEqual(saved["classification"]["tags_de"], [])
        self.assertEqual(saved["classification"]["tags_en"], [])

    def test_label_uses_text_only_and_cached_batches_resume_without_key(self):
        before = self.source.read_bytes()
        rows = [{"tag": tag, "language": language}
                for tag, language in (("bitcoin", "both"), ("cat", "en"), ("katze", "de"))]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                classifier, "request_bytes", return_value=response(rows)) as request:
            status, _ = self.run_cli("--label")
        self.assertEqual(status, 0)
        request.assert_called_once()
        req = request.call_args.args[0]
        self.assertEqual(req.full_url, classifier.API_URL)
        payload = json.loads(req.data)
        self.assertFalse(payload["store"])
        content = payload["input"][0]["content"]
        self.assertEqual(content, [{"type": "input_text", "text": json.dumps(["bitcoin", "cat", "katze"])}])
        self.assertIn("untrusted", payload["instructions"])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(migration.load_labels(self.cache), {"bitcoin": "both", "cat": "en", "katze": "de"})
        cache_before = self.cache.read_bytes()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch.object(classifier, "request_bytes") as request:
            self.run_cli("--label")
        request.assert_not_called()
        self.assertEqual(self.cache.read_bytes(), cache_before)

    def test_batch_cost_includes_running_total_for_this_run(self):
        responses = [response([{"tag": tag, "language": language}])
                     for tag, language in (("bitcoin", "both"), ("cat", "en"), ("katze", "de"))]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                classifier, "request_bytes", side_effect=responses):
            status, output = self.run_cli("--label", "--batch-size", "1", "--concurrency", "1")
        self.assertEqual(status, 0)
        lines = [line for line in output.splitlines() if line.startswith("Cached ")]
        for line, total in zip(lines, ("0.000070", "0.000140", "0.000210")):
            self.assertIn(f"batch cost ~$0.000070; run total ~${total}", line)
        self.assertEqual(len(lines), 3)

    def test_unavailable_batch_cost_marks_running_total_as_partial(self):
        responses = [response([{"tag": tag, "language": language}])
                     for tag, language in (("bitcoin", "both"), ("cat", "en"), ("katze", "de"))]
        missing_usage = json.loads(responses[1])
        missing_usage.pop("usage")
        responses[1] = json.dumps(missing_usage).encode()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                classifier, "request_bytes", side_effect=responses):
            _, output = self.run_cli("--label", "--batch-size", "1", "--concurrency", "1")
        lines = [line for line in output.splitlines() if line.startswith("Cached ")]
        self.assertIn("batch cost unavailable; run total (known costs) ~$0.000070", lines[1])
        self.assertIn("run total (known costs) ~$0.000140; 1 batch cost(s) unavailable", lines[2])

    def test_partial_responses_cache_only_unambiguous_requested_tags_and_full_cost(self):
        cases = [
            ([{"tag": "bitcoin", "language": "both"}], {"bitcoin": "both"}),
            ([{"tag": "bitcoin", "language": "both"}, {"tag": "cat", "language": "en"},
              {"tag": "cat", "language": "en"}], {"bitcoin": "both", "cat": "en"}),
            ([{"tag": tag, "language": "de"} for tag in ["bitcoin", "cat", "invented"]],
             {"bitcoin": "de", "cat": "de"}),
            ([{"tag": "bitcoin", "language": "both"}, {"tag": "cat", "language": "en"},
              {"tag": "cat", "language": "de"}, {"tag": "katze", "language": "de"}],
             {"bitcoin": "both", "katze": "de"}),
            ([{"tag": "Bitcoin", "language": "both"}], {}),
            ([], {}),
        ]
        for rows, expected in cases:
            self.cache.unlink(missing_ok=True)
            with self.subTest(rows=rows), patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                    classifier, "request_bytes", return_value=response(rows)), contextlib.redirect_stderr(io.StringIO()):
                status, output = self.run_cli("--label")
            self.assertEqual(status, 1)
            self.assertEqual(migration.load_labels(self.cache), expected)
            cached = json.loads(self.cache.read_text())
            self.assertEqual(set(cached["unlabeled_tags"]), {"bitcoin", "cat", "katze"} - expected.keys())
            self.assertEqual(cached["call"]["usage"], {"input_tokens": 200, "output_tokens": 100})
            self.assertIn("run total ~$0.000070", output, "partial responses still incur their full cost")
            self.assertIn(f"Cached {len(expected)}/3 labels", output)

    def test_partial_response_resume_requests_only_missing_tags(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                classifier, "request_bytes", return_value=response([{"tag": "bitcoin", "language": "both"}])), \
                contextlib.redirect_stderr(io.StringIO()):
            status, _ = self.run_cli("--label", "--report", str(self.report))
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(self.report.read_text())["missing_labels"], ["cat", "katze"])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                classifier, "request_bytes", return_value=response([
                    {"tag": "cat", "language": "en"}, {"tag": "katze", "language": "de"},
                ])) as request:
            status, _ = self.run_cli("--label")
        self.assertEqual(status, 0)
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(json.loads(payload["input"][0]["content"][0]["text"]), ["cat", "katze"])
        self.assertEqual(migration.load_labels(self.cache), {"bitcoin": "both", "cat": "en", "katze": "de"})

    def test_malformed_response_discards_batch_without_aborting_pass(self):
        for raw in (b"not json", response([{"tag": "cat", "language": "invalid"}])):
            with self.subTest(raw=raw), patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                    classifier, "request_bytes", return_value=raw), contextlib.redirect_stderr(io.StringIO()):
                status, output = self.run_cli("--label")
            self.assertEqual(status, 1)
            self.assertFalse(self.cache.exists())
            self.assertIn("attempted 3 tags; cached 0 labels", output)
            self.assertIn("run total (known costs)", output)

    def test_limit_and_retry_unknown_and_override_precedence(self):
        self.cache_labels({"bitcoin": "unknown"})
        self.overrides.write_text(json.dumps({"bitcoin": "both"}))
        rows = [{"tag": "cat", "language": "en"}]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                classifier, "request_bytes", return_value=response(rows)) as request:
            self.run_cli("--label", "--limit", "1", "--retry-unknown", "--overrides", str(self.overrides))
        request.assert_called_once()
        self.assertEqual(migration.load_labels(self.cache, self.overrides), {"bitcoin": "both", "cat": "en"})
        rows = [{"tag": "bitcoin", "language": "both"}]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                classifier, "request_bytes", return_value=response(rows)):
            self.run_cli("--label", "--limit", "1", "--retry-unknown")
        self.assertEqual(migration.load_labels(self.cache)["bitcoin"], "both")

    def test_failed_batch_does_not_prevent_later_success_or_report(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}), patch.object(
                classifier, "request_bytes", side_effect=[
                    response([{"tag": "bitcoin", "language": "both"}]),
                    classifier.ClassificationError("failed batch"),
                    response([{"tag": "katze", "language": "de"}]),
                ]) as request, contextlib.redirect_stderr(io.StringIO()) as errors:
            status, output = self.run_cli("--label", "--batch-size", "1", "--concurrency", "1",
                                         "--report", str(self.report))
        self.assertEqual(status, 1)
        self.assertEqual(request.call_count, 3)
        self.assertIn("continuing with other batches", errors.getvalue())
        self.assertEqual(migration.load_labels(self.cache), {"bitcoin": "both", "katze": "de"})
        report = json.loads(self.report.read_text())
        self.assertEqual(report["missing_labels"], ["cat"])
        self.assertEqual(report["labeling_pass"], {"attempted_tags": 3, "cached_labels": 2, "failed_batches": 1})
        self.assertIn("run total (known costs) ~$0.000140", output)
        self.assertIn("Labeling pass complete", output)

    def test_malformed_cache_and_path_alias_fail_before_network_or_writes(self):
        for text in ('{"partial":', json.dumps({"labels": {"cat": []}}),
                     json.dumps({"labels": {"Cat": "de", "cat": "en"}})):
            with self.subTest(text=text):
                self.cache.write_text(text)
                with self.assertRaises(ValueError):
                    self.run_cli("--apply")
        self.cache.unlink()
        with self.assertRaisesRegex(ValueError, "distinct paths"):
            self.run_cli("--report", str(self.source))
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaises(classifier.APIConfigurationError):
                self.run_cli("--label")
        self.assertFalse(self.cache.exists())

    def test_exports_migrated_lists_without_changing_search_tags(self):
        self.write_records(legacy_record("https://archiv0.derrosarotepanzer.com/images/2024/01/example.jpg"))
        self.cache_labels({"katze": "de", "cat": "en", "bitcoin": "both"})
        output = self.root / "classification_index.json"
        export_index(self.source, output)
        before = json.loads(output.read_text())["2024/01/example.jpg"]
        self.run_cli("--apply")
        export_index(self.source, output)
        after = json.loads(output.read_text())["2024/01/example.jpg"]
        self.assertEqual(set(classifier.all_tags(after)), set(before["tags"]))
        self.assertEqual(after["tags"], ["Bitcoin"])
        self.assertEqual(after["tags_de"], ["Katze"])
        self.assertEqual(after["tags_en"], ["cat"])
        self.assertEqual(after["tag_format_version"], 2)


if __name__ == "__main__":
    unittest.main()
