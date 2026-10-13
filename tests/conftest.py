import json
import shutil
from pathlib import Path

import pytest

from patchpilot.agents.llm import MockProvider
from patchpilot.agents.workflow import Workflow
from patchpilot.domain.models import Task
from patchpilot.domain.policy import PolicyEngine, load_policy
from patchpilot.sandbox.runner import Sandbox

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "validator_bug"

PLAN_OK = json.dumps({
    "hypothesis": "validate_email returns True for '' because of an explicit empty-string branch",
    "steps": ["Remove the empty-string early return in validate_email", "Add regression test for empty and whitespace input"],
    "expected_files": ["app/validators.py", "tests/test_validators.py"],
    "assumptions": ["Empty string must be rejected per issue acceptance criteria"],
})
FIX_CODE = {"path": "app/validators.py", "old": '    if value == "":\n        return True\n', "new": ""}
FIX_TEST = {"path": "tests/test_validators.py",
            "old": '    assert not validate_email("nope")\n',
            "new": '    assert not validate_email("nope")\n\n\ndef test_empty_email_rejected():\n    assert not validate_email("")\n'}


def patch_json(edits, rationale="Reject empty email", confidence=0.9):
    return json.dumps({"rationale": rationale, "confidence": confidence, "edits": edits})


@pytest.fixture
def repo(tmp_path):
    dst = tmp_path / "validator_bug"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".ruff_cache"))
    return dst


@pytest.fixture
def make_task(repo):
    def _mk(title="validate_email accepts empty string", body="Acceptance: empty string must return False.", **kw):
        return Task(repository="acme/validator-bug", repo_path=str(repo), issue_ref="#12", issue_title=title, issue_body=body, **kw)
    return _mk


@pytest.fixture
def make_workflow(tmp_path):
    def _mk(script, policy_overrides=None, pr_client=None):
        pol = load_policy(ROOT / "policies" / "default.yaml")
        for k, v in (policy_overrides or {}).items():
            setattr(pol.limits, k, v)
        llm = MockProvider(script)
        wf = Workflow(llm, PolicyEngine(pol), Sandbox(), artifacts_dir=tmp_path / "artifacts", pr_client=pr_client)
        return wf, llm
    return _mk
