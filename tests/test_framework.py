from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from local_swe_benchmark.config import load_yaml
from local_swe_benchmark.reporting import summarize, write_report, write_run_reports
from local_swe_benchmark.runner import run_trial
from local_swe_benchmark.runner import _parse_test_counts
from local_swe_benchmark.tasks import discover_tasks, load_task
from local_swe_benchmark.tools import ToolBox
from local_swe_benchmark.providers.lmstudio import LMStudioProvider


ROOT = Path(__file__).resolve().parents[1]


class FakeProvider:
    def __init__(self):
        self.called = 0

    def chat(self, messages, tools, temperature, top_p, seed, max_tokens, timeout_seconds):
        self.called += 1
        usage = {"prompt_tokens": 100 * self.called, "completion_tokens": 20}
        if self.called == 1:
            return {"content": "", "tool_calls": [{
                "id": "call-edit", "type": "function",
                "function": {"name": "edit_file", "arguments": json.dumps({
                    "path": "src/order_service/checkout.py",
                    "old_text": "    def total(self, items: list[dict[str, int | float]]) -> float:\n        return calculate_total(items)\n",
                    "new_text": "    def total(self, items: list[dict[str, int | float]], discount_percent: float = 0.0) -> float:\n        if not 0 <= discount_percent <= 100:\n            raise ValueError('discount_percent must be between 0 and 100')\n        subtotal = calculate_total(items)\n        return round(subtotal * (1 - discount_percent / 100), 2)\n",
                })}}], "usage": usage, "ttft_seconds": .1, "generation_seconds": .5}
        return {"content": "Implemented and checked the coupon behavior.", "tool_calls": [],
                "usage": usage, "ttft_seconds": .1, "generation_seconds": .5}


class NoopProvider:
    def chat(self, *args):
        return {"content": "I am done.", "tool_calls": [], "usage": {"prompt_tokens": 100, "completion_tokens": 5},
                "ttft_seconds": .1, "generation_seconds": .2}


class ScriptedProvider:
    def __init__(self, response):
        self.response = response
        self.called = False

    def chat(self, *args):
        if not self.called:
            self.called = True
            return self.response
        return {"content": "Finished.", "tool_calls": [], "usage": {"prompt_tokens": 90, "completion_tokens": 5},
                "ttft_seconds": .1, "generation_seconds": .2}


