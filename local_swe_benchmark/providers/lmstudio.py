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

    @property
    def management_base_url(self) -> str:
        """Return the LM Studio server root for its native model-management API."""
        if self.base_url.endswith("/v1"):
            return self.base_url[:-3]
        return self.base_url

    def _management_request(self, method: str, path: str,
                            payload: dict[str, Any] | None = None,
                            timeout_seconds: int = 120) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.management_base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            kind = "model_error" if exc.code in {400, 404, 422} else "infrastructure"
            if re.search(r"context.{0,20}(length|window|limit)|maximum context|too many tokens",
                         detail, re.IGNORECASE):
                kind = "context_error"
            raise ProviderError(
                f"LM Studio model-management API returned HTTP {exc.code}: {detail}", kind) from exc
        except urllib.error.URLError as exc:
            kind = "timeout" if isinstance(exc.reason, TimeoutError) else "infrastructure"
            raise ProviderError(f"LM Studio model-management request failed: {exc.reason}", kind) from exc
        except (TimeoutError, json.JSONDecodeError) as exc:
            kind = "timeout" if isinstance(exc, TimeoutError) else "model_error"
            raise ProviderError(f"LM Studio model-management response failed: {exc}", kind) from exc
        if not isinstance(result, dict):
            raise ProviderError("LM Studio model-management API returned a non-object response",
                                "model_error")
        return result

    def ensure_model_loaded(self, context_length: int,
                            timeout_seconds: int = 900) -> dict[str, Any]:
        """Load the configured model at the requested context and verify the result.

        The OpenAI-compatible chat-completions endpoint has no context-length
        parameter. LM Studio's native API must load the model with this setting.
        """
        if isinstance(context_length, bool) or not isinstance(context_length, int) or context_length < 1:
            raise ValueError("context_length must be a positive integer")

        inventory = self._management_request("GET", "/api/v1/models", timeout_seconds)
        models = inventory.get("models")
        if not isinstance(models, list):
            raise ProviderError("LM Studio model list did not contain a models array", "model_error")

        selected_model = next((item for item in models
                               if item.get("key") == self.model
                               or any(instance.get("id") == self.model
                                      for instance in item.get("loaded_instances", []))), None)
        loaded_instances = selected_model.get("loaded_instances", []) if selected_model else []

        for instance in loaded_instances:
            config = instance.get("config") or {}
            if config.get("context_length") == context_length:
                self.model = instance["id"]
                return {"instance_id": self.model, "context_length": context_length,
                        "load_time_seconds": None, "reused": True}

        # Existing instances of this exact model may have been loaded by the UI
        # with a different context. Remove only those instances before reloading.
        for instance in loaded_instances:
            instance_id = instance.get("id")
            if instance_id:
                self._management_request("POST", "/api/v1/models/unload",
                                         {"instance_id": instance_id}, timeout_seconds)

        loaded = self._management_request(
            "POST", "/api/v1/models/load",
            {"model": selected_model.get("key") if selected_model else self.model,
             "context_length": context_length,
             "echo_load_config": True}, timeout_seconds)
        instance_id = loaded.get("instance_id")
        loaded_context = (loaded.get("load_config") or {}).get("context_length")
        if loaded.get("status") != "loaded" or not instance_id:
            raise ProviderError(f"LM Studio did not confirm model loading: {loaded}", "model_error")
        if loaded_context != context_length:
            raise ProviderError(
                f"LM Studio loaded context_length={loaded_context!r}, "
                f"but the profile requested {context_length}", "context_error")
        self.model = instance_id
        return {"instance_id": instance_id, "context_length": loaded_context,
                "load_time_seconds": loaded.get("load_time_seconds"), "reused": False}

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
