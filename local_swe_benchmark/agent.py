from __future__ import annotations

import json
import time
from typing import Any, Protocol

from .tools import TOOL_SCHEMAS, ToolBox


SYSTEM_PROMPT = """You are a software engineering agent working in a repository.
Solve the user's task by inspecting and changing the repository with the
provided tools. The task description and repository are the only source of the
solution; do not assume or request a reference patch. Use tools to inspect
relevant code, make focused changes, and run the available public tests/build.
Read tool output, correct errors you introduce, and verify the final diff and
repository status before finishing. Do not access files outside the repository
or attempt to inspect benchmark evaluator data. When done, return a concise
summary, tests run and their outcomes, and any remaining limitation. If you
cannot finish, state the concrete blocker."""


class ChatProvider(Protocol):
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             temperature: float, top_p: float, seed: int | None,
             max_tokens: int, timeout_seconds: int) -> dict[str, Any]: ...


class Agent:
    """Provider-independent explicit tool loop."""

    def __init__(self, provider: ChatProvider, toolbox: ToolBox, event: Any,
                 temperature: float, top_p: float, seed: int | None,
                 max_iterations: int, max_tool_calls: int, max_tokens: int,
                 timeout_seconds: int):
        self.provider, self.toolbox, self.event = provider, toolbox, event
        self.temperature, self.top_p, self.seed = temperature, top_p, seed
        self.max_iterations, self.max_tool_calls = max_iterations, max_tool_calls
        self.max_tokens, self.timeout_seconds = max_tokens, timeout_seconds
        self.iterations = self.tool_calls = self.successful_tool_calls = 0
        self.failed_tool_calls = self.prompt_tokens = self.completion_tokens = 0
        self.contexts: list[int | None] = []
        self.completion_token_observations: list[int] = []
        self.ttft: list[float] = []
        self.generation_seconds = 0.0
        self.requests = 0
        self.reported_model: str | None = None
        self.stop_reason = "completed"
        self.deadline = time.monotonic() + timeout_seconds

    def run(self, task_description: str) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task_description},
        ]
        while self.iterations < self.max_iterations:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                self.stop_reason = "timeout"
                return ""
            token_budget = self.max_tokens - self.completion_tokens
            if token_budget <= 0:
                self.stop_reason = "token_limit"
                return ""
            self.iterations += 1
            self.event({"type": "model_request", "iteration": self.iterations,
                        "messages": messages, "temperature": self.temperature,
                        "top_p": self.top_p, "seed": self.seed,
                        "max_tokens": token_budget})
            response = self.provider.chat(messages, TOOL_SCHEMAS, self.temperature,
                                          self.top_p, self.seed, token_budget,
                                          max(1, int(remaining)))
            self.requests += 1
            if response.get("reported_model"):
                self.reported_model = response["reported_model"]
            usage = response.get("usage") or {}
            if isinstance(usage.get("prompt_tokens"), int):
                self.prompt_tokens += usage["prompt_tokens"]
                self.contexts.append(usage["prompt_tokens"])
            else:
                self.contexts.append(None)
            if isinstance(usage.get("completion_tokens"), int):
                self.completion_tokens += usage["completion_tokens"]
                self.completion_token_observations.append(usage["completion_tokens"])
            if response.get("ttft_seconds") is not None:
                self.ttft.append(response["ttft_seconds"])
            self.generation_seconds += response.get("generation_seconds", 0.0)
            self.event({"type": "model_response", "iteration": self.iterations,
                        "usage": usage, "ttft_seconds": response.get("ttft_seconds"),
                        "reported_model": response.get("reported_model"),
                        "generation_seconds": response.get("generation_seconds"),
                        "content": response.get("content"), "tool_calls": response.get("tool_calls")})
            if self.completion_tokens >= self.max_tokens:
                self.stop_reason = "token_limit"
                return ""
            if response.get("finish_reason") == "length":
                self.stop_reason = "token_limit"
                return ""
            calls = response.get("tool_calls", [])
            assistant: dict[str, Any] = {"role": "assistant", "content": response.get("content") or None}
            if calls:
                assistant["tool_calls"] = calls
            messages.append(assistant)
            if not calls:
                return response.get("content") or ""
            for call in calls:
                if time.monotonic() >= self.deadline:
                    self.stop_reason = "timeout"
                    return ""
                if self.tool_calls >= self.max_tool_calls:
                    self.stop_reason = "tool_limit"
                    return ""
                self.tool_calls += 1
                name = call.get("function", {}).get("name", "")
                try:
                    args = json.loads(call.get("function", {}).get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("Tool arguments must be an object")
                    result = self.toolbox.execute(name, args)
                except Exception as exc:
                    result = {"ok": False, "error": f"Invalid tool call: {exc}"}
                    self.event({"type": "tool", "name": name, "result": result})
                self.successful_tool_calls += bool(result.get("ok"))
                self.failed_tool_calls += not bool(result.get("ok"))
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": json.dumps(result, ensure_ascii=False)})
        self.stop_reason = "iteration_limit"
        return ""