class FrameworkTests(unittest.TestCase):
    def test_bundled_json_yaml_configuration_loads_without_yaml_package(self):
        data = load_yaml(ROOT / "configs/models.yaml")
        self.assertIn("qwen3.5-9b-q6", data["models"])

    def test_task_catalog_has_repository_commit_and_hidden_tests(self):
        paths = discover_tasks(ROOT / "tasks")
        self.assertTrue(paths)
        task = next(load_task(p) for p in paths if load_task(p).category == "creation")
        self.assertEqual(len(task.base_commit), 40)
        self.assertTrue(task.repository_bundle.is_file())
        self.assertTrue(task.hidden_files)

    def test_tool_paths_are_confined_to_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            box = ToolBox(root, [], None, 2, lambda event: None)
            result = box.execute("write_file", {"path": "../outside", "content": "no"})
            self.assertFalse(result["ok"])
            self.assertFalse((Path(directory) / "outside").exists())

    def test_runner_uses_fresh_worktree_and_evaluates_hidden_tests(self):
        source = load_task(ROOT / "tasks/creation/coupon-checkout/task.yaml")
        executable = sys.executable
        task = replace(source,
                       build_command=[executable, "-m", "compileall", "-q", "src"],
                       public_command=[executable, "-m", "unittest", "discover", "-s", "tests"],
                       hidden_command=[executable, "-m", "unittest", "discover", "-s", ".benchmark_hidden"],
                       allowed_commands=[Path(executable).name])
        original = (ROOT / ".benchmark_cache/order_service-source/src/order_service/checkout.py").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            result = run_trial(task, "test-model", {"model": "fake", "temperature": 0},
                               Path(directory), "test-experiment", FakeProvider())
            self.assertTrue(result["success"], result.get("evaluation"))
            self.assertTrue(result["evaluation"]["hidden_tests"]["passed"])
            self.assertTrue((Path(directory) / result["run_id"] / "patch.diff").is_file())
            self.assertFalse(Path(result["repository"]["worktree"]).exists())
            self.assertTrue(result["repository"]["worktree_removed"])
        self.assertEqual((task.repository / "src/order_service/checkout.py").read_text(encoding="utf-8"), original)
        self.assertEqual((ROOT / ".benchmark_cache/order_service-source/src/order_service/checkout.py").read_text(encoding="utf-8"), original)

    def test_hidden_tests_reject_the_unmodified_base(self):
        source = load_task(ROOT / "tasks/creation/coupon-checkout/task.yaml")
        executable = sys.executable
        task = replace(source, build_command=[executable, "-m", "compileall", "-q", "src"],
                       public_command=[executable, "-m", "unittest", "discover", "-s", "tests"],
                       hidden_command=[executable, "-m", "unittest", "discover", "-s", ".benchmark_hidden"],
                       allowed_commands=[Path(executable).name])
        with tempfile.TemporaryDirectory() as directory:
            result = run_trial(task, "noop", {"model": "fake"}, Path(directory), "base-rejection", NoopProvider())
            self.assertFalse(result["success"])
            self.assertFalse(result["evaluation"]["hidden_tests"]["passed"])
            self.assertEqual(result["failure_class"], "FAIL_TEST_FAILURE")

    def test_analysis_rubric_scores_final_response_without_patch(self):
        source = load_task(ROOT / "tasks/analysis/negative-quantity/task.yaml")
        executable = sys.executable
        task = replace(source, build_command=[executable, "-m", "compileall", "-q", "src"],
                       public_command=[executable, "-m", "unittest", "discover", "-s", "tests"],
                       allowed_commands=[Path(executable).name])
        provider = type("AnswerProvider", (), {"chat": lambda self, *args: {
            "content": "A negative quantity is accepted in src/order_service/pricing.py because calculate_total multiplies quantity without validation, so the total can become negative.",
            "tool_calls": [], "usage": {"prompt_tokens": 10, "completion_tokens": 30},
            "ttft_seconds": .1, "generation_seconds": .3}})()
        with tempfile.TemporaryDirectory() as directory:
            result = run_trial(task, "analysis-model", {"model": "fake"}, Path(directory), "analysis", provider)
            self.assertTrue(result["success"], result.get("evaluation"))
            self.assertTrue(result["evaluation"]["analysis"]["passed"])
            self.assertEqual(result["repository"]["files_changed"], [])

    def test_refactoring_task_requires_behavior_and_hidden_structure_check(self):
        source = load_task(ROOT / "tasks/refactoring/pricing-helper/task.yaml")
        executable = sys.executable
        task = replace(source, build_command=[executable, "-m", "compileall", "-q", "src"],
                       public_command=[executable, "-m", "unittest", "discover", "-s", "tests"],
                       hidden_command=[executable, "-m", "unittest", "discover", "-s", ".benchmark_hidden"],
                       allowed_commands=[Path(executable).name])
        content = """from __future__ import annotations


def calculate_line_total(item: dict[str, int | float]) -> float:
    return float(item[\"unit_price\"]) * int(item[\"quantity\"])


def calculate_total(items: list[dict[str, int | float]]) -> float:
    return round(sum(calculate_line_total(item) for item in items), 2)
"""
        provider = ScriptedProvider({"content": "", "tool_calls": [{
            "id": "call-write", "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps({"path": "src/order_service/pricing.py", "content": content})}},
        ], "usage": {"prompt_tokens": 100, "completion_tokens": 40}, "ttft_seconds": .1, "generation_seconds": .5})
        with tempfile.TemporaryDirectory() as directory:
            result = run_trial(task, "refactor-model", {"model": "fake"}, Path(directory), "refactor", provider)
            self.assertTrue(result["success"], result.get("evaluation"))
            self.assertTrue(result["evaluation"]["hidden_tests"]["passed"])

    def test_reports_keep_raw_runs_and_descriptive_statistics(self):
        rows = [{"status": "success", "success": True, "duration_seconds": 2,
                 "llm": {"prompt_tokens": 12}, "agent": {"iterations": 3}},
                {"status": "failure", "success": False, "duration_seconds": 4,
                 "llm": {"prompt_tokens": 16}, "agent": {"iterations": 5}}]
        summary = summarize(rows)
        self.assertEqual(summary["success_rate"], .5)
        self.assertEqual(summary["metrics"]["duration_seconds"]["median"], 3)
        with tempfile.TemporaryDirectory() as directory:
            path = write_report(rows, Path(directory) / "results.json", "json")
            self.assertEqual(len(json.loads(path.read_text())["runs"]), 2)
            csv_path = write_report(rows, Path(directory) / "results.csv", "csv")
            self.assertIn("run_id", csv_path.read_text())
            html_path = write_report(rows, Path(directory) / "results.html", "html")
            self.assertIn("Failure analysis", html_path.read_text())

    def test_run_reports_are_grouped_by_model_and_task(self):
        record = {
            "run_id": "run-123", "status": "success", "success": True,
            "duration_seconds": 12.5,
            "task": {"id": "creation-coupon-001", "category": "creation"},
            "model": {"profile": "qwen3.5-9b-q6", "loaded_context": 50000,
                      "model_load_seconds": 2.5},
            "controls": {"configured_context": 50000},
            "llm": {"prompt_tokens": 100, "completion_tokens": 30,
                    "total_tokens": 130, "peak_context": 100, "tokens_per_second": 20},
            "agent": {"iterations": 2, "tool_calls": 3, "successful_tool_calls": 3,
                      "failed_tool_calls": 0, "files_changed": 1, "lines_added": 4,
                      "lines_removed": 1, "self_corrected": False},
            "evaluation": {
                "build": {"passed": True, "exit_code": 0, "command": ["python", "-m", "compileall"]},
                "public_tests": {"passed": True, "exit_code": 0, "tests_total": 3,
                                 "tests_passed": 3, "tests_failed": 0, "tests_skipped": 0,
                                 "command": ["python", "-m", "unittest"], "stdout": "", "stderr": "OK"},
                "hidden_tests": {"passed": True, "exit_code": 0, "tests_total": 2,
                                 "tests_passed": 2, "tests_failed": 0, "tests_skipped": 0,
                                 "command": ["python", "-m", "unittest"], "stdout": "", "stderr": "OK"},
                "analysis": None,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = write_run_reports(record, [record], root / "reports")
            expected = root / "reports" / "qwen3.5-9b-q6" / "creation-coupon-001"
            self.assertEqual(paths["html"], expected / "run-123.html")
            self.assertTrue(paths["html"].is_file())
            self.assertTrue(paths["json"].is_file())
            self.assertTrue(paths["summary_html"].is_file())
            self.assertTrue(paths["summary_json"].is_file())
            report = paths["html"].read_text(encoding="utf-8")
            self.assertIn("Build and test evaluation", report)
            self.assertIn("2 passed / 0 failed / 2 total", report)
            self.assertIn("Configured context", report)
            self.assertIn("Loaded context", report)
            self.assertIn("50000", report)
            csv_path = write_report([record], root / "context.csv", "csv")
            self.assertIn("configured_context,loaded_context,model_load_seconds", csv_path.read_text())

    def test_test_result_parser_handles_unittest_pytest_and_unknown_outputs(self):
        self.assertEqual(_parse_test_counts("Ran 4 tests in 0.2s\n\nOK"),
                         {"tests_total": 4, "tests_failed": 0, "tests_skipped": 0, "tests_passed": 4})
        self.assertEqual(_parse_test_counts("2 passed, 1 failed"),
                         {"tests_total": 3, "tests_passed": 2, "tests_failed": 1, "tests_skipped": 0})
        self.assertEqual(_parse_test_counts("custom runner complete"),
                         {"tests_total": None, "tests_passed": None, "tests_failed": None, "tests_skipped": None})
        self.assertEqual(_parse_test_counts("Ran 4 tests in 0.2s\n\nOK (skipped=1)"),
                         {"tests_total": 4, "tests_failed": 0, "tests_skipped": 1, "tests_passed": 3})

    def test_lmstudio_streaming_tool_call_and_usage_parsing(self):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 22, "completion_tokens": 9, "total_tokens": 31}},
        ]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                for chunk in chunks:
                    yield ("data: " + json.dumps(chunk) + "\n\n").encode()
                yield b"data: [DONE]\n\n"

        provider = LMStudioProvider("http://localhost:1234/v1", "test-model")
        with patch("local_swe_benchmark.providers.lmstudio.urllib.request.urlopen", return_value=Response()):
            result = provider.chat([], [], .2, 1.0, 7, 100, 20)
        self.assertEqual(result["usage"]["prompt_tokens"], 22)
        self.assertEqual(result["finish_reason"], "tool_calls")
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(result["tool_calls"][0]["function"]["arguments"]), {"path": "a.py"})

    def test_lmstudio_loads_profile_context_before_inference(self):
        class JsonResponse:
            def __init__(self, value):
                self.value = value

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(self.value).encode()

        responses = [
            JsonResponse({"models": [{"key": "test-model", "loaded_instances": []}]}),
            JsonResponse({"status": "loaded", "instance_id": "test-model-instance",
                          "load_time_seconds": 2.5,
                          "load_config": {"context_length": 50176}}),
        ]
        provider = LMStudioProvider("http://localhost:1234/v1", "test-model")
        with patch("local_swe_benchmark.providers.lmstudio.urllib.request.urlopen",
                   side_effect=responses) as urlopen:
            loaded = provider.ensure_model_loaded(50000)

        self.assertEqual(urlopen.call_count, 2)
        inventory_request = urlopen.call_args_list[0].args[0]
        self.assertEqual(inventory_request.full_url, "http://localhost:1234/api/v1/models")
        self.assertEqual(inventory_request.method, "GET")
        self.assertIsNone(inventory_request.data)
        load_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(load_request.full_url, "http://localhost:1234/api/v1/models/load")
        self.assertEqual(json.loads(load_request.data), {
            "model": "test-model", "context_length": 50000, "echo_load_config": True})
        self.assertEqual(loaded["context_length"], 50176)
        self.assertEqual(provider.model, "test-model-instance")

    def test_lmstudio_reloads_model_when_existing_context_differs(self):
        class JsonResponse:
            def __init__(self, value):
                self.value = value

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(self.value).encode()

        responses = [
            JsonResponse({"models": [{"key": "test-model", "loaded_instances": [
                {"id": "test-model-old", "config": {"context_length": 32768}}]}]}),
            JsonResponse({"instance_id": "test-model-old"}),
            JsonResponse({"status": "loaded", "instance_id": "test-model-new",
                          "load_time_seconds": 1.0,
                          "load_config": {"context_length": 50176}}),
        ]
        provider = LMStudioProvider("http://localhost:1234/v1", "test-model")
        with patch("local_swe_benchmark.providers.lmstudio.urllib.request.urlopen",
                   side_effect=responses) as urlopen:
            loaded = provider.ensure_model_loaded(50000)

        unload_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(unload_request.full_url, "http://localhost:1234/api/v1/models/unload")
        self.assertEqual(json.loads(unload_request.data), {"instance_id": "test-model-old"})
        self.assertEqual(loaded["instance_id"], "test-model-new")
        self.assertEqual(loaded["context_length"], 50176)

    def test_lmstudio_reuses_context_rounded_up_to_next_512_tokens(self):
        class JsonResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"models": [{"key": "test-model", "loaded_instances": [
                    {"id": "test-model-instance", "config": {"context_length": 50176}}]}]}).encode()

        provider = LMStudioProvider("http://localhost:1234/v1", "test-model")
        with patch("local_swe_benchmark.providers.lmstudio.urllib.request.urlopen",
                   return_value=JsonResponse()) as urlopen:
            loaded = provider.ensure_model_loaded(50000)

        urlopen.assert_called_once()
        self.assertEqual(loaded["context_length"], 50176)
        self.assertTrue(loaded["reused"])


if __name__ == "__main__":
    unittest.main()
