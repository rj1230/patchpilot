import pytest

from patchpilot.domain.audit import AuditLog, verify_chain
from patchpilot.domain.models import RiskLevel
from patchpilot.domain.models import TaskState as S
from patchpilot.domain.policy import PolicyEngine, glob_match, load_policy
from patchpilot.domain.state import IllegalTransition, StateMachine


@pytest.fixture
def engine():
    return PolicyEngine(load_policy("policies/default.yaml"))


@pytest.mark.parametrize("path,hit", [
    (".github/workflows/ci.yml", True), ("infra/main.tf", True), ("src/auth/login.py", True),
    ("config/my_secret.txt", True), ("secret.txt", True), ("src/app/validators.py", False),
    ("./migrations/001.sql", True), ("docs/author.md", False),
])
def test_protected_paths(engine, path, hit):
    assert bool(engine.protected_hits([path])) is hit


def test_glob_basics():
    assert glob_match("a/b/c.py", "**/*.py") and glob_match("c.py", "**/*.py")
    assert not glob_match("a/b/c.txt", "**/*.py")


def test_scope_limits(engine):
    ds = engine.check_scope([f"f{i}.py" for i in range(9)], 10)
    assert any(d.rule_id == "max_files_changed" for d in ds)
    assert any(d.rule_id == "max_patch_lines" for d in engine.check_scope(["a.py"], 301, "patch"))
    assert engine.blocked


def test_secret_scan(engine):
    assert engine.check_added_text(['api_key = "abcdefghijklmnop1234"']).decision == "block"
    assert engine.check_added_text(["x = 1"]).decision == "allow"


def test_budget(engine):
    assert engine.check_budget(4, 0, 0).rule_id == "max_patch_attempts"
    assert engine.check_budget(1, 21 * 60, 0).rule_id == "max_runtime"
    assert engine.check_budget(1, 1, 1) is None


def test_risk_and_approval(engine):
    assert engine.classify_risk("fix email validator", ["validators.py"]) == RiskLevel.LOW
    assert engine.classify_risk("fix login", ["auth/login.py"]) == RiskLevel.HIGH
    assert engine.classify_risk("x", [f"f{i}" for i in range(6)]) == RiskLevel.MEDIUM
    assert not engine.needs_approval(RiskLevel.LOW, 1)
    assert engine.needs_approval(RiskLevel.HIGH, 1)
    assert engine.needs_approval(RiskLevel.LOW, 6)
    assert engine.needs_approval(RiskLevel.LOW, 1, coverage_delta=-0.5)


def test_required_checks(engine):
    assert engine.required_checks_met({"lint": True, "unit_tests": True}).decision == "allow"
    assert engine.required_checks_met({"lint": True}).decision == "block"


def test_state_machine():
    sm = StateMachine()
    sm.to(S.VALIDATE_REQUEST)
    with pytest.raises(IllegalTransition):
        sm.to(S.CREATE_PULL_REQUEST)  # cannot skip patch/validation
    sm.to(S.BLOCK_AND_REPORT)
    assert sm.terminal
    with pytest.raises(IllegalTransition):
        sm.to(S.GENERATE_PATCH)  # blocked runs never continue


def test_policy_block_cannot_reach_pr():
    from patchpilot.domain.state import TRANSITIONS
    assert S.CREATE_PULL_REQUEST not in TRANSITIONS[S.POLICY_CHECK]
    assert S.CREATE_PULL_REQUEST not in TRANSITIONS[S.GENERATE_PATCH]


def test_audit_chain_detects_tampering(tmp_path):
    log = AuditLog("t1", "c1", tmp_path / "a.jsonl")
    for i in range(3):
        log.emit("evt", {"i": i})
    assert log.verify()
    log.events[1].payload["i"] = 99
    assert not verify_chain(log.events)
    assert (tmp_path / "a.jsonl").read_text().count("\n") == 3
