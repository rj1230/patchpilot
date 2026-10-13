"""Typed domain models (blueprint section 2 data model)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskState(str, Enum):
    RECEIVED = "RECEIVED"
    VALIDATE_REQUEST = "VALIDATE_REQUEST"
    CLONE_REPOSITORY = "CLONE_REPOSITORY"
    ANALYZE_REPOSITORY = "ANALYZE_REPOSITORY"
    RETRIEVE_CONTEXT = "RETRIEVE_CONTEXT"
    CREATE_PLAN = "CREATE_PLAN"
    POLICY_CHECK = "POLICY_CHECK"
    GENERATE_PATCH = "GENERATE_PATCH"
    STATIC_ANALYSIS = "STATIC_ANALYSIS"
    RUN_TESTS_IN_SANDBOX = "RUN_TESTS_IN_SANDBOX"
    ANALYZE_FAILURE = "ANALYZE_FAILURE"
    REPAIR_PATCH = "REPAIR_PATCH"
    AWAIT_APPROVAL = "AWAIT_APPROVAL"
    CREATE_PULL_REQUEST = "CREATE_PULL_REQUEST"
    AWAIT_REVIEW = "AWAIT_REVIEW"
    MERGED = "MERGED"
    REJECTED = "REJECTED"
    BLOCK_AND_REPORT = "BLOCK_AND_REPORT"
    FAILED = "FAILED"


class Task(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task_"))
    repository: str  # allowlisted repo name, e.g. "acme/api"
    repo_path: str  # local path (MVP)
    base_sha: str | None = None
    issue_ref: str | None = None
    issue_title: str
    issue_body: str = ""
    requester: str = "cli"
    created_at: datetime = Field(default_factory=utcnow)


class Citation(BaseModel):
    path: str
    symbol: str
    start_line: int
    end_line: int
    commit_sha: str
    chunk_hash: str

    def ref(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}@{self.commit_sha[:8]} ({self.symbol})"


class Plan(BaseModel):
    hypothesis: str
    steps: list[str]
    expected_files: list[str]
    intended_commands: list[str] = Field(default_factory=lambda: ["ruff", "pytest"])
    assumptions: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class FileEdit(BaseModel):
    """Exact search/replace edit; `old` empty means create a new file."""

    path: str
    old: str = ""
    new: str


class PatchProposal(BaseModel):
    rationale: str
    edits: list[FileEdit]
    confidence: float = Field(0.5, ge=0, le=1)


class PatchAttempt(BaseModel):
    attempt_no: int
    base_sha: str
    branch: str
    diff: str = ""
    changed_files: list[str] = Field(default_factory=list)
    changed_lines: int = 0
    rationale: str = ""
    confidence: float = 0.0
    error: str | None = None


class CheckResult(BaseModel):
    check_type: str
    command: list[str]
    exit_code: int
    duration_s: float
    summary: str
    stdout_hash: str = ""
    passed: bool = False
    output_tail: str = ""


class PolicyDecision(BaseModel):
    rule_id: str
    decision: str  # "allow" | "block" | "escalate"
    rationale: str
    actor: str = "policy_engine"
    timestamp: datetime = Field(default_factory=utcnow)


class AuditEvent(BaseModel):
    seq: int
    task_id: str
    correlation_id: str
    event_type: str
    payload: dict
    payload_hash: str
    prev_hash: str
    actor: str
    timestamp: datetime = Field(default_factory=utcnow)
    event_hash: str = ""
