from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any


class ProviderError(RuntimeError):
    def __init__(self, message: str, kind: str = "infrastructure"):
        super().__init__(message)
        self.kind = kind


class LMStudioProvider:
    def __init__(self, base_url: str, model: str, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             temperature: float, top_p: float, seed: int | None,
             max_tokens: int, timeout_seconds: int) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages,
                                "tools": tools, "tool_choice": "auto",
                                "temperature": temperature, "top_p": top_p,
                                "max_tokens": max_tokens, "stream": True,
                                "stream_options": {"include_usage": True}}
        if seed is not None:
            body["seed"] = seed
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.base_url + "/chat/completions",
                                         data=json.dumps(body).encode(), headers=headers)
        start = time.monotonic()
        ttft = None
        content: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage = None
        finish_reason = None
        reported_model = None
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    if chunk.get("model"):
                        reported_model = chunk["model"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    if ttft is None and (delta.get("content") or delta.get("tool_calls")):
                        ttft = time.monotonic() - start
                    if choices[0].get("finish_reason"):
                        finish_reason = choices[0]["finish_reason"]
                    if delta.get("content"):
                        content.append(delta["content"])
                    for tc in delta.get("tool_calls", []):
                        slot = tool_calls.setdefault(tc["index"], {"id": "", "type": "function",
                                                                       "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            slot["id"] += tc["id"]
                        fn = tc.get("function", {})
                        slot["function"]["name"] += fn.get("name", "")
                        slot["function"]["arguments"] += fn.get("arguments", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            kind = "model_error" if exc.code in {400, 404, 422} else "infrastructure"
            if re.search(r"context.{0,20}(length|window|limit)|maximum context|too many tokens", detail, re.IGNORECASE):
                kind = "context_error"
            raise ProviderError(f"LM Studio returned HTTP {exc.code}: {detail}", kind) from exc
        except urllib.error.URLError as exc:
            kind = "timeout" if isinstance(exc.reason, TimeoutError) else "infrastructure"
            raise ProviderError(f"LM Studio request failed: {exc.reason}", kind) from exc
        except (TimeoutError, json.JSONDecodeError) as exc:
            kind = "timeout" if isinstance(exc, TimeoutError) else "model_error"
            raise ProviderError(f"LM Studio response failed: {exc}", kind) from exc
        end = time.monotonic()
        return {"content": "".join(content),
                "tool_calls": [tool_calls[i] for i in sorted(tool_calls)],
                "usage": usage, "ttft_seconds": ttft,
                "finish_reason": finish_reason,
                "reported_model": reported_model,
                "generation_seconds": end - start,
                "response_seconds": end - start}
