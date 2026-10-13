"""GitHub integration: webhook signature verification + branch/commit/PR via REST (injectable transport)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.request
from collections.abc import Callable

Transport = Callable[[str, str, dict, dict | None], tuple[int, dict]]


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of X-Hub-Signature-256."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def urllib_transport(method: str, url: str, headers: dict, payload: dict | None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"message": e.read().decode()[:300]}


class GitHubClient:
    """Uses a short-lived, repository-scoped installation token (GitHub App). Needs contents:write and
    pull_requests:write only; never admin. Branch is created from the pinned base SHA."""

    def __init__(self, repo: str, token: str, base_branch: str = "main", transport: Transport = urllib_transport,
                 api: str = "https://api.github.com"):
        self.repo, self.base_branch, self.t, self.api = repo, base_branch, transport, api
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                  "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "patchpilot"}

    def _req(self, method: str, path: str, payload: dict | None = None) -> dict:
        status, data = self.t(method, f"{self.api}/repos/{self.repo}{path}", self.h, payload)
        if status >= 300:
            raise RuntimeError(f"GitHub {method} {path} -> {status}: {data.get('message', '')[:120]}")
        return data

    def create_pull_request(self, run) -> str:
        """`run` is a RunResult; run.final_files maps changed path -> final content."""
        sha = run.attempts[-1].base_sha
        branch = run.branch
        self._req("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
        for path, content in run.final_files.items():
            existing = None
            status, data = self.t("GET", f"{self.api}/repos/{self.repo}/contents/{path}?ref={branch}", self.h, None)
            if status == 200:
                existing = data.get("sha")
            body = {"message": f"PatchPilot: update {path}", "branch": branch,
                    "content": base64.b64encode(content.encode()).decode()}
            if existing:
                body["sha"] = existing
            self._req("PUT", f"/contents/{path}", body)
        pr = self._req("POST", "/pulls", {"title": run.pr_title, "head": branch, "base": self.base_branch,
                                          "body": run.pr_body, "draft": False})
        return pr["html_url"]


class DryRunPRClient:
    """Writes the PR payload to disk instead of calling GitHub (default for local/eval)."""

    def __init__(self, out_dir):
        self.out_dir = out_dir

    def create_pull_request(self, run) -> str:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        f = self.out_dir / f"{run.task.id}.pr.json"
        f.write_text(json.dumps({"title": run.pr_title, "branch": run.branch, "body": run.pr_body, "diff": run.final_diff}, indent=2))
        return f"file://{f}"


def parse_issue_event(payload: dict, trigger_label: str = "patchpilot") -> dict | None:
    """Return task fields for an `issues` webhook that should start a run, else None."""
    if payload.get("action") not in ("opened", "labeled"):
        return None
    issue, repo = payload.get("issue") or {}, payload.get("repository") or {}
    labels = {lb.get("name") for lb in issue.get("labels", [])}
    if trigger_label not in labels or not repo.get("full_name"):
        return None
    return {"repository": repo["full_name"], "issue_ref": f"#{issue.get('number')}",
            "issue_title": issue.get("title", ""), "issue_body": issue.get("body") or "",
            "requester": (issue.get("user") or {}).get("login", "github")}
