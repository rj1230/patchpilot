from pathlib import Path

import pytest

from patchpilot.sandbox.runner import CommandNotAllowed, DockerBackend, Limits, LocalBackend, Sandbox

GOOD = "def add(a, b):\n    return a + b\n"
TEST_OK = "from m import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
TEST_BAD = "from m import add\n\n\ndef test_add():\n    assert add(1, 2) == 4\n"


@pytest.fixture
def src(tmp_path):
    (tmp_path / "m.py").write_text(GOOD)
    (tmp_path / "test_m.py").write_text(TEST_OK)
    return tmp_path


def test_arbitrary_command_rejected(src):
    sb = Sandbox()
    ws = sb.new_workspace(src)
    for bad in ("rm -rf /", "bash", "curl http://evil", "python -c 'import os'"):
        with pytest.raises(CommandNotAllowed):
            sb.run_check(bad, ws)
    sb.cleanup()


def test_pytest_and_ruff_pass(src):
    sb = Sandbox()
    ws = sb.new_workspace(src)
    r = sb.run_check("pytest", ws)
    assert r.passed and r.check_type == "unit_tests" and r.stdout_hash
    assert sb.run_check("ruff", ws).passed
    sb.cleanup()


def test_failure_is_structured(src):
    (src / "test_m.py").write_text(TEST_BAD)
    sb = Sandbox()
    r = sb.run_check("pytest", sb.new_workspace(src))
    assert not r.passed and r.exit_code != 0 and "assert" in r.output_tail
    sb.cleanup()


def test_workspace_isolated_and_cleaned(src):
    sb = Sandbox()
    ws = sb.new_workspace(src)
    (ws / "m.py").write_text("BROKEN(")
    assert (src / "m.py").read_text() == GOOD  # original untouched
    root = ws.parent
    sb.cleanup()
    assert not root.exists()


def test_timeout_kills(src):
    (src / "test_m.py").write_text("import time\n\n\ndef test_slow():\n    time.sleep(30)\n")
    sb = Sandbox(limits=Limits(wall_seconds=2))
    r = sb.run_check("pytest", sb.new_workspace(src))
    assert r.exit_code == 124 and "timed out" in r.summary
    sb.cleanup()


def test_secrets_not_leaked_to_children(src, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 36)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    (src / "test_m.py").write_text(
        "import os\n\n\ndef test_env():\n    assert 'GITHUB_TOKEN' not in os.environ\n    assert 'ANTHROPIC_API_KEY' not in os.environ\n"
    )
    sb = Sandbox()
    assert sb.run_check("pytest", sb.new_workspace(src)).passed
    sb.cleanup()


def test_missing_tool_is_failure_not_pass(src):
    sb = Sandbox()
    r = sb.run_check("pip_audit", sb.new_workspace(src))
    if r.exit_code == 127:  # scanner absent => must block, never silently pass
        assert not r.passed and "scanner execution failure" in r.summary
    sb.cleanup()


def test_docker_command_is_locked_down():
    argv = DockerBackend().docker_argv(["pytest", "-q"], Path("/tmp/ws"), Limits())
    joined = " ".join(argv)
    for flag in ("--network none", "--read-only", "--user 65534:65534", "--cap-drop ALL", "no-new-privileges", "--pids-limit"):
        assert flag in joined
    assert "docker.sock" not in joined and "--privileged" not in joined and ".ssh" not in joined


def test_backend_is_pluggable(src):
    assert isinstance(Sandbox().backend, LocalBackend)
