import io
import json
import urllib.error

import pytest

from patchpilot.agents import llm
from patchpilot.agents.llm import GroqProvider


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_requires_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        GroqProvider()


def test_request_shape_and_parse(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    seen = {}

    def fake(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        seen["body"] = json.loads(req.data)
        return _Resp({"choices": [{"message": {"content": '{"ok": 1}'}}]})

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake)
    out = GroqProvider().complete("plan", "sys", "user")
    assert out == '{"ok": 1}'
    assert seen["url"].startswith("https://api.groq.com/openai/v1/")
    assert seen["auth"] == "Bearer gsk_test"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}


def test_model_env_override(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("GROQ_MODEL", "some-model")
    assert GroqProvider().model == "some-model"


def test_http_error_does_not_leak_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")

    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(b'{"error":"rate limit"}'))

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as e:
        GroqProvider().complete("plan", "s", "u")
    assert "429" in str(e.value) and "gsk_secret" not in str(e.value)
