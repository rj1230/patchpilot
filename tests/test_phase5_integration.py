import hashlib
import hmac
import json

from conftest import FIX_CODE, FIX_TEST, PLAN_OK, ROOT, patch_json
from fastapi.testclient import TestClient

from patchpilot.agents.llm import MockProvider
from patchpilot.api.app import Settings, create_app
from patchpilot.cli import main
from patchpilot.evaluation.bench import run_case, summarize
from patchpilot.evaluation.demo_cases import demo_cases
from patchpilot.integrations.github import GitHubClient, parse_issue_event, verify_signature

SECRET = "whsec_test"


def sig(body: bytes, secret=SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_verification():
    body = b'{"a":1}'
    assert verify_signature(SECRET, body, sig(body))
    assert not verify_signature(SECRET, body, sig(body, "other"))
    assert not verify_signature(SECRET, body, None) and not verify_signature("", body, sig(body))
    assert not verify_signature(SECRET, body, "sha1=abc")


def test_github_client_flow_with_fake_transport(make_workflow, make_task):
    calls = []

    def transport(method, url, headers, payload):
        calls.append((method, url.split("/repos/")[1], payload))
        assert headers["Authorization"].startswith("Bearer ")
        if method == "GET":
            return 404, {}
        if url.endswith("/pulls"):
            return 201, {"html_url": "https://github.com/acme/validator-bug/pull/7"}
        return 201, {}

    gh = GitHubClient("acme/validator-bug", "ghs_faketoken", transport=transport)
    wf, _ = make_workflow({"plan": [PLAN_OK], "patch": [patch_json([FIX_CODE, FIX_TEST])]}, pr_client=gh)
    r = wf.run(make_task())
    assert r.pr_url.endswith("/pull/7") and r.state.value == "AWAIT_REVIEW"
    assert any(m == "POST" and p.endswith("/git/refs") for m, p, _ in calls)
    puts = [p for m, p, _ in calls if m == "PUT"]
    assert any("contents/app/validators.py" in p for p in puts)
    ref_payload = next(pl for m, p, pl in calls if m == "POST" and p.endswith("/git/refs"))
    assert ref_payload["sha"] == r.attempts[-1].base_sha  # branch cut from the pinned SHA
    assert not any(p.endswith("/merge") or "merge" in p for _, p, _ in calls)  # never merges


def test_parse_issue_event():
    ev = {"action": "labeled", "issue": {"number": 12, "title": "t", "body": "b", "labels": [{"name": "patchpilot"}], "user": {"login": "ada"}},
          "repository": {"full_name": "acme/validator-bug"}}
    assert parse_issue_event(ev)["issue_ref"] == "#12"
    ev["issue"]["labels"] = []
    assert parse_issue_event(ev) is None


def _client(repo, tmp_path, script):
    st = Settings(webhook_secret=SECRET, policy_path=str(ROOT / "policies/default.yaml"), artifacts_dir=tmp_path / "art",
                  repos={"acme/validator-bug": str(repo)}, llm_factory=lambda: MockProvider(script))
    return TestClient(create_app(st))


def test_api_webhook_to_pr_and_dedup(repo, tmp_path):
    c = _client(repo, tmp_path, {"plan": [PLAN_OK], "patch": [patch_json([FIX_CODE, FIX_TEST])]})
    payload = json.dumps({"action": "labeled", "issue": {"number": 12, "title": "validate_email accepts empty string", "body": "must be False",
                          "labels": [{"name": "patchpilot"}], "user": {"login": "ada"}}, "repository": {"full_name": "acme/validator-bug"}}).encode()
    h = {"X-Hub-Signature-256": sig(payload), "X-GitHub-Event": "issues", "X-GitHub-Delivery": "d1", "Content-Type": "application/json"}
    assert c.post("/webhooks/github", content=payload, headers={**h, "X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    res = c.post("/webhooks/github", content=payload, headers=h)
    assert res.status_code == 202 and res.json()["status"] == "accepted"
    assert c.post("/webhooks/github", content=payload, headers=h).json()["status"] == "duplicate-ignored"  # replay-safe
    st = c.get(f"/tasks/{res.json()['task_id']}").json()
    assert st["state"] == "AWAIT_REVIEW" and st["audit_verified"] is True and st["pr_url"].startswith("file://")


def test_api_rejects_non_allowlisted_repo(repo, tmp_path):
    c = _client(repo, tmp_path, {"plan": [PLAN_OK], "patch": ["{}"]})
    assert c.post("/tasks", json={"repository": "evil/other", "issue_title": "x"}).status_code == 403


def test_api_human_approval_flow(repo, tmp_path):
    plan = json.dumps({"hypothesis": "password check", "steps": ["fix"], "expected_files": ["app/validators.py"]})
    c = _client(repo, tmp_path, {"plan": [plan], "patch": [patch_json([FIX_CODE, FIX_TEST])]})
    tid = c.post("/tasks", json={"repository": "acme/validator-bug", "issue_title": "Fix password strength check", "issue_body": "validators"}).json()["task_id"]
    assert c.get(f"/tasks/{tid}").json()["state"] == "AWAIT_APPROVAL"
    d = c.post(f"/tasks/{tid}/decision", json={"approve": True, "reviewer": "grace"}).json()
    assert d["state"] == "AWAIT_REVIEW" and d["pr_url"]
    assert c.post(f"/tasks/{tid}/decision", json={"approve": True}).status_code == 409  # cannot approve twice


def test_bench_demo_suite(tmp_path):
    outs = [run_case(c, ROOT / "policies/default.yaml", tmp_path)[0] for c in demo_cases()]
    s = summarize(outs)
    assert s["task_resolution_rate"] == 1.0 and s["correct_block_rate"] == 1.0 and s["policy_compliance"] == 1.0
    assert {o.case_id: o.attempts for o in outs}["empty-email-repair"] == 2


def test_cli_bench_and_audit_verify(tmp_path, make_workflow, make_task):
    out = tmp_path / "b.md"
    assert main(["bench", "--policy", str(ROOT / "policies/default.yaml"), "--out", str(out)]) == 0
    assert "task_resolution_rate" in out.read_text()
    wf, _ = make_workflow({"plan": [PLAN_OK], "patch": [patch_json([FIX_CODE, FIX_TEST])]})
    r = wf.run(make_task())
    log = tmp_path / "artifacts" / r.task.id / "audit.jsonl"
    assert main(["verify-audit", str(log)]) == 0
    log.write_text(log.read_text().replace("indexed", "indexeX", 1))  # tamper
    assert main(["verify-audit", str(log)]) == 1
