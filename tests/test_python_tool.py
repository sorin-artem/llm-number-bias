from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from openai.types.chat import ChatCompletion

import pick_number as app
from python_tool import DockerPython, PYTHON_TOOL
from test_pick_number import arguments, completion


def tool_request(code="print(4)", arguments_text=None):
    return ChatCompletion.model_validate({
        "id": "gen-tool", "object": "chat.completion", "created": 1, "model": "test/model",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "reasoning_details": [{"type": "reasoning.text", "text": "Use the tool."}],
            "tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": "run_python", "arguments": arguments_text if arguments_text is not None else json.dumps({"code": code})}}],
        }}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60, "cost": 0.002},
    })


class ToolLoopTests(unittest.TestCase):
    def run_trial(self, effects, max_rounds=2):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = Mock()
        self.client.chat.completions.create.side_effect = effects
        self.runner = Mock(image="test-image", image_id="sha256:test")
        self.runner.execute.return_value = {"stdout": "4\n", "stderr": "", "exit_code": 0}
        with redirect_stdout(io.StringIO()):
            code = app.run_experiment(arguments(self.temp.name, models=["test/a"], samples=1,
                                                max_tool_rounds=max_rounds), self.client, "test-key", self.runner)
        self.directory = next(Path(self.temp.name).iterdir())
        self.row = json.loads((self.directory / "responses.jsonl").read_text().strip())
        return code

    def test_auto_tool_roundtrip_preserves_reasoning_and_total_usage(self):
        self.assertEqual(self.run_trial([tool_request(), completion("4")]), 0)
        calls = self.client.chat.completions.create.call_args_list
        self.assertEqual(calls[0].kwargs["tool_choice"], "auto")
        self.assertEqual(calls[0].kwargs["tools"], [PYTHON_TOOL])
        self.assertEqual(calls[0].kwargs["messages"][0], {"role": "user", "content": app.PROMPT})
        messages = calls[1].kwargs["messages"]
        self.assertEqual(messages[1]["reasoning_details"][0]["text"], "Use the tool.")
        self.assertEqual(messages[2]["role"], "tool")
        self.assertEqual(messages[2]["tool_call_id"], "call-1")
        self.assertEqual(json.loads(messages[2]["content"])["stdout"], "4\n")
        self.assertEqual(self.row["tool_calls_requested"], 1)
        self.assertEqual(self.row["tool_calls_successful"], 1)
        self.assertEqual(self.row["api_request_count"], 2)
        self.assertAlmostEqual(self.row["cost_usd"], 0.003)
        self.assertEqual(self.row["prompt_tokens"], 60)
        events = [json.loads(s) for s in (self.directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual([e["event"] for e in events], ["api_response", "tool_result", "api_response"])

    def test_auto_does_not_force_python(self):
        self.assertEqual(self.run_trial([completion("7")]), 0)
        self.runner.execute.assert_not_called()
        self.assertEqual(self.row["tool_calls_requested"], 0)
        self.assertEqual(self.row["api_request_count"], 1)

    def test_tool_round_limit_does_not_make_unbounded_requests(self):
        self.assertEqual(self.run_trial([tool_request(), tool_request()], max_rounds=1), 1)
        self.assertEqual(self.client.chat.completions.create.call_count, 2)
        self.assertEqual(self.runner.execute.call_count, 1)
        self.assertEqual(self.row["status"], "tool_limit")
        self.assertEqual(self.row["tool_calls_requested"], 2)

    def test_second_request_error_preserves_first_request_cost(self):
        from openai import APIConnectionError
        import httpx2
        failure = APIConnectionError(request=httpx2.Request("POST", app.BASE_URL))
        self.assertEqual(self.run_trial([tool_request(), failure]), 1)
        self.assertEqual(self.row["status"], "error")
        self.assertIsNone(self.row["cost_usd"])
        self.assertAlmostEqual(self.row["reported_cost_usd"], 0.002)
        summary = json.loads((self.directory / "summary.json").read_text())
        self.assertAlmostEqual(summary["reported_cost_usd"], 0.002)
        self.assertEqual(summary["cost_reported_for"], 0)

    def test_tool_call_before_interruption_is_saved(self):
        self.assertEqual(self.run_trial([tool_request(), KeyboardInterrupt()]), 130)
        self.assertEqual(self.row["status"], "interrupted")
        self.assertEqual(self.row["tool_calls_successful"], 1)
        self.assertIsNone(self.row["cost_usd"])
        self.assertAlmostEqual(self.row["reported_cost_usd"], 0.002)

    def test_schema_does_not_hint_at_random_numbers(self):
        description = PYTHON_TOOL["function"]["description"].lower()
        self.assertNotIn("random", description)
        self.assertNotIn("number", description)


class ToolArgumentTests(unittest.TestCase):
    def test_invalid_arguments_never_start_a_process(self):
        runner = DockerPython.__new__(DockerPython)
        for name, arguments_text in [("other", "{}"), ("run_python", "invalid-json"),
                                     ("run_python", "[]"), ("run_python", '{"code": ""}'),
                                     ("run_python", '{"code": "print(4)", "extra": 1}')]:
            with self.subTest(arguments=arguments_text), patch("python_tool.subprocess.Popen") as process:
                self.assertNotEqual(runner.execute(name, arguments_text)["exit_code"], 0)
                process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
