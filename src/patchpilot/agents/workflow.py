"""Orchestrator: typed state machine driving retrieval -> plan -> patch -> checks -> PR draft."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ..domain.audit import AuditLog
from ..domain.models import (
    CheckResult,
    PatchAttempt,
    PatchProposal,
    Plan,
    PolicyDecision,
    RiskLevel,
    Task,
    new_id,
)
from ..domain.models import TaskState as S
from ..domain.policy import PolicyEngine, normalize
from ..domain.state import StateMachine
from ..repository.indexer import RepoIndex, build_index
from ..repository.retrieval import Retriever
from ..sandbox.runner import Sandbox
from . import prompts
from .llm import LLMProvider, parse_json
from .patching import PatchError, apply_edits

CHECK_ORDER_STATIC = ["ruff"]
CHECK_ORDER_TESTS = ["pytest"]


@dataclass
class RunResult:
    task: Task
    state: S
    plan: Plan | None = None
    attempts: list[PatchAttempt] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    decisions: list[PolicyDecision] = field(default_factory=list)
    risk: RiskLevel = RiskLevel.LOW
    confidence: float = 0.0
    final_diff: str = ""
    final_files: dict[str, str] = field(default_factory=dict)
    pr_title: str = ""
    pr_body: str = ""
    branch: str = ""
    pr_url: str | None = None
    reason: str = ""
    llm_calls: int = 0
    duration_s: float = 0.0
    prompt_hash: str = ""
    audit: AuditLog | None = None
    state_history: list[S] = field(default_factory=list)

    def summary(self) -> dict:
        return {"task": self.task.id, "state": self.state.value, "attempts": len(self.attempts),
                "risk": self.risk.value, "confidence": round(self.confidence, 2), "reason": self.reason,
                "llm_calls": self.llm_calls, "duration_s": round(self.duration_s, 2), "pr_url": self.pr_url}


class Workflow:
    def __init__(self, llm: LLMProvider, policy: PolicyEngine, sandbox: Sandbox | None = None,
                 artifacts_dir: Path | None = None, pr_client=None):
        self.llm, self.policy, self.sandbox = llm, policy, sandbox or Sandbox()
        self.artifacts_dir, self.pr_client = artifacts_dir, pr_client

    # ---- helpers ------------------------------------------------------
    def _call(self, r: RunResult, role: str, system: str, user: str) -> str:
        r.llm_calls += 1
        r.audit.emit("llm_call", {"role": role, "prompt_hash": r.prompt_hash, "provider": self.llm.name,
                                  "input_hash": hashlib.sha256(user.encode()).hexdigest()[:16]}, actor="llm")
        return self.llm.complete(role, system, user)

    def _block(self, sm: StateMachine, r: RunResult, reason: str) -> RunResult:
        r.reason = reason
        sm.to(S.BLOCK_AND_REPORT)
        r.audit.emit("blocked", {"reason": reason})
        return self._finish(sm, r)

    def _fail(self, sm: StateMachine, r: RunResult, reason: str) -> RunResult:
        r.reason = reason
        sm.to(S.FAILED)
        r.audit.emit("failed", {"reason": reason})
        return self._finish(sm, r)

    def _finish(self, sm: StateMachine, r: RunResult) -> RunResult:
        r.state, r.state_history, r.decisions = sm.state, list(sm.history), list(self.policy.decisions)
        r.audit.emit("terminal", {"state": sm.state.value})
        self.sandbox.cleanup()
        if self.artifacts_dir:
            self._persist(r)
        return r

    def _persist(self, r: RunResult) -> None:
        d = self.artifacts_dir / r.task.id
        d.mkdir(parents=True, exist_ok=True)
        (d / "summary.json").write_text(json.dumps(r.summary(), indent=2))
        (d / "report.md").write_text(render_report(r))
        if r.final_diff:
            (d / "patch.diff").write_text(r.final_diff)
        (d / "checks.json").write_text(json.dumps([c.model_dump() for c in r.checks], indent=2))
        (d / "policy.json").write_text(json.dumps([x.model_dump(mode="json") for x in r.decisions], indent=2))
        (d / "audit.jsonl").write_text("\n".join(e.model_dump_json() for e in r.audit.events) + "\n")

    # ---- main run -----------------------------------------------------
    def run(self, task: Task) -> RunResult:
        t0 = time.monotonic()
        sm = StateMachine()
        audit = AuditLog(task.id, new_id("corr_"))
        r = RunResult(task=task, state=sm.state, audit=audit, prompt_hash=prompts.prompt_hash())
        audit.emit("received", {"repository": task.repository, "issue": task.issue_ref, "title": task.issue_title})
        try:
            return self._run(task, sm, r, t0)
        except Exception as e:  # never crash silently: stop safely, preserve artifacts, explain
            if sm.state not in (S.FAILED, S.BLOCK_AND_REPORT) and not sm.terminal:
                return self._fail(sm, r, f"unexpected error: {type(e).__name__}: {str(e)[:200]}")
            raise
        finally:
            r.duration_s = time.monotonic() - t0

    def _run(self, task: Task, sm: StateMachine, r: RunResult, t0: float) -> RunResult:
        pol = self.policy
        sm.to(S.VALIDATE_REQUEST)
        pol.check_repository(task.repository)
        pol.check_autonomy()
        if pol.blocked:
            return self._block(sm, r, "; ".join(d.rationale for d in pol.decisions if d.decision == "block"))
        issue_text = f"{task.issue_title}\n{task.issue_body}"
        inj = prompts.scan_injection(issue_text)
        if inj:
            r.audit.emit("injection_suspected", {"where": "issue", "patterns": inj})

        sm.to(S.CLONE_REPOSITORY)
        src = Path(task.repo_path)
        if not src.is_dir():
            return self._fail(sm, r, f"repository path not found: {task.repo_path}")

        sm.to(S.ANALYZE_REPOSITORY)
        index: RepoIndex = build_index(src, task.base_sha)
        base_sha = index.commit_sha
        r.audit.emit("indexed", {"sha": base_sha, "files": len(index.files), "chunks": len(index.chunks)})

        sm.to(S.RETRIEVE_CONTEXT)
        hits = Retriever(index).search(issue_text, k=10)
        evidence = [(h.chunk.citation().ref(), h.chunk.text) for h in hits]
        citations = [h.chunk.citation() for h in hits]
        for path in sorted({h.chunk.path for h in hits}):  # whole files: comments sit outside symbol chunks
            if prompts.scan_injection((index.root / path).read_text(errors="ignore")):
                r.audit.emit("injection_suspected", {"where": path})
        r.audit.emit("retrieved", {"refs": [e[0] for e in evidence]})

        sm.to(S.CREATE_PLAN)
        raw = self._call(r, "plan", prompts.PLAN_SYSTEM, prompts.plan_prompt(task.issue_title, task.issue_body, evidence, index.files))
        try:
            pd = parse_json(raw)
            plan = Plan(hypothesis=pd["hypothesis"], steps=pd["steps"], expected_files=pd["expected_files"],
                        assumptions=pd.get("assumptions", []), citations=citations[:6])
        except (ValueError, KeyError, ValidationError) as e:
            return self._fail(sm, r, f"planner returned invalid output: {e}")
        r.plan = plan
        r.audit.emit("plan", plan.model_dump(mode="json"), actor="llm")

        sm.to(S.POLICY_CHECK)
        pol.check_scope(plan.expected_files, None, "plan")
        r.risk = pol.classify_risk(issue_text, plan.expected_files)
        if pol.blocked:
            return self._block(sm, r, "; ".join(d.rationale for d in pol.decisions if d.decision == "block"))

        # ---- patch / validate / repair loop ----
        hint: str | None = None
        attempt_no = 0
        while True:
            attempt_no += 1
            over = pol.check_budget(attempt_no, time.monotonic() - t0, r.llm_calls)
            if over:
                return self._block(sm, r, over.rationale + " - stopping with evidence report")
            sm.to(S.GENERATE_PATCH)
            ws = self.sandbox.new_workspace(src)
            att = PatchAttempt(attempt_no=attempt_no, base_sha=base_sha, branch=f"patchpilot/{task.id}-a{attempt_no}")
            r.attempts.append(att)
            files = {p: (ws / p).read_text() for p in plan.expected_files if (ws / p).is_file()}
            raw = self._call(r, "patch", prompts.PATCH_SYSTEM, prompts.patch_prompt(plan.model_dump_json(exclude={"citations"}), files, hint))
            try:
                prop = PatchProposal.model_validate(parse_json(raw))
                # gate on proposed paths BEFORE touching the workspace
                n0 = len(pol.decisions)
                pol.check_scope(sorted({normalize(e.path) for e in prop.edits}), None, "patch paths")
                if any(d.decision == "block" for d in pol.decisions[n0:]):
                    return self._block(sm, r, "; ".join(d.rationale for d in pol.decisions[n0:] if d.decision == "block"))
                att.diff, att.changed_files, att.changed_lines, added = apply_edits(ws, prop.edits)
                att.rationale, att.confidence = prop.rationale, prop.confidence
            except (ValueError, ValidationError, PatchError) as e:
                att.error = f"invalid patch: {e}"
                sm.to(S.ANALYZE_FAILURE)
                hint = att.error
                r.audit.emit("patch_invalid", {"attempt": attempt_no, "error": att.error[:300]})
                sm.to(S.REPAIR_PATCH)
                continue
            r.audit.emit("patch", {"attempt": attempt_no, "files": att.changed_files, "lines": att.changed_lines,
                                   "diff_hash": hashlib.sha256(att.diff.encode()).hexdigest()[:16]}, actor="llm")

            # deterministic gate on the *actual* diff, not the plan
            n_before = len(pol.decisions)
            pol.check_scope(att.changed_files, att.changed_lines, "patch")
            pol.check_added_text(added)
            if any(d.decision == "block" for d in pol.decisions[n_before:]):
                reasons = "; ".join(d.rationale for d in pol.decisions[n_before:] if d.decision == "block")
                return self._block(sm, r, reasons)

            sm.to(S.STATIC_ANALYSIS)
            results: dict[str, bool] = {}
            failed: CheckResult | None = None
            for name in CHECK_ORDER_STATIC:
                c = self.sandbox.run_check(name, ws)
                r.checks.append(c)
                results[c.check_type] = c.passed
                r.audit.emit("check", {"name": name, "exit": c.exit_code, "hash": c.stdout_hash})
                if not c.passed:
                    failed = c
                    break
            if failed is None:
                sm.to(S.RUN_TESTS_IN_SANDBOX)
                for name in CHECK_ORDER_TESTS + self._extra_checks():
                    c = self.sandbox.run_check(name, ws)
                    r.checks.append(c)
                    results[c.check_type] = c.passed
                    r.audit.emit("check", {"name": name, "exit": c.exit_code, "hash": c.stdout_hash})
                    if not c.passed:
                        failed = c
                        break
            if failed is not None:
                if failed.exit_code == 127 or failed.exit_code == 124:  # scanner missing / timeout => terminal
                    return self._block(sm, r, f"{failed.summary} - requires human review")
                sm.to(S.ANALYZE_FAILURE)
                hint = f"{failed.summary}\n{failed.output_tail[-2500:]}"
                r.audit.emit("repair", {"attempt": attempt_no, "failed_check": failed.check_type})
                sm.to(S.REPAIR_PATCH)
                continue

            # all required checks green -> final policy gate
            gate = pol.required_checks_met(results)
            if gate.decision == "block":
                return self._block(sm, r, gate.rationale)
            r.final_diff, r.branch = att.diff, att.branch
            r.final_files = {p: (ws / p).read_text() for p in att.changed_files}  # before workspace cleanup
            r.confidence = round(min(att.confidence, 1.0 - 0.15 * (attempt_no - 1)), 2)
            r.risk = pol.classify_risk(issue_text, att.changed_files, att.changed_lines)
            r.pr_title = f"[PatchPilot] {task.issue_title}"[:120]
            r.pr_body = render_pr_body(r)
            if pol.needs_approval(r.risk, len(att.changed_files)):
                r.reason = "awaiting human approval before PR creation"
                sm.to(S.AWAIT_APPROVAL)
                return self._finish(sm, r)
            return self.open_pr(sm, r, ws_source=src)

    def _extra_checks(self) -> list[str]:
        return []

    # ---- PR creation (also used after human approval) ------------------
    def open_pr(self, sm: StateMachine, r: RunResult, ws_source: Path | None = None) -> RunResult:
        sm.to(S.CREATE_PULL_REQUEST)
        if self.pr_client is not None:
            try:
                r.pr_url = self.pr_client.create_pull_request(r)
            except Exception as e:
                return self._fail(sm, r, f"PR creation failed: {type(e).__name__}: {str(e)[:160]}")
        r.audit.emit("pr_created", {"url": r.pr_url, "branch": r.branch}, actor="integration")
        sm.to(S.AWAIT_REVIEW)
        r.reason = "PR ready for human review (no auto-merge)"
        return self._finish(sm, r)


def render_pr_body(r: RunResult) -> str:
    p = r.plan
    cites = "\n".join(f"- `{c.ref()}`" for c in (p.citations if p else []))
    checks = "\n".join(f"- {'PASS' if c.passed else 'FAIL'} - {c.summary} ({c.duration_s}s)" for c in r.checks)
    pol = "\n".join(f"- `{d.rule_id}`: {d.decision} - {d.rationale}" for d in r.decisions or [])
    att = r.attempts[-1] if r.attempts else None
    return f"""## Issue
{r.task.issue_title} ({r.task.issue_ref or 'n/a'})

