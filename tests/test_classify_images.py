"""Offline CLI tests: python3 -m unittest discover -s tests -v"""

import argparse
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
from urllib.error import HTTPError
from urllib.request import Request

from scripts import classify_images as cli

RESULT = {
    "text": "Grüße\nHello!",
    "languages": ["German", "English"],
    "tags": ["Drake", "vergleich", "reaktion"],
    "image_type": "meme",
    "description": "Zwei Bildfelder zeigen gegensätzliche Reaktionen.",
    "meme_template": {
        "status": "recognized", "name": "Drake Hotline Bling",
        "confidence": 0.95, "evidence": "Drake rejects the top panel and approves the bottom one.",
    },
}
URL = "https://example.com/meme.jpg"


def record(url=URL, model=cli.DEFAULT_MODEL, reasoning_effort=cli.DEFAULT_REASONING_EFFORT):
    return {
        "url": url, "model": model, "reasoning_effort": reasoning_effort, "schema_version": cli.SCHEMA_VERSION,
        "classification": copy.deepcopy(RESULT),
    }


class ClassificationTests(unittest.TestCase):
    def test_verified_model_and_reasoning_effort_defaults(self):
        args = cli.build_parser().parse_args([URL])
        self.assertEqual(args.model, "gpt-6-luna")
        self.assertEqual(cli.DEFAULT_REASONING_EFFORT, "low")
        self.assertIsNone(args.reasoning_effort)
        self.assertFalse(args.no_reasoning)
        self.assertFalse(hasattr(cli, "reasoning_effort"))

    def test_pricing_validation(self):
        self.assertEqual(cli.pricing_arg("0.1,0.01,0.125,0.5"), (0.1, 0.01, 0.125, 0.5))
        for value in ("1,2,3", "1,2,3,4,5", "1,-2,3,4", "1,nan,3,4", "inf,2,3,4", "a,b,c,d"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                cli.pricing_arg(value)

    def test_cost_accounts_for_cache_writes_and_reasoning(self):
        usage = {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 100},
            "output_tokens": 500, "output_tokens_details": {"reasoning_tokens": 300},
            "total_tokens": 1500,
        }
        cost = cli.estimate_cost(usage, "gpt-6-luna")
        self.assertAlmostEqual(cost["total_usd"], 0.0003345)
        self.assertEqual(cost["rates"], (0.1, 0.01, 0.125, 0.5))
        self.assertIsNone(cli.estimate_cost(usage, "custom-model"))
        self.assertIsNone(cli.estimate_cost(usage, "gpt-6-luna", "flex"))
        custom = cli.estimate_cost(usage, "custom-model", "flex", (1, 0.1, 1.25, 5))
        self.assertAlmostEqual(custom["total_usd"], 0.003345)
        self.assertEqual(custom["source"], "custom effective rates")

    def test_cost_long_context_threshold(self):
        usage = {"input_tokens": 272000, "output_tokens": 100}
        self.assertAlmostEqual(cli.estimate_cost(usage, "gpt-6-luna")["total_usd"], 0.02725)
        usage["input_tokens"] += 1
        cost = cli.estimate_cost(usage, "gpt-6-luna")
        self.assertAlmostEqual(cost["total_usd"], 0.0544752)
        self.assertEqual(cost["rates"], (0.2, 0.02, 0.25, 0.75))
        self.assertEqual(cost["source"], "standard long-context rates")

    def test_cost_with_missing_or_invalid_usage_is_unavailable(self):
        for usage in (None, {}, {"input_tokens": -1, "output_tokens": 5},
                      {"input_tokens": True, "output_tokens": 5},
                      {"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 11}},
                      {"input_tokens": 10, "output_tokens": 5, "input_tokens_details": []}):
            with self.subTest(usage=usage):
                self.assertIsNone(cli.estimate_cost(usage, "gpt-6-luna"))

    def test_archive_hosts_match_gallery(self):
        for year in range(2021, 2034):
            self.assertEqual(cli.archive_host(year), f"https://archiv{max(0, year - 2024)}.derrosarotepanzer.com")
        with self.assertRaises(ValueError):
            cli.archive_host(2020)

    def test_month_and_url_validation(self):
        self.assertEqual(cli.month_arg("2025/09"), "2025/09")
        for value in ("2025/13", "2025/9", "../09"):
            with self.assertRaises(argparse.ArgumentTypeError):
                cli.month_arg(value)
        for value in ("file:///etc/passwd", "https://", "https://user:secret@example.com/image.jpg"):
            with self.assertRaises(ValueError):
                cli.validate_url(value)

    def test_image_formats(self):
        for data, mime in (
            (b"\xff\xd8\xffstuff", "jpeg"),
            (b"\x89PNG\r\n\x1a\nstuff", "png"),
            (b"GIF89astuff", "gif"),
            (b"RIFF1234WEBPstuff", "webp"),
        ):
            self.assertTrue(cli.image_data_url(data).startswith(f"data:image/{mime};base64,"))
        with self.assertRaises(cli.ClassificationError):
            cli.image_data_url(b"<html>not an image</html>")

    def test_watermark_removal(self):
        cases = [
            ("@RosarotePanzer\nCaption", "Caption"),
            ("@Rosarote\nPanzer\nCaption", "Caption"),
            ("@rosarote PANZER\r\nCaption", "Caption"),
            ("Caption\n@RosarotePanzer", "Caption"),
            ("Caption @RosarotePanzer", "Caption"),
            ("First\n@Rosarote\nPanzer\nSecond", "First\nSecond"),
            ("First\n\nSecond\n@RosarotePanzer", "First\n\nSecond"),
            ("@RosarotePanzer", ""),
            ("@Rosarote\nPanzer", ""),
            ("Caption @OtherHandle", "Caption @OtherHandle"),
            ("@RosarotePanzerBackup", "@RosarotePanzerBackup"),
            ("  Caption\n\n", "  Caption\n\n"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(cli.remove_watermark(source), expected)

    def test_classification_strips_watermark_even_if_model_returns_it(self):
        for text, expected in [("@Rosarote\nPanzer\nGrüße", "Grüße"), ("@RosarotePanzer", ""),
                               ("Grüße\nHello!", "Grüße Hello!"), ("Grüße\r\nHello!", "Grüße Hello!")]:
            response_result = copy.deepcopy(RESULT)
            response_result["text"] = text
            response = {"status": "completed", "output": [{"content": [{"type": "output_text", "text": json.dumps(response_result)}]}]}
            with self.subTest(text=text), patch.object(cli, "request_bytes", side_effect=[b"\xff\xd8\xffimage", json.dumps(response).encode()]):
                result = cli.classify(URL, "custom-model", "secret", timeout=10, retries=0)
                self.assertEqual(result["classification"]["text"], expected)
                self.assertEqual(result["classification"]["languages"], RESULT["languages"] if expected else [])

    def test_schema_validation(self):
        self.assertEqual(cli.validate_classification(RESULT), RESULT)
        no_template = copy.deepcopy(RESULT)
        no_template["meme_template"].update(status="none", name=None)
        no_template["tags"] = []
        cli.validate_classification(no_template)
        mutations = [
            lambda result: result.pop("text"),
            lambda result: result.update(extra="field"),
            lambda result: result.update(languages="German"),
            lambda result: result.pop("tags"),
            lambda result: result.update(tags="meme"),
            lambda result: result.update(tags=[123]),
            lambda result: result.update(image_type="invalid"),
            lambda result: result["meme_template"].update(confidence=1.1),
            lambda result: result["meme_template"].update(confidence=True),
            lambda result: result["meme_template"].update(confidence=float("nan")),
            lambda result: result["meme_template"].update(name=None),
            lambda result: result["meme_template"].update(status="unknown"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                result = copy.deepcopy(RESULT)
                mutate(result)
                with self.assertRaises(cli.ClassificationError):
                    cli.validate_classification(result)

    @patch.object(cli, "request_bytes")
    def test_openai_request_and_result(self, fetch):
        response = {
            "id": "resp_123", "status": "completed", "usage": {"total_tokens": 200},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(RESULT)}]}],
        }
        fetch.side_effect = [b"\xff\xd8\xffimage", json.dumps(response).encode()]
        result = cli.classify(URL, "custom-model", "test-secret", timeout=10, retries=0)
        self.assertEqual(result["classification"], {**RESULT, "text": "Grüße Hello!"})
        self.assertEqual(result["model"], "custom-model")
        self.assertEqual(result["usage"], {"total_tokens": 200})
        image_request = fetch.call_args_list[0].args[0]
        self.assertIsNone(image_request.get_header("Authorization"))
        api_request = fetch.call_args_list[1].args[0]
        self.assertEqual(api_request.get_header("Authorization"), "Bearer test-secret")
        payload = json.loads(api_request.data)
        self.assertEqual(payload["model"], "custom-model")
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertNotIn("thinking", payload)
        self.assertEqual(result["reasoning_effort"], "low")
        self.assertNotIn("thinking", result)
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(payload["text"]["format"]["schema"], cli.CLASSIFICATION_SCHEMA)
        self.assertEqual(payload["input"][0]["content"][0]["detail"], "high")
        self.assertIn("Populate tags", payload["instructions"])
        self.assertIn("German search keywords", payload["instructions"])
        self.assertIn("tags", payload["text"]["format"]["schema"]["required"])
        self.assertEqual(result["classification"]["tags"], RESULT["tags"])
        self.assertIn("Write description in German", payload["instructions"])
        self.assertIn("alt attribute", payload["instructions"])
        self.assertIn("German alt text", payload["text"]["format"]["schema"]["properties"]["description"]["description"])

    @patch.object(cli, "request_bytes")
    def test_classification_normalizes_tags_and_excludes_watermark(self, fetch):
        classification = copy.deepcopy(RESULT)
        classification["tags"] = ["  Bert  ", "bert", "", "  ", "@Rosarote\nPanzer", "konzertsaal"]
        response = {"status": "completed", "output": [{"content": [{"type": "output_text", "text": json.dumps(classification)}]}]}
        fetch.side_effect = [b"\xff\xd8\xffimage", json.dumps(response).encode()]
        result = cli.classify(URL, cli.DEFAULT_MODEL, "secret", timeout=10, retries=0)
        self.assertEqual(result["classification"]["tags"], ["Bert", "konzertsaal"])

    def test_reasoning_levels_are_sent_as_effort_or_omitted(self):
        response = {
            "status": "completed",
            "output": [{"content": [{"type": "output_text", "text": json.dumps(RESULT)}]}],
        }
        for effort in (*cli.REASONING_EFFORTS, None):
            with self.subTest(effort=effort), patch.object(cli, "request_bytes", side_effect=[b"\xff\xd8\xffimage", json.dumps(response).encode()]) as fetch:
                result = cli.classify(URL, "custom-model", "secret", reasoning_effort=effort, timeout=10, retries=0)
                payload = json.loads(fetch.call_args.args[0].data)
                self.assertEqual(result["reasoning_effort"], effort)
                if effort is None:
                    self.assertNotIn("reasoning", payload)
                else:
                    self.assertEqual(payload["reasoning"], {"effort": effort})
                self.assertNotIn("thinking", payload)
                self.assertNotIn("thinking", result)
        for effort in ("invalid", "light", "default"):
            with self.subTest(effort=effort), patch.object(cli, "request_bytes") as fetch:
                with self.assertRaises(ValueError):
                    cli.classify(URL, "custom-model", "secret", reasoning_effort=effort, timeout=10, retries=0)
                fetch.assert_not_called()

    def test_refusal_incomplete_and_invalid_response(self):
        responses = [
            {"status": "completed", "output": [{"content": [{"type": "refusal", "refusal": "Cannot analyze"}]}]},
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
            {"status": "completed", "output": []},
            {"error": {"message": "unknown model"}},
        ]
        for response in responses:
            with self.subTest(response=response), patch.object(cli, "request_bytes", side_effect=[b"\xff\xd8\xffimage", json.dumps(response).encode()]):
                with self.assertRaises(cli.ClassificationError):
                    cli.classify(URL, cli.DEFAULT_MODEL, "secret", timeout=10, retries=0)

    @patch.object(cli.time, "sleep")
    @patch.object(cli, "urlopen")
    def test_retry_and_size_limit(self, urlopen, sleep):
        error = HTTPError(URL, 429, "Rate limited", {"Retry-After": "1"}, io.BytesIO(b"retry"))
        urlopen.side_effect = [error, io.BytesIO(b"ok")]
        self.assertEqual(cli.request_bytes(Request(URL), timeout=10, retries=1, max_bytes=100), b"ok")
        sleep.assert_called_once_with(1)
        urlopen.side_effect = None
        urlopen.return_value = io.BytesIO(b"too large")
        with self.assertRaises(cli.ClassificationError):
            cli.request_bytes(Request(URL), timeout=10, retries=0, max_bytes=2)

    @patch.object(cli, "urlopen")
    def test_authentication_errors_are_fatal_without_retries(self, urlopen):
        urlopen.side_effect = HTTPError(cli.API_URL, 401, "Unauthorized", {}, io.BytesIO(b"invalid key"))
        with self.assertRaises(cli.APIConfigurationError):
            cli.request_bytes(Request(cli.API_URL), timeout=10, retries=3, max_bytes=100)
        self.assertEqual(urlopen.call_count, 1)


class ConcurrencyTests(unittest.TestCase):
    def test_submission_is_bounded_and_workers_overlap(self):
        release = threading.Event()
        ready = threading.Event()
        lock = threading.Lock()
        submitted = []
        active = 0
        maximum = 0

        def urls():
            for number in range(20):
                submitted.append(str(number))
                yield str(number)

        def work(url):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    ready.set()
            try:
                if not release.wait(3):
                    raise AssertionError("workers were not released")
                return record(url)
            finally:
                with lock:
                    active -= 1

        results = cli.classify_many(urls(), work, 2)
        first = []
        consumer = threading.Thread(target=lambda: first.append(next(results)))
        consumer.start()
        try:
            self.assertTrue(ready.wait(2), "two workers should run concurrently")
            self.assertEqual(submitted, ["0", "1"], "do not eagerly queue the archive")
        finally:
            release.set()
            consumer.join(3)
            results.close()
        self.assertFalse(consumer.is_alive())
        self.assertEqual(maximum, 2)
        self.assertEqual(len(first), 1)
        self.assertIsNone(first[0][2])

    def test_fatal_api_error_stops_submission_and_drains_paid_success(self):
        barrier = threading.Barrier(2)
        release = threading.Event()
        fatal_observed = threading.Event()
        called = []
        original_wait = cli.wait

        def work(url):
            called.append(url)
            barrier.wait(timeout=2)
            if url == "bad":
                raise cli.APIConfigurationError("invalid model")
            if not release.wait(3):
                raise AssertionError("success was not drained")
            return record(url)

        def observe_wait(futures, **kwargs):
            if fatal_observed.is_set():
                release.set()
            done, pending = original_wait(futures, **kwargs)
            if any(isinstance(future.exception(), cli.APIConfigurationError) for future in done):
                fatal_observed.set()
            return done, pending

        results = cli.classify_many(["bad", "good", "never-started"], work, 2)
        try:
            with patch.object(cli, "wait", side_effect=observe_wait):
                url, result, error = next(results)
                self.assertEqual(url, "good")
                self.assertEqual(result["url"], "good")
                self.assertIsNone(error)
                with self.assertRaises(cli.APIConfigurationError):
                    next(results)
        finally:
            release.set()
            results.close()
        self.assertCountEqual(called, ["bad", "good"])

    def test_interrupt_stops_submission_and_drains_running_work(self):
        release = threading.Event()
        started = threading.Event()
        called = []
        original_wait = cli.wait
        interrupted = False
        def work(url):
            called.append(url)
            started.set()
            if not release.wait(3):
                raise AssertionError("worker was not released")
            return record(url)
        def interrupt_wait(futures, **kwargs):
            nonlocal interrupted
            if not interrupted:
                self.assertTrue(started.wait(2))
                interrupted = True
                raise KeyboardInterrupt()
            release.set()
            return original_wait(futures, **kwargs)
        results = cli.classify_many([URL, "never-started"], work, 1)
        try:
            with patch.object(cli, "wait", side_effect=interrupt_wait):
                self.assertEqual(next(results)[1]["url"], URL)
                with self.assertRaises(KeyboardInterrupt):
                    next(results)
        finally:
            release.set()
            results.close()
        self.assertEqual(called, [URL])

    def test_discovery_error_drains_success_before_raising(self):
        def urls():
            yield URL
            raise cli.ClassificationError("bad monthly index")
        results = cli.classify_many(urls(), record, 2)
        self.assertEqual(next(results)[1]["url"], URL)
        with self.assertRaisesRegex(cli.ClassificationError, "bad monthly index"):
            next(results)


class ArchiveCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.index = self.root / "dir_index.json"
        self.index.write_text(json.dumps({"2024/12": 1, "2025/01": 3}))
        self.output = self.root / "results.jsonl"
        self.options = ["--archive", "--dir-index", str(self.index), "--output", str(self.output), "--concurrency", "1"]
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_cli(self, options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli.main(options)
        return status, stdout.getvalue(), stderr.getvalue()

    @patch.object(cli, "fetch_json")
    def test_remote_indexes_hosts_and_deduplication(self, fetch):
        fetch.side_effect = [
            [{"name": "old.jpg"}],
            [{"name": "new image.jpg"}, {"name": "new image.jpg"}, {"name": "thumbnails.jpg"}],
        ]
        urls = list(cli.archive_urls(self.index, [], timeout=10, retries=0))
        self.assertEqual(urls, [
            "https://archiv0.derrosarotepanzer.com/images/2024/12/old.jpg",
            "https://archiv1.derrosarotepanzer.com/images/2025/01/new%20image.jpg",
        ])
        self.assertEqual(fetch.call_args_list[1].args[0], "https://archiv1.derrosarotepanzer.com/images/2025/01/entry_index.json")

    @patch.object(cli, "fetch_json", return_value=[{"name": "../bad.jpg"}])
    def test_reject_invalid_index_names(self, fetch):
        with self.assertRaises(cli.ClassificationError):
            list(cli.archive_urls(self.index, ["2025/01"], timeout=10, retries=0))
        with self.assertRaises(cli.ClassificationError):
            list(cli.archive_urls(self.index, ["2025/02"], timeout=10, retries=0))

    @patch.object(cli, "fetch_json", return_value=[{"name": "new.jpg"}])
    @patch.object(cli, "classify")
    def test_dry_run_requires_no_key_and_does_not_write(self, classify, fetch):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            status, stdout, _ = self.run_cli(self.options + ["--dry-run", "--month", "2025/01", "--limit", "1"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.strip(), "https://archiv1.derrosarotepanzer.com/images/2025/01/new.jpg")
        self.assertFalse(self.output.exists())
        classify.assert_not_called()
        self.assertEqual(fetch.call_count, 1)

    @patch.object(cli, "archive_urls", return_value=iter([URL, URL + "?2", URL + "?3"]))
    @patch.object(cli, "classify")
    def test_resume_limit_and_newline_boundary(self, classify, urls):
        # An otherwise valid file without a final newline must still append safely.
        self.output.write_text(json.dumps(record(URL)), encoding="utf-8")
        classify.side_effect = lambda url, model, key, **kwargs: record(url, model, kwargs["reasoning_effort"])
        status, _, _ = self.run_cli(self.options + ["--limit", "1"])
        self.assertEqual(status, 0)
        classify.assert_called_once()
        self.assertEqual(classify.call_args.args[0], URL + "?2")
        self.assertEqual(classify.call_args.kwargs["reasoning_effort"], "low")
        rows = [json.loads(line) for line in self.output.read_text().splitlines()]
        self.assertEqual([row["url"] for row in rows], [URL, URL + "?2"])
        self.assertEqual(cli.completed_urls(self.output, "different-model"), set())

    def test_resume_reprocesses_results_before_current_content_policy(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                old = record()
                del old["classification"]["tags"]
                old["schema_version"] = version
                self.output.write_text(json.dumps(old) + "\n", encoding="utf-8")
                self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL), set())

    def test_resume_matches_effort_and_handles_legacy_records(self):
        self.output.write_text(json.dumps(record()) + "\n", encoding="utf-8")
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, "low"), {URL})
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, "high"), set())
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, None), set())
        for old, expected in (("light", "low"), ("low", "low"), ("high", "high"), ("default", None)):
            with self.subTest(old=old):
                legacy = record()
                del legacy["reasoning_effort"]
                legacy["thinking"] = old
                self.output.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
                self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, expected), {URL})
                self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, "medium"), set())
        del legacy["thinking"]
        self.output.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, None), {URL})
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, "low"), set())

    def test_canonical_effort_takes_precedence_over_legacy_field(self):
        current = record(reasoning_effort=None)
        current["thinking"] = "high"
        self.output.write_text(json.dumps(current) + "\n", encoding="utf-8")
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, None), {URL})
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL, "high"), set())

    @patch.object(cli, "archive_urls", return_value=iter([URL]))
    @patch.object(cli, "classify", return_value=record(reasoning_effort="high"))
    def test_archive_forwards_custom_reasoning_effort_and_reclassifies(self, classify, urls):
        self.output.write_text(json.dumps(record()) + "\n", encoding="utf-8")
        status, _, _ = self.run_cli(self.options + ["--reasoning-effort", "high"])
        self.assertEqual(status, 0)
        classify.assert_called_once_with(URL, cli.DEFAULT_MODEL, "test-key", reasoning_effort="high", timeout=90, retries=3)
        self.assertEqual(len(self.output.read_text().splitlines()), 2)

    @patch.object(cli, "archive_urls", return_value=iter([URL]))
    @patch.object(cli, "classify", return_value=record())
    def test_force_appends(self, classify, urls):
        self.output.write_text(json.dumps(record()) + "\n", encoding="utf-8")
        status, _, _ = self.run_cli(self.options + ["--force"])
        self.assertEqual(status, 0)
        self.assertEqual(len(self.output.read_text().splitlines()), 2)
        classify.assert_called_once()

    @patch.object(cli, "archive_urls", return_value=iter([URL, URL + "?2"]))
    @patch.object(cli, "classify", side_effect=[cli.ClassificationError("bad image"), record(URL + "?2")])
    def test_image_failure_continues_and_is_not_marked_completed(self, classify, urls):
        status, _, stderr = self.run_cli(self.options)
        self.assertEqual(status, 1)
        self.assertIn("1 classified, 1 failed", stderr)
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL), {URL + "?2"})

    @patch.object(cli, "archive_urls", return_value=iter([URL, URL + "?2"]))
    @patch.object(cli, "classify", side_effect=cli.APIConfigurationError("invalid model"))
    def test_bad_api_configuration_stops_archive(self, classify, urls):
        status, _, stderr = self.run_cli(self.options)
        self.assertEqual(status, 1)
        self.assertIn("invalid model", stderr)
        self.assertEqual(classify.call_count, 1)

    def test_corrupt_resume_file_is_not_modified(self):
        self.output.write_text('{"url":', encoding="utf-8")
        status, _, stderr = self.run_cli(self.options)
        self.assertEqual(status, 1)
        self.assertIn("results.jsonl:1", stderr)
        self.assertEqual(self.output.read_text(), '{"url":')

    @patch.object(cli, "classify", return_value=record())
    def test_single_url_prints_json_and_honors_model(self, classify):
        status, stdout, _ = self.run_cli([URL, "--model", "custom-model", "--reasoning-effort", "high"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout)["classification"]["text"], RESULT["text"])
        self.assertEqual(classify.call_args.args[1], "custom-model")
        self.assertEqual(classify.call_args.args[2], "test-key")
        self.assertEqual(classify.call_args.kwargs["reasoning_effort"], "high")
        self.assertFalse(self.output.exists())

    @patch.object(cli.time, "perf_counter", side_effect=[10, 11, 13, 14, 19, 20])
    @patch.object(cli, "request_bytes")
    def test_single_url_stderr_diagnostics_keep_stdout_json(self, fetch, clock):
        usage = {
            "input_tokens": 1000, "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 100},
            "output_tokens": 500, "output_tokens_details": {"reasoning_tokens": 300}, "total_tokens": 1500,
        }
        response = {
            "id": "resp_debug", "model": "gpt-6-luna", "service_tier": "default", "status": "completed", "usage": usage,
            "output": [{"content": [{"type": "output_text", "text": json.dumps(RESULT)}]}],
        }
        fetch.side_effect = [b"\xff\xd8\xffimage", json.dumps(response).encode()]
        status, stdout, stderr = self.run_cli([URL])
        self.assertEqual(status, 0)
        result = json.loads(stdout)
        self.assertEqual(result["classification"], {**RESULT, "text": "Grüße Hello!"})
        self.assertEqual(result["usage"], usage)
        for value in ("model=gpt-6-luna", "reasoning_effort=low", "image_bytes=8",
                      "download=2.000s", "api=5.000s", "total=10.000s", '"reasoning_tokens": 300',
                      "estimated_usd=$0.00033450", "response_id=resp_debug"):
            self.assertIn(value, stderr)
        self.assertNotIn("test-key", stderr)
        self.assertNotIn("Bearer", stderr)
        self.assertNotIn("thinking", stderr)
        self.assertNotIn("thinking", result)
        self.assertNotIn("[debug]", stdout)

    @patch.object(cli, "classify", side_effect=cli.ClassificationError("API failed"))
    def test_single_url_failure_reports_timing_and_unavailable_cost(self, classify):
        status, stdout, stderr = self.run_cli([URL])
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertIn("timing total=", stderr)
        self.assertIn("usage=unavailable", stderr)
        self.assertIn("cost=unavailable", stderr)
        self.assertIn("ERROR: API failed", stderr)

    @patch.object(cli, "classify", return_value=dict(record(), usage={"input_tokens": 1000, "output_tokens": 500}))
    def test_single_url_custom_rates_and_unknown_model(self, classify):
        status, _, stderr = self.run_cli([URL, "--model", "custom-model", "--pricing", "1,0.1,1.25,5"])
        self.assertEqual(status, 0)
        self.assertIn("estimated_usd=$0.00350000", stderr)
        self.assertIn("custom effective rates", stderr)
        status, _, stderr = self.run_cli([URL, "--model", "custom-model"])
        self.assertEqual(status, 0)
        self.assertIn("cost=unavailable", stderr)

    @patch.object(cli, "classify")
    def test_single_url_dry_run_reports_zero_cost(self, classify):
        status, stdout, stderr = self.run_cli([URL, "--dry-run"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.strip(), URL)
        self.assertIn("API not called", stderr)
        self.assertIn("cost=$0.00", stderr)
        classify.assert_not_called()

    @patch.object(cli, "classify", return_value=record(reasoning_effort=None))
    def test_no_reasoning_omits_api_setting(self, classify):
        status, stdout, stderr = self.run_cli([URL, "--no-reasoning"])
        self.assertEqual(status, 0)
        self.assertIsNone(classify.call_args.kwargs["reasoning_effort"])
        self.assertIsNone(json.loads(stdout)["reasoning_effort"])
        self.assertIn("reasoning_effort=model default (omitted)", stderr)

    @patch.object(cli, "archive_urls", return_value=iter([URL, URL + "?2", URL + "?3"]))
    @patch.object(cli, "classify", side_effect=[record(), cli.APIConfigurationError("invalid model")])
    def test_archive_fatal_error_preserves_completed_checkpoint(self, classify, discover):
        status, _, stderr = self.run_cli(self.options)
        self.assertEqual(status, 1)
        self.assertIn("invalid model", stderr)
        self.assertEqual(classify.call_count, 2)
        rows = [json.loads(line) for line in self.output.read_text().splitlines()]
        self.assertEqual([row["url"] for row in rows], [URL])

    @patch.object(cli, "classify")
    def test_multiple_urls_execute_concurrently_and_emit_jsonl(self, classify):
        urls = [URL + f"?{i}" for i in range(4)]
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active = maximum = 0

        def work(url, model, key, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                barrier.wait(timeout=2)
                return record(url, model, kwargs["reasoning_effort"])
            finally:
                with lock:
                    active -= 1
        classify.side_effect = work
        status, stdout, stderr = self.run_cli(urls + ["--concurrency", "2"])
        self.assertEqual(status, 0)
        rows = [json.loads(line) for line in stdout.splitlines()]
        self.assertCountEqual([row["url"] for row in rows], urls)
        self.assertEqual(maximum, 2)
        self.assertIn("4 classified, 0 failed", stderr)
        for url in urls:
            self.assertIn(f"[{url}]", stderr)
        self.assertNotIn("test-key", stderr)
        self.assertFalse(self.output.exists())

    @patch.object(cli, "classify")
    def test_multiple_url_failures_do_not_corrupt_success_jsonl(self, classify):
        urls = [URL + f"?{i}" for i in range(3)]
        def work(url, model, key, **kwargs):
            if url == urls[1]:
                raise cli.ClassificationError("bad image")
            return record(url, model)
        classify.side_effect = work
        status, stdout, stderr = self.run_cli(urls + ["--concurrency", "2"])
        self.assertEqual(status, 1)
        self.assertCountEqual([json.loads(line)["url"] for line in stdout.splitlines()], [urls[0], urls[2]])
        self.assertIn(f"ERROR [{urls[1]}]: bad image", stderr)
        self.assertIn("2 classified, 1 failed", stderr)

    @patch.object(cli, "classify", return_value=record())
    def test_multiple_urls_limit_dry_run_and_upfront_validation(self, classify):
        urls = [URL, URL + "?2", URL + "?3"]
        status, stdout, _ = self.run_cli(urls + ["--limit", "1"])
        self.assertEqual(status, 0)
        self.assertEqual(len(stdout.splitlines()), 1)
        classify.assert_called_once()
        classify.reset_mock()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            status, stdout, stderr = self.run_cli(urls + ["--dry-run", "--limit", "2"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.splitlines(), urls[:2])
        self.assertIn("API not called", stderr)
        classify.assert_not_called()
        status, stdout, _ = self.run_cli([URL, "file:///not-an-image"])
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        classify.assert_not_called()
        status, stdout, _ = self.run_cli([URL, "--concurrency", "2", URL + "?2", "--dry-run"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout.splitlines(), [URL, URL + "?2"])
        classify.assert_not_called()

    @patch.object(cli, "classify")
    @patch.object(cli, "archive_urls")
    def test_concurrent_archive_jsonl_is_valid_and_resumable(self, discover, classify):
        urls = [URL + f"?{i}" for i in range(4)]
        barrier = threading.Barrier(2)
        discover.side_effect = lambda *args, **kwargs: iter(urls)
        def work(url, model, key, **kwargs):
            barrier.wait(timeout=2)
            return record(url, model, kwargs["reasoning_effort"])
        classify.side_effect = work
        status, stdout, _ = self.run_cli(self.options + ["--concurrency", "2"])
        self.assertEqual(status, 0)
        self.assertEqual(stdout, "")
        rows = [json.loads(line) for line in self.output.read_text().splitlines()]
        self.assertCountEqual([row["url"] for row in rows], urls)
        self.assertEqual(cli.completed_urls(self.output, cli.DEFAULT_MODEL), set(urls))
        classify.reset_mock()
        original = self.output.read_bytes()
        status, _, _ = self.run_cli(self.options + ["--concurrency", "2"])
        self.assertEqual(status, 0)
        classify.assert_not_called()
        self.assertEqual(self.output.read_bytes(), original)

    @patch.object(cli, "archive_urls", return_value=iter([URL, URL + "?2"]))
    @patch.object(cli, "classify", return_value=record())
    def test_archive_limit_does_not_prefetch_additional_api_calls(self, classify, discover):
        status, _, _ = self.run_cli(self.options + ["--concurrency", "4", "--limit", "1"])
        self.assertEqual(status, 0)
        classify.assert_called_once()
        self.assertEqual(len(self.output.read_text().splitlines()), 1)

    def test_cli_requires_key_and_exactly_one_mode(self):
        for options in ([], [URL, "--archive"], [URL, "--reasoning-effort", "invalid"],
                        [URL, "--reasoning-effort", "light"], [URL, "--thinking", "low"],
                        [URL, "--reasoning-effort", "low", "--no-reasoning"],
                        [URL, "--concurrency", "0"], [URL, "--concurrency", "-1"],
                        ["--archive", "--pricing", "1,1,1,1"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(options)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main([URL])


if __name__ == "__main__":
    unittest.main()
