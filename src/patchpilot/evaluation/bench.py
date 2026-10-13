"""Benchmark harness: frozen tasks, hidden acceptance tests, metrics report.
Each case = repo fixture + scripted-or-real provider + expected disposition + optional acceptance test."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..agents.llm import LLMProvider
from ..agents.workflow import RunResult, Workflow
from ..domain.models import Task
from ..domain.models import TaskState as S
from ..domain.policy import PolicyEngine, load_policy
from ..integrations.github import DryRunPRClient
from ..sandbox.runner import Sandbox


@dataclass
class BenchCase:
    id: str
    repo_fixture: Path
    issue_title: str
    issue_body: str
    provider_factory: object  # callable() -> LLMProvider
    expect: str  # "pr" | "blocked"
    split: str = "dev"  # dev | validation | holdout
    acceptance_test: str | None = None  # hidden test source, run against the patched result
    category: str = "bug_fix"


@dataclass
class CaseOutcome:
    case_id: str
    split: str
    category: str
    state: str
    expected: str
    resolved: bool  # expected outcome met AND acceptance passed
    policy_compliant: bool
    attempts: int
    llm_calls: int
    duration_s: float
    checks_passed: bool
    notes: str = ""


def _acceptance(repo: Path, patch_files: dict[str, str], test_src: str) -> bool:
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "r"
        shutil.copytree(repo, ws, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".ruff_cache"))
        for rel, content in patch_files.items():
            (ws / rel).write_text(content)
        (ws / "test_hidden_acceptance.py").write_text(test_src)
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_hidden_acceptance.py"],
                           cwd=ws, capture_output=True, text=True, timeout=120)
        return p.returncode == 0


def run_case(case: BenchCase, policy_path: Path, work: Path) -> tuple[CaseOutcome, RunResult]:
    repo = work / case.id
    shutil.copytree(case.repo_fixture, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".ruff_cache"))
    llm: LLMProvider = case.provider_factory()
    pol = PolicyEngine(load_policy(policy_path))
    wf = Workflow(llm, pol, Sandbox(), artifacts_dir=work / "artifacts", pr_client=DryRunPRClient(work / "prs"))
    task = Task(repository=f"bench/{case.id}", repo_path=str(repo), issue_title=case.issue_title, issue_body=case.issue_body)
    t0 = time.monotonic()
    r = wf.run(task)
    dur = time.monotonic() - t0
    got = "pr" if r.state == S.AWAIT_REVIEW else "blocked" if r.state == S.BLOCK_AND_REPORT else r.state.value.lower()
    resolved = got == case.expect
    notes = ""
    if resolved and case.expect == "pr" and case.acceptance_test:
        resolved = _acceptance(case.repo_fixture, r.final_files, case.acceptance_test)
        notes = "acceptance " + ("passed" if resolved else "FAILED")
    protected_touched = bool(pol.protected_hits(list(r.final_files)))
    return CaseOutcome(case.id, case.split, case.category, r.state.value, case.expect, resolved,
                       not protected_touched and r.audit.verify(), len(r.attempts), r.llm_calls, round(dur, 2),
                       bool(r.checks) and all(c.passed for c in r.checks), notes or r.reason[:80]), r


def summarize(outcomes: list[CaseOutcome]) -> dict:
    n = len(outcomes) or 1
    pr_cases = [o for o in outcomes if o.expected == "pr"]
    blocked_cases = [o for o in outcomes if o.expected == "blocked"]
    durs = sorted(o.duration_s for o in outcomes) or [0]
    return {
        "cases": len(outcomes),
        "task_resolution_rate": round(sum(o.resolved for o in pr_cases) / (len(pr_cases) or 1), 3),
        "first_attempt_rate": round(sum(1 for o in pr_cases if o.resolved and o.attempts == 1) / (len(pr_cases) or 1), 3),
        "correct_block_rate": round(sum(o.resolved for o in blocked_cases) / (len(blocked_cases) or 1), 3),
        "policy_compliance": round(sum(o.policy_compliant for o in outcomes) / n, 3),
        "avg_attempts": round(sum(o.attempts for o in outcomes) / n, 2),
        "avg_llm_calls": round(sum(o.llm_calls for o in outcomes) / n, 2),
        "p95_duration_s": durs[min(len(durs) - 1, int(0.95 * len(durs)))],
    }


def to_markdown(outcomes: list[CaseOutcome], summary: dict) -> str:
    rows = "\n".join(f"| {o.case_id} | {o.split} | {o.category} | {o.expected} | {o.state} | {'yes' if o.resolved else 'NO'} | {o.attempts} | {o.duration_s}s |" for o in outcomes)
    return ("# PatchPilot benchmark\n\n| case | split | category | expected | state | resolved | attempts | time |\n|---|---|---|---|---|---|---|---|\n"
            + rows + "\n\n```json\n" + json.dumps(summary, indent=2) + "\n```\n")
