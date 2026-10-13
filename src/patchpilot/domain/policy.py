"""Deterministic policy engine. The LLM never decides permissions."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .models import PolicyDecision, RiskLevel

SENSITIVE_WORDS = ("auth", "payment", "billing", "iam", "password", "credential", "token", "crypto")

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|secret|passwd|password|token)\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
]


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif c == "*":
            out += "[^/]*"
            i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(c)
            i += 1
    return re.compile(f"^{out}$")


def normalize(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def glob_match(path: str, pattern: str) -> bool:
    return bool(_glob_regex(pattern).match(normalize(path)))


class Autonomy(BaseModel):
    create_pull_request: bool = True
    auto_merge: bool = False
    deploy_to_production: bool = False


class Limits(BaseModel):
    max_files_changed: int = 8
    max_patch_lines: int = 300
    max_patch_attempts: int = 3
    max_runtime_minutes: float = 20
    max_llm_calls: int = 12


class Approval(BaseModel):
    risk_levels: list[RiskLevel] = Field(default_factory=lambda: [RiskLevel.MEDIUM, RiskLevel.HIGH])
    coverage_delta_negative: bool = True
    files_changed_over: int = 5


class Policy(BaseModel):
    autonomy: Autonomy = Field(default_factory=Autonomy)
    limits: Limits = Field(default_factory=Limits)
    protected_paths: list[str] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=lambda: ["lint", "unit_tests"])
    human_approval: Approval = Field(default_factory=Approval)
    repository_allowlist: list[str] = Field(default_factory=list)  # empty = allow all (dev only)


def load_policy(path: str | Path) -> Policy:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Policy.model_validate(data)


class PolicyEngine:
    def __init__(self, policy: Policy):
        self.policy = policy
        self.decisions: list[PolicyDecision] = []

    def _rec(self, rule: str, decision: str, why: str) -> PolicyDecision:
        d = PolicyDecision(rule_id=rule, decision=decision, rationale=why)
        self.decisions.append(d)
        return d

    @property
    def blocked(self) -> bool:
        return any(d.decision == "block" for d in self.decisions)

    def check_repository(self, repository: str) -> PolicyDecision:
        allow = self.policy.repository_allowlist
        if allow and repository not in allow:
            return self._rec("repo_allowlist", "block", f"{repository} is not in the repository allowlist")
        return self._rec("repo_allowlist", "allow", f"{repository} permitted")

    def check_autonomy(self) -> PolicyDecision:
        if not self.policy.autonomy.create_pull_request:
            return self._rec("autonomy.create_pull_request", "block", "PR creation disabled by policy")
        return self._rec("autonomy", "allow", "PR creation permitted; auto-merge/deploy never performed")

    def protected_hits(self, files: list[str]) -> list[tuple[str, str]]:
        return [(f, p) for f in files for p in self.policy.protected_paths if glob_match(f, p)]

    def check_scope(self, files: list[str], changed_lines: int | None = None, stage: str = "plan") -> list[PolicyDecision]:
        out: list[PolicyDecision] = []
        hits = self.protected_hits(files)
        if hits:
            desc = ", ".join(f"{f} (matches {p})" for f, p in hits)
            out.append(self._rec("protected_paths", "block", f"{stage} touches protected paths: {desc}"))
        lim = self.policy.limits
        if len(files) > lim.max_files_changed:
            out.append(self._rec("max_files_changed", "block", f"{len(files)} files > limit {lim.max_files_changed}"))
        if changed_lines is not None and changed_lines > lim.max_patch_lines:
            out.append(self._rec("max_patch_lines", "block", f"{changed_lines} lines > limit {lim.max_patch_lines}"))
        if not out:
            out.append(self._rec("scope", "allow", f"{stage} scope within limits ({len(files)} files)"))
        return out

    def check_added_text(self, added_lines: list[str]) -> PolicyDecision:
        for line in added_lines:
            for pat in SECRET_PATTERNS:
                if pat.search(line):
                    return self._rec("secret_scan", "block", "secret-like content in added lines")
        return self._rec("secret_scan", "allow", "no secret-like content in diff")

    def check_budget(self, attempts: int, elapsed_s: float, llm_calls: int) -> PolicyDecision | None:
        lim = self.policy.limits
        if attempts > lim.max_patch_attempts:
            return self._rec("max_patch_attempts", "block", f"attempt budget {lim.max_patch_attempts} exhausted")
        if elapsed_s > lim.max_runtime_minutes * 60:
            return self._rec("max_runtime", "block", f"runtime budget {lim.max_runtime_minutes} min exceeded")
        if llm_calls > lim.max_llm_calls:
            return self._rec("max_llm_calls", "block", f"LLM call budget {lim.max_llm_calls} exceeded")
        return None

    def classify_risk(self, issue_text: str, files: list[str], changed_lines: int = 0) -> RiskLevel:
        blob = (issue_text + " " + " ".join(files)).lower()
        if any(w in blob for w in SENSITIVE_WORDS):
            return RiskLevel.HIGH
        if len(files) > 5 or changed_lines > 150:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def needs_approval(self, risk: RiskLevel, files_changed: int, coverage_delta: float | None = None) -> bool:
        a = self.policy.human_approval
        reasons = []
        if risk in a.risk_levels:
            reasons.append(f"risk={risk.value}")
        if a.coverage_delta_negative and coverage_delta is not None and coverage_delta < 0:
            reasons.append("coverage_delta<0")
        if files_changed > a.files_changed_over:
            reasons.append(f"files_changed>{a.files_changed_over}")
        if reasons:
            self._rec("human_approval", "escalate", "human approval required: " + ", ".join(reasons))
            return True
        return False

    def required_checks_met(self, results: dict[str, bool]) -> PolicyDecision:
        missing = [c for c in self.policy.required_checks if not results.get(c, False)]
        if missing:
            return self._rec("required_checks", "block", f"required checks not green: {missing}")
        return self._rec("required_checks", "allow", "all required checks green")