## Root-cause hypothesis
{p.hypothesis if p else ''}

## Evidence
{cites}

## Plan
{chr(10).join(f'{i + 1}. {s}' for i, s in enumerate(p.steps)) if p else ''}

## Change summary
{att.rationale if att else ''}
Files: {', '.join(att.changed_files) if att else ''} ({att.changed_lines if att else 0} changed lines)

## Deterministic checks
{checks}

## Policy
{pol}

## Confidence / cost
confidence {r.confidence} - risk {r.risk.value} - attempts {len(r.attempts)} - LLM calls {r.llm_calls} - prompt `{r.prompt_hash}`

## Rollback
Close this PR or `git revert` the merge commit; no schema/config/infra changes are included.
"""


def render_report(r: RunResult) -> str:
    rows = "\n".join(f"- {d.rule_id}: **{d.decision}** - {d.rationale}" for d in r.decisions)
    ck = "\n".join(f"- {'PASS' if c.passed else 'FAIL'} {c.summary}" for c in r.checks)
    flow = " -> ".join(s.value for s in r.state_history)
    return (f"# Run {r.task.id}: {r.state.value}\n\n**Reason:** {r.reason}\n\n**Flow:** {flow}\n\n"
            f"## Policy decisions\n{rows}\n\n## Checks\n{ck or '(none run)'}\n\n"
            f"## PR body\n{r.pr_body or '(no PR)'}\n")
