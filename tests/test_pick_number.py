import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from openai.types.chat import ChatCompletion

import pick_number as app


def completion(answer="7", finish_reason="stop", cost=0.001):
    return ChatCompletion.model_validate({
        "id": "gen-test", "object": "chat.completion", "created": 1,
        "model": "test/model", "provider": "test-provider",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50,
                  "completion_tokens_details": {"reasoning_tokens": 25}, "cost": cost},
    })


def arguments(directory, **overrides):
    values = dict(output=Path(directory), models=["test/a", "test/b"], samples=2,
                  prompt=app.PROMPT, reasoning="low", max_tokens=1024, timeout=120, max_tool_rounds=2)
    values.update(overrides)
    return argparse.Namespace(**values)


class AnswerTests(unittest.TestCase):
    def test_natural_answers(self):
        for answer, number in [("7", 7), ("I'll pick **7**.", 7), ("10!", 10), ("1", 1)]:
            with self.subTest(answer=answer):
                self.assertEqual(app.classify(answer, "stop"), ("valid", number))

    def test_invalid_answers(self):
        for answer, status in [
            ("", "empty"), ("seven", "unparsed"), ("7 or 8", "ambiguous"),
            ("Between 1 and 10, I choose 7", "ambiguous"), ("7, 7", "ambiguous"),
            ("0", "out_of_range"), ("11", "out_of_range"), ("-7", "unparsed"),
            ("7.5", "unparsed"), ("7e0", "unparsed"), ("choice7", "unparsed"),
        ]:
            with self.subTest(answer=answer):
                self.assertEqual(app.classify(answer, "stop"), (status, None))

    def test_truncation_and_refusal_are_not_valid_numbers(self):
        self.assertEqual(app.classify("7", "length"), ("truncated", None))
        self.assertEqual(app.classify("7", "content_filter"), ("incomplete", None))

    def test_sdk_keeps_openrouter_usage_extensions(self):
        fields = app.response_fields(completion().model_dump(mode="json"))
        self.assertEqual(fields["cost_usd"], 0.001)
        self.assertEqual(fields["reasoning_tokens"], 25)
        self.assertEqual(fields["provider"], "test-provider")

    def test_unknown_cost_is_not_zero(self):
        row = {"model": "test/a", "status": "valid", "number": 7}
        config = {"models": ["test/a"], "samples": 1, "run_id": "test", "state": "completed"}
        self.assertIsNone(app.summarize([row], config)["reported_cost_usd"])


