"""Allowlisted command runner. The model never supplies a shell command, only a check name."""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..domain.models import CheckResult

# check name -> (policy check type, argv template)
ALLOWED: dict[str, tuple[str, list[str]]] = {
    "ruff": ("lint", ["ruff", "check", "--no-cache", "."]),
    "pytest": ("unit_tests", [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]),
    "bandit": ("sast", ["bandit", "-q", "-r", ".", "-x", "./tests"]),
    "pip_audit": ("dependency_scan", ["pip-audit", "--progress-spinner", "off"]),
}
SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "PYTHONPATH", "HOME", "TMPDIR")
OUTPUT_CAP = 20_000


class CommandNotAllowed(Exception):
    pass


@dataclass
class Limits:
    wall_seconds: int = 120
    memory_mb: int = 1024
    cpu_seconds: int = 120
    pids: int = 128
    file_mb: int = 50


def _scrubbed_env() -> dict[str, str]:
    # Keep only explicitly approved environment variables. Windows needs
    # system and user temp paths for reliable child-process initialization.
    allowed = set(SAFE_ENV_KEYS)

    if os.name == "nt":
        allowed.update({
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "PROGRAMDATA",
            "COMSPEC",
            "PATHEXT",
        })

    env = {
        k: v for k, v in os.environ.items()
        if k.upper() in {name.upper() for name in allowed}
    }

    if not env.get("PATH"):
        env["PATH"] = os.defpath

    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _preexec(limits: Limits):
    def fn():  # runs in the child: hard resource limits (POSIX)
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_mb << 20, limits.memory_mb << 20))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits.file_mb << 20, limits.file_mb << 20))
        os.setsid()
    return fn


class LocalBackend:
    """Environment-scrubbed local runner.

    POSIX uses resource limits and a new session. Windows uses a new
    process group and taskkill on timeout. This backend is not a security
    boundary for untrusted repositories; use DockerBackend or another
    isolated runner for those.
    """

    name = "local"

    def run(self, argv: list[str], cwd: Path, limits: Limits) -> tuple[int, str]:
        proc = None

        try:
            kwargs = {
                "cwd": cwd,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "env": _scrubbed_env(),
            }

            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["preexec_fn"] = _preexec(limits)

            proc = subprocess.Popen(argv, **kwargs)

            try:
                output, _ = proc.communicate(timeout=limits.wall_seconds)
                return proc.returncode, output or ""

            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        pass

                    if proc.poll() is None:
                        proc.kill()
                else:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

                    if proc.poll() is None:
                        proc.kill()

                output, _ = proc.communicate()
                return 124, (output or "") + "\nTIMEOUT"

        except FileNotFoundError:
            return 127, f"tool not found: {argv[0]}"
        except OSError as exc:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()
            return 1, f"runner OS error: {exc}"

class DockerBackend:
    """Ephemeral, non-root, read-only, network-less container per command."""

    name = "docker"

    def __init__(self, image: str = "patchpilot-runner:latest"):
        self.image = image

    def docker_argv(self, argv: list[str], cwd: Path, limits: Limits) -> list[str]:
        template_argv = ["python" if a == sys.executable else a for a in argv]
        return [
            "docker", "run", "--rm", "--network", "none", "--read-only", "--user", "65534:65534",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", f"{limits.memory_mb}m",
            "--cpus", "1", "--pids-limit", str(limits.pids), "--tmpfs", "/tmp:rw,size=64m,noexec",
            "-v", f"{cwd}:/work:rw", "-w", "/work", "-e", "PYTHONDONTWRITEBYTECODE=1",
            self.image, *template_argv,
        ]  # no docker.sock, no ssh keys, no host env passthrough

    def run(self, argv: list[str], cwd: Path, limits: Limits) -> tuple[int, str]:
        cmd = self.docker_argv(argv, cwd, limits)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=limits.wall_seconds)
            return p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            return 124, "TIMEOUT"
        except FileNotFoundError:
            return 127, "docker not available"


class Sandbox:
    def __init__(self, backend=None, limits: Limits | None = None):
        self.backend = backend or LocalBackend()
        self.limits = limits or Limits()
        self._workspaces: list[Path] = []

    def new_workspace(self, source: Path) -> Path:
        """Fresh copy per patch attempt; the source repository is never mutated."""
        ws = Path(tempfile.mkdtemp(prefix="pp_ws_")) / "repo"
        shutil.copytree(source, ws, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".ruff_cache"))
        self._workspaces.append(ws.parent)
        return ws

    def run_check(self, name: str, workspace: Path) -> CheckResult:
        if name not in ALLOWED:
            raise CommandNotAllowed(f"'{name}' is not an allowlisted check")
        check_type, argv = ALLOWED[name]
        t0 = time.monotonic()
        code, out = self.backend.run(list(argv), workspace, self.limits)
        dur = time.monotonic() - t0
        tail = out[-OUTPUT_CAP:]
        return CheckResult(
            check_type=check_type, command=argv, exit_code=code, duration_s=round(dur, 3),
            summary=_summarize(name, code, out), stdout_hash=hashlib.sha256(out.encode()).hexdigest()[:16],
            passed=(code == 0), output_tail=tail,
        )

    def cleanup(self) -> None:
        for w in self._workspaces:
            shutil.rmtree(w, ignore_errors=True)
        self._workspaces.clear()


def _summarize(name: str, code: int, out: str) -> str:
    if code == 127:
        return f"{name}: tool unavailable (scanner execution failure)"
    if code == 124:
        return f"{name}: timed out"
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    return f"{name}: {'passed' if code == 0 else 'failed'} - {last[:160]}"
