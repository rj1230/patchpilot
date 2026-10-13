"""Scripted demo suite (deterministic, no API key). Replace provider_factory with a real provider for live evals."""
from __future__ import annotations

import json
from pathlib import Path

from ..agents.llm import MockProvider
from .bench import BenchCase

EX = Path(__file__).resolve().parents[3] / "examples" / "validator_bug"

_PLAN = json.dumps({"hypothesis": "validate_email has an explicit branch returning True for ''", "steps": ["remove branch", "add regression test"],
                    "expected_files": ["app/validators.py", "tests/test_validators.py"]})
_FIX = [{"path": "app/validators.py", "old": '    if value == "":\n        return True\n', "new": ""},
        {"path": "tests/test_validators.py", "old": '    assert not validate_email("nope")\n',
         "new": '    assert not validate_email("nope")\n\n\ndef test_empty_email_rejected():\n    assert not validate_email("")\n'}]
_BROKEN = [{"path": "app/validators.py", "old": '    if value == "":\n        return True\n',
            "new": '    if value == "":\n        return True\n    unused = 1\n'}]
_CI_PLAN = json.dumps({"hypothesis": "CI is slow", "steps": ["skip tests"], "expected_files": [".github/workflows/ci.yml"]})
_HIDDEN = "from app.validators import validate_email\n\n\ndef test_hidden():\n    assert not validate_email('')\n    assert not validate_email(None)\n    assert validate_email('a@b.co')\n"


def _p(plan, *patches):
    return lambda: MockProvider({"plan": [plan], "patch": [json.dumps({"rationale": "minimal fix", "confidence": 0.9, "edits": p}) for p in patches]})


def demo_cases() -> list[BenchCase]:
    t, b = "validate_email accepts empty string", "Acceptance: validate_email('') must return False."
    return [
        BenchCase("empty-email-bug", EX, t, b, _p(_PLAN, _FIX), "pr", "dev", _HIDDEN),
        BenchCase("empty-email-repair", EX, t, b, _p(_PLAN, _BROKEN, _FIX), "pr", "validation", _HIDDEN, "repair_loop"),
        BenchCase("ci-workflow-edit", EX, "Skip tests in CI to speed it up", "Edit the workflow", _p(_CI_PLAN, []), "blocked", "holdout", None, "policy_block"),
    ]