class RunTests(unittest.TestCase):
    def run_with(self, directory, effects, **overrides):
        client = Mock()
        client.chat.completions.create.side_effect = effects
        with redirect_stdout(io.StringIO()):
            code = app.run_experiment(arguments(directory, **overrides), client, "secret-test-key")
        run_dir = next(Path(directory).iterdir())
        rows = [json.loads(line) for line in (run_dir / "responses.jsonl").read_text().splitlines()]
        return code, client, run_dir, rows

    def test_round_robin_independent_requests_and_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            code, client, run_dir, rows = self.run_with(directory, [completion()] * 4)
            self.assertEqual(code, 0)
            calls = client.chat.completions.create.call_args_list
            self.assertEqual([call.kwargs["model"] for call in calls], ["test/a", "test/b", "test/a", "test/b"])
            for call in calls:
                self.assertEqual(call.kwargs["messages"], [{"role": "user", "content": app.PROMPT}])
                self.assertNotIn("temperature", call.kwargs)
                self.assertEqual(call.kwargs["extra_body"]["reasoning"], {"effort": "low"})
                self.assertTrue(call.kwargs["extra_body"]["provider"]["require_parameters"])
            self.assertEqual(len({row["trace_id"] for row in rows}), 4)
            self.assertIn("response", rows[0])
            self.assertEqual((run_dir / "distribution.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertTrue((run_dir / "responses.csv").exists())
            summary = json.loads((run_dir / "summary.json").read_text())
            self.assertAlmostEqual(summary["reported_cost_usd"], 0.004)
            self.assertEqual(summary["models"][0]["percentages"]["7"], 100)

    def test_interruption_keeps_completed_and_in_flight_records(self):
        with tempfile.TemporaryDirectory() as directory:
            code, client, run_dir, rows = self.run_with(directory, [completion(), KeyboardInterrupt()])
            self.assertEqual(code, 130)
            self.assertEqual(client.chat.completions.create.call_count, 2)
            self.assertEqual([row["status"] for row in rows], ["valid", "interrupted"])
            self.assertEqual(json.loads((run_dir / "config.json").read_text())["state"], "interrupted")
            self.assertTrue((run_dir / "distribution.png").exists())

    def test_error_is_logged_without_retry_or_credential_leak(self):
        from openai import APIConnectionError
        import httpx2 as httpx
        failure = APIConnectionError(message="Provider echoed secret-test-key", request=httpx.Request("POST", app.BASE_URL))
        with tempfile.TemporaryDirectory() as directory:
            code, client, run_dir, rows = self.run_with(directory, [failure, completion()], samples=1)
            self.assertEqual(code, 1)
            self.assertEqual(client.chat.completions.create.call_count, 2)
            self.assertEqual([row["status"] for row in rows], ["error", "valid"])
            for path in run_dir.iterdir():
                if path.suffix in {".json", ".jsonl", ".csv"}:
                    self.assertNotIn("secret-test-key", path.read_text())

    def test_invalid_key_stops_remaining_calls(self):
        from openai import AuthenticationError
        import httpx2 as httpx
        response = httpx.Response(401, request=httpx.Request("POST", app.BASE_URL))
        failure = AuthenticationError("Invalid key", response=response, body={"error": "invalid"})
        with tempfile.TemporaryDirectory() as directory:
            code, client, run_dir, rows = self.run_with(directory, [failure])
            self.assertEqual(code, 1)
            self.assertEqual(client.chat.completions.create.call_count, 1)
            self.assertEqual(rows[0]["http_status"], 401)
            self.assertEqual(json.loads((run_dir / "config.json").read_text())["state"], "failed")

    def test_real_sdk_http_errors_are_not_retried(self):
        import httpx2
        from openai import OpenAI
        requests = []

        def rate_limit(request):
            requests.append(request)
            return httpx2.Response(429, json={"error": {"message": "Slow down", "code": 429}})

        with tempfile.TemporaryDirectory() as directory:
            with OpenAI(api_key="fake-test-key", base_url=app.BASE_URL, max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(rate_limit))) as client:
                with redirect_stdout(io.StringIO()):
                    code = app.run_experiment(arguments(directory, samples=1), client, "fake-test-key")
            self.assertEqual(code, 1)
            self.assertEqual(len(requests), 2)
            for request in requests:
                payload = json.loads(request.content)
                self.assertEqual(payload["messages"], [{"role": "user", "content": app.PROMPT}])
                self.assertEqual(payload["reasoning"], {"effort": "low"})

    def test_empty_and_truncated_results_have_no_valid_percentages(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _, run_dir, _ = self.run_with(directory, [completion(""), completion("7", "length")], samples=1)
            self.assertEqual(code, 1)
            summary = json.loads((run_dir / "summary.json").read_text())
            self.assertTrue(all(m["valid"] == 0 for m in summary["models"]))
            self.assertTrue(all(sum(m["percentages"].values()) == 0 for m in summary["models"]))

    def test_plot_is_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, run_dir, _ = self.run_with(directory, [completion(), completion()], samples=1)
            with patch.object(app, "OpenAI", side_effect=AssertionError("API must not be used")):
                with patch("sys.argv", ["pick_number.py", "plot", str(run_dir)]), redirect_stdout(io.StringIO()):
                    self.assertEqual(app.main(), 0)


if __name__ == "__main__":
    unittest.main()
