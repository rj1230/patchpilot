"""Provider-agnostic LLM interface. Models propose; deterministic code disposes."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol


class LLMProvider(Protocol):
    name: str

    def complete(self, role: str, system: str, user: str) -> str: ...


class MockProvider:
    """Scripted provider for tests/evals: `script[role]` is a list of responses (or callables(user)->str)."""

    name = "mock"

    def __init__(self, script: dict[str, list[str | Callable[[str], str]]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, str]] = []

    def complete(self, role: str, system: str, user: str) -> str:
        self.calls.append((role, user))
        queue = self.script.get(role) or []
        if not queue:
            raise RuntimeError(f"MockProvider has no scripted response left for role '{role}'")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item(user) if callable(item) else item


class GroqProvider:
    """Groq adapter (OpenAI-compatible chat completions, stdlib only). Needs GROQ_API_KEY.

    JSON mode is on by default because the planner and patcher must return a JSON object; the
    prompts already say "JSON", which Groq requires when response_format is json_object."""

    name = "groq"
    URL = "https://api.groq.com/openai/v1/chat/completions"
    DEFAULT_MODEL = "llama-3.3-70b-versatile"  # override with --model or GROQ_MODEL if Groq retires it

    def __init__(self, model: str | None = None, max_tokens: int = 4000, json_mode: bool = True):
        self.model = model or os.environ.get("GROQ_MODEL") or self.DEFAULT_MODEL
        self.max_tokens, self.json_mode = max_tokens, json_mode
        self.key = os.environ.get("GROQ_API_KEY")
        if not self.key:
            raise RuntimeError("GROQ_API_KEY not set")

    def complete(self, role: str, system: str, user: str) -> str:
        payload: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0.1,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self.URL,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "User-Agent": "patchpilot/0.1",
            },
        )  # default Python UA is often blocked by Cloudflare
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            # Surface the provider error without exposing the API key.
            detail = e.read().decode(errors="ignore")[:500]
            raise RuntimeError(f"Groq API error {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not connect to Groq API: {e.reason}") from e
        except TimeoutError as e:
            raise RuntimeError("Groq API request timed out after 120 seconds.") from e

        try:
            choices = data.get("choices") or []
            content = choices[0].get("message", {}).get("content") if choices else None
        except (AttributeError, IndexError, TypeError) as e:
            raise RuntimeError("Groq API returned an unexpected response structure.") from e

        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Groq API returned an empty response.")

        return content


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str) -> dict:
    t = _FENCE.sub("", text.strip())
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in model output")
    return json.loads(t[start : end + 1])

