"""FastAPI control plane: webhook intake, task submission/status, human approval."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ..agents.llm import GroqProvider
from ..agents.workflow import RunResult, Workflow
from ..domain.models import Task
from ..domain.models import TaskState as S
from ..domain.policy import PolicyEngine, load_policy
from ..domain.state import TERMINAL, StateMachine
from ..integrations.github import (
    DryRunPRClient,
    parse_issue_event,
    verify_signature,
)


@dataclass(slots=True)
class Settings:
    webhook_secret: str = ""
    policy_path: str = "policies/default.yaml"
    artifacts_dir: Path = Path("artifacts")
    repos: dict[str, str] = field(default_factory=dict)
    llm_factory: Callable[[], Any] | None = None
    pr_client_factory: Callable[[Task], Any] | None = None
    sandbox: str = "local"
    llm_provider: str = "groq"

    @staticmethod
    def from_env() -> Settings:
        try:
            repos = json.loads(os.environ.get("PATCHPILOT_REPOS", "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                'PATCHPILOT_REPOS must be valid JSON, e.g. \'{"demo": "/absolute/path/to/repo"}\''
            ) from exc

        if not isinstance(repos, dict):
            raise RuntimeError("PATCHPILOT_REPOS must be a JSON object.")

        return Settings(
            webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
            policy_path=os.environ.get(
                "PATCHPILOT_POLICY",
                "policies/default.yaml",
            ),
            artifacts_dir=Path(os.environ.get("PATCHPILOT_ARTIFACTS", "artifacts")),
            repos={str(name): str(path) for name, path in repos.items()},
            llm_factory=GroqProvider,
            sandbox=os.environ.get("PATCHPILOT_SANDBOX", "local"),
            llm_provider=os.environ.get("PATCHPILOT_LLM_PROVIDER", "groq"),
        )


class SubmitTask(BaseModel):
    repository: str
    issue_title: str = Field(min_length=1)
    issue_body: str = ""
    issue_ref: str | None = None
    base_sha: str | None = None


class Decision(BaseModel):
    approve: bool
    reviewer: str = Field(default="reviewer", min_length=1)


@dataclass(slots=True)
class RunEntry:
    result: RunResult
    workflow: Workflow | None = None


class TaskRegistry:
    """Thread-safe in-memory registry for background workflow runs."""

    def __init__(self) -> None:
        self._runs: dict[str, RunEntry] = {}
        self._lock = threading.RLock()

    def put(self, task_id: str, entry: RunEntry) -> None:
        with self._lock:
            self._runs[task_id] = entry

    def get(self, task_id: str) -> RunEntry:
        with self._lock:
            entry = self._runs.get(task_id)

        if entry is None:
            raise KeyError(task_id)

        return entry

    def update(
        self,
        task_id: str,
        result: RunResult,
        workflow: Workflow,
    ) -> None:
        with self._lock:
            self._runs[task_id] = RunEntry(
                result=result,
                workflow=workflow,
            )

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._runs)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    app = FastAPI(title="PatchPilot", version="0.1.0")

    runs = TaskRegistry()
    seen_deliveries: set[str] = set()
    deliveries_lock = threading.Lock()

    config.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def make_workflow(task: Task) -> Workflow:
        if config.llm_factory is None:
            raise RuntimeError("No LLM factory configured. Provide Settings.llm_factory.")

        policy = PolicyEngine(load_policy(config.policy_path))

        pr_client = (
            config.pr_client_factory(task)
            if config.pr_client_factory
            else DryRunPRClient(config.artifacts_dir / "prs")
        )

        return Workflow(
            config.llm_factory(),
            policy,
            artifacts_dir=config.artifacts_dir,
            pr_client=pr_client,
        )

    def execute(task: Task) -> None:
        try:
            workflow = make_workflow(task)

            # Make the task visible immediately as queued.
            runs.update(
                task.id,
                RunResult(
                    task=task,
                    state=S.RECEIVED,
                    audit=None,
                ),
                workflow,
            )

            result = workflow.run(task)
            runs.update(task.id, result, workflow)

        except Exception as exc:
            try:
                entry = runs.get(task.id)
                entry.result.state = S.FAILED
                entry.result.reason = f"Workflow startup failed: {exc}"
            except KeyError:
                pass

    def submit(
        fields: dict[str, Any],
        background: BackgroundTasks,
    ) -> Task:
        repository = fields["repository"]
        repo_path = config.repos.get(repository)

        if repo_path is None:
            raise HTTPException(
                status_code=403,
                detail=(f"repository {repository!r} is not allowlisted"),
            )

        if not Path(repo_path).exists():
            raise HTTPException(
                status_code=500,
                detail=(f"allowlisted repository path does not exist: {repo_path}"),
            )

        task = Task(repo_path=repo_path, **fields)

        runs.put(
            task.id,
            RunEntry(
                result=RunResult(
                    task=task,
                    state=S.RECEIVED,
                    audit=None,
                )
            ),
        )

        background.add_task(execute, task)
        return task

    def get_run_or_404(task_id: str) -> RunEntry:
        try:
            return runs.get(task_id)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail="unknown task",
            ) from None

    def serialize_run(entry: RunEntry) -> dict[str, Any]:
        result = entry.result
        payload = result.summary()

        payload["policy"] = [decision.model_dump(mode="json") for decision in result.decisions]

        payload["audit_verified"] = result.audit.verify() if result.audit else None
        payload["pr_body"] = result.pr_body
        payload["running"] = result.state not in TERMINAL

        return payload

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "llm_ready": config.llm_factory is not None,
            "provider": config.llm_provider,
            "sandbox": config.sandbox,
            "repositories": len(config.repos),
            "tasks": len(runs.ids()),
        }

    @app.get("/repos")
    def repos() -> list[str]:
        return sorted(config.repos)

    @app.post("/tasks", status_code=202)
    def create_task(
        body: SubmitTask,
        background: BackgroundTasks,
    ) -> dict[str, str]:
        task = submit(body.model_dump(), background)
        return {
            "task_id": task.id,
            "state": S.RECEIVED.value,
        }

    @app.get("/tasks")
    def list_tasks() -> list[dict[str, Any]]:
        return [serialize_run(runs.get(task_id)) for task_id in runs.ids()]

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        entry = get_run_or_404(task_id)
        return serialize_run(entry)

    @app.post("/tasks/{task_id}/decision")
    def decide(
        task_id: str,
        decision: Decision,
    ) -> dict[str, Any]:
        entry = get_run_or_404(task_id)
        result = entry.result

        if result.state != S.AWAIT_APPROVAL:
            raise HTTPException(
                status_code=409,
                detail=(f"task is in {result.state.value}, not AWAIT_APPROVAL"),
            )

        if entry.workflow is None:
            raise HTTPException(
                status_code=500,
                detail="workflow is not available for this task",
            )

        state_machine = StateMachine(S.AWAIT_APPROVAL)
        state_machine.history = list(result.state_history)

        result.audit.emit(
            "human_decision",
            {
                "approve": decision.approve,
                "reviewer": decision.reviewer,
            },
            actor=decision.reviewer,
        )

        if not decision.approve:
            state_machine.to(S.REJECTED)
            result.state = S.REJECTED
            result.reason = f"rejected by {decision.reviewer}"
            return serialize_run(entry)

        entry.workflow.open_pr(state_machine, result)
        return serialize_run(entry)

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(
        request: Request,
        background: BackgroundTasks,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
    ) -> dict[str, str]:
        body = await request.body()

        if not verify_signature(
            config.webhook_secret,
            body,
            x_hub_signature_256,
        ):
            raise HTTPException(
                status_code=401,
                detail="invalid signature",
            )

        if x_github_delivery:
            with deliveries_lock:
                if x_github_delivery in seen_deliveries:
                    return {"status": "duplicate-ignored"}

                seen_deliveries.add(x_github_delivery)

        if x_github_event != "issues":
            return {
                "status": "ignored",
                "event": x_github_event or "unknown",
            }

        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=400,
                detail="invalid JSON payload",
            ) from None

        fields = parse_issue_event(event)

        if not fields:
            return {"status": "ignored"}

        task = submit(fields, background)
        return {
            "status": "accepted",
            "task_id": task.id,
        }

    return app

