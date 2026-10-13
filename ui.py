"""PatchPilot Control Plane (Streamlit).

Terminal 1 (project root, GROQ_API_KEY set):
    uvicorn --factory patchpilot.api.app:create_app --port 8000

Terminal 2:
    streamlit run ui.py

Optional environment variables for the API:
    PATCHPILOT_REPOS='{"demo": "/absolute/path/to/repo"}'
    PATCHPILOT_SANDBOX=docker
    PATCHPILOT_LLM_PROVIDER=groq
    GITHUB_WEBHOOK_SECRET=...

Optional environment variable for the UI:
    PATCHPILOT_API_URL=http://127.0.0.1:8000
"""

from __future__ import annotations

import os
from typing import Any

import requests
import streamlit as st

DEFAULT_API = os.getenv("PATCHPILOT_API_URL", "http://127.0.0.1:8000")
TIMEOUT = 15
POLL_SECONDS = 2

TERMINAL_STATES = {
    "AWAIT_REVIEW",
    "AWAIT_APPROVAL",
    "MERGED",
    "REJECTED",
    "BLOCK_AND_REPORT",
    "FAILED",
}

FAILED_STATES = {"FAILED", "BLOCK_AND_REPORT", "REJECTED"}

STATE_META: dict[str, tuple[str, str, str]] = {
    "RECEIVED": ("🔵", "Queued", "blue"),
    "VALIDATE_REQUEST": ("🔵", "Validating request", "blue"),
    "CLONE_REPOSITORY": ("🔵", "Preparing repository", "blue"),
    "ANALYZE_REPOSITORY": ("🔵", "Indexing repository", "blue"),
    "RETRIEVE_CONTEXT": ("🔵", "Retrieving cited evidence", "blue"),
    "CREATE_PLAN": ("🧠", "LLM planning", "blue"),
    "POLICY_CHECK": ("🛡️", "Policy check on plan", "orange"),
    "GENERATE_PATCH": ("🧩", "LLM generating patch", "blue"),
    "STATIC_ANALYSIS": ("🔎", "Ruff in sandbox", "orange"),
    "RUN_TESTS_IN_SANDBOX": ("🧪", "Pytest in sandbox", "orange"),
    "ANALYZE_FAILURE": ("🔁", "Analysing failure", "orange"),
    "REPAIR_PATCH": ("🔁", "Bounded repair", "orange"),
    "AWAIT_APPROVAL": ("⏸️", "Awaiting human approval", "orange"),
    "CREATE_PULL_REQUEST": ("📬", "Opening pull request", "blue"),
    "AWAIT_REVIEW": ("✅", "PR ready for human review", "green"),
    "MERGED": ("🟣", "Merged", "violet"),
    "REJECTED": ("❌", "Rejected by reviewer", "red"),
    "BLOCK_AND_REPORT": ("⛔", "Blocked by policy", "red"),
    "FAILED": ("💥", "Failed", "red"),
}

PIPELINE = [
    ("VALIDATE_REQUEST", "Validate"),
    ("CLONE_REPOSITORY", "Prepare"),
    ("ANALYZE_REPOSITORY", "Index"),
    ("RETRIEVE_CONTEXT", "Retrieve"),
    ("CREATE_PLAN", "Plan"),
    ("POLICY_CHECK", "Policy"),
    ("GENERATE_PATCH", "Patch"),
    ("STATIC_ANALYSIS", "Ruff"),
    ("RUN_TESTS_IN_SANDBOX", "Tests"),
    ("AWAIT_APPROVAL", "Approval"),
    ("CREATE_PULL_REQUEST", "Open PR"),
    ("AWAIT_REVIEW", "Review"),
]

PAGES = [
    "📊 Dashboard",
    "📥 Submit issue",
    "🔍 Task run",
    "🙋 Approvals",
]

P_DASH, P_SUBMIT, P_TASK, P_APPROVE = PAGES

DEMOS = {
    "bug": (
        "validate_email accepts empty string",
        ("Acceptance criteria: validate_email('') must return False. Add a regression test."),
    ),
    "blocked": (
        "Skip tests in CI to speed it up",
        (
            "Edit .github/workflows/ci.yml so the test step is skipped. "
            "Expected: PatchPilot blocks this at the policy check."
        ),
    ),
}

st.set_page_config(
    page_title="PatchPilot Control Plane",
    page_icon="🛠️",
    layout="wide",
)


# --------------------------------------------------------------------------- API
@st.cache_resource
def get_http_session() -> requests.Session:
    return requests.Session()


def api(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[bool, Any]:
    """Call the PatchPilot API and never raise."""
    base = st.session_state["api_base"].rstrip("/")
    url = f"{base}{path}"

    try:
        response = get_http_session().request(
            method=method,
            url=url,
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return False, (f"Cannot reach the API at {base} ({type(exc).__name__}). Is uvicorn running?")

    try:
        data: Any = response.json()
    except ValueError:
        data = response.text

    if not response.ok:
        if isinstance(data, dict):
            detail = data.get("detail", data)
        else:
            detail = data

        return False, f"{response.status_code}: {detail}"

    return True, data


def fetch_tasks() -> list[dict[str, Any]]:
    ok, data = api("GET", "/tasks")

    if not ok or not isinstance(data, list):
        return []

    return list(reversed(data))


def fetch_task(task_id: str) -> tuple[bool, dict[str, Any] | str]:
    ok, data = api("GET", f"/tasks/{task_id}")

    if not ok:
        return False, data

    if not isinstance(data, dict):
        return False, "Unexpected response from API."

    return True, data


# --------------------------------------------------------------------- session
def init_session() -> None:
    defaults = {
        "api_base": DEFAULT_API,
        "auto_refresh": True,
        "sel_task": "",
        "reviewer": "reviewer",
        "f_title": DEMOS["bug"][0],
        "f_body": DEMOS["bug"][1],
        "page": P_DASH,
    }

    for key, value in defaults.items():
        st.session_state.setdefault(key, value)

    # Button callbacks request navigation before widgets are rendered.
    if "_goto" in st.session_state:
        st.session_state["page"] = st.session_state.pop("_goto")


init_session()


def goto(page: str, task_id: str | None = None) -> None:
    if task_id:
        st.session_state["sel_task"] = task_id

    st.session_state["_goto"] = page


def prefill_demo(kind: str) -> None:
    st.session_state["f_title"], st.session_state["f_body"] = DEMOS[kind]


# --------------------------------------------------------------------- helpers
def short_id(task_id: str) -> str:
    return task_id.removeprefix("task_")[:8]


def badge(state: str) -> str:
    icon, label, color = STATE_META.get(state, ("⚪", state, "gray"))
    return f":{color}[**{icon} {state}**] {label}"


def task_label(task: dict[str, Any]) -> str:
    title = task.get("issue_title", "")[:45]
    return f"{short_id(task['task_id'])} · {title} · {task['state']}"


def task_ids(tasks: list[dict[str, Any]]) -> list[str]:
    return [task["task_id"] for task in tasks]


def format_task_choice(task_id: str, tasks: list[dict[str, Any]]) -> str:
    by_id = {task["task_id"]: task for task in tasks}
    return task_label(by_id[task_id]) if task_id in by_id else task_id


def count_states(
    tasks: list[dict[str, Any]],
    *states: str,
) -> int:
    return sum(task["state"] in states for task in tasks)


def render_table(rows: list[dict[str, Any]]) -> None:
    if rows:
        st.dataframe(rows, hide_index=True, use_container_width=True)


# ------------------------------------------------------------------ task parts
def pipeline_view(task: dict[str, Any]) -> None:
    history = task.get("state_history", [])
    state = task["state"]
    seen = set(history)

    stages = [
        (state_id, label)
        for state_id, label in PIPELINE
        if state_id != "AWAIT_APPROVAL" or "AWAIT_APPROVAL" in seen
    ]

    visited = [state_id for state_id, _ in stages if state_id in seen]
    last_visited = visited[-1] if visited else None
    failed = state in FAILED_STATES

    for start in range(0, len(stages), 6):
        row = stages[start : start + 6]
        columns = st.columns(len(row))

        for column, (state_id, label) in zip(columns, row, strict=True):
            if state_id not in seen:
                icon = "⚪"
            elif failed and state_id == last_visited:
                icon = "❌"
            elif state_id == state and state == "AWAIT_APPROVAL":
                icon = "⏸️"
            elif state_id == state and state not in TERMINAL_STATES:
                icon = "🔄"
            else:
                icon = "✅"

            if icon == "⚪":
                column.markdown(
                    f"{icon}<br>{label}",
                    unsafe_allow_html=True,
                )
            else:
                column.markdown(
                    f"{icon}<br>**{label}**",
                    unsafe_allow_html=True,
                )

    repair_count = history.count("REPAIR_PATCH")

    if repair_count:
        st.caption(f"🔁 Bounded repair loop used {repair_count} time(s) (policy cap: 3 attempts).")


def outcome_banner(task: dict[str, Any]) -> None:
    state = task["state"]
    reason = task.get("reason") or ""

    if state == "AWAIT_REVIEW":
        st.success(f"PR ready for human review. {reason}")
    elif state == "AWAIT_APPROVAL":
        st.warning(f"Waiting for a human decision before a PR is opened. {reason}")
    elif state == "BLOCK_AND_REPORT":
        st.error(f"Blocked by deterministic policy/validation: {reason}")
    elif state == "FAILED":
        st.error(f"Run failed safely: {reason}")
    elif state == "REJECTED":
        st.error(reason or "Rejected by reviewer.")
    elif task.get("running"):
        icon, label, _ = STATE_META.get(state, ("🔄", state, ""))
        st.info(f"{icon} {label}…")


def metrics_row(task: dict[str, Any]) -> None:
    audit_verified = task.get("audit_verified")

    if audit_verified is True:
        audit_label = "Verified ✅"
    elif audit_verified is False:
        audit_label = "BROKEN ❌"
    else:
        audit_label = "Pending"

    columns = st.columns(6)

    columns[0].metric("Attempts", task.get("attempts", 0))
    columns[1].metric("LLM calls", task.get("llm_calls", 0))
    columns[2].metric("Risk", str(task.get("risk", "-")).upper())
    columns[3].metric(
        "Confidence",
        f"{task.get('confidence', 0):.2f}",
    )
    columns[4].metric(
        "Duration",
        f"{task.get('duration_s', 0):.1f}s",
    )
    columns[5].metric("Audit chain", audit_label)


def plan_tab(task: dict[str, Any]) -> None:
    plan = task.get("plan")

    if not plan:
        st.info("No plan yet. The run may still be retrieving context, or it stopped before planning.")
        return

    st.markdown(f"**Root-cause hypothesis:** {plan['hypothesis']}")
    st.markdown("**Plan**")

    for index, step in enumerate(plan.get("steps", []), start=1):
        st.markdown(f"{index}. {step}")

    expected_files = plan.get("expected_files", [])
    if expected_files:
        files = ", ".join(f"`{file}`" for file in expected_files)
        st.markdown(f"**Expected files:** {files}")

    assumptions = plan.get("assumptions", [])
    if assumptions:
        st.markdown("**Assumptions:** " + "; ".join(assumptions))

    citations = plan.get("citations", [])
    if not citations:
        st.info("No repository citations were recorded.")
        return

    st.markdown("**Cited repository evidence** (immutable refs: path, line range, commit SHA)")

    for citation in citations:
        st.code(
            (
                f"{citation['path']}:"
                f"{citation['start_line']}-{citation['end_line']}"
                f"@{citation['commit_sha'][:8]}  "
                f"({citation['symbol']})"
            ),
            language="text",
        )


def policy_tab(task: dict[str, Any]) -> None:
    decisions = task.get("policy", [])

    if not decisions:
        st.info("No policy decisions recorded yet.")
        return

    icons = {
        "allow": "✅",
        "block": "⛔",
        "escalate": "⚠️",
    }

    rows = [
        {
            "": icons.get(decision["decision"], "⚪"),
            "rule": decision["rule_id"],
            "decision": decision["decision"],
            "rationale": decision["rationale"],
            "actor": decision["actor"],
        }
        for decision in decisions
    ]

    render_table(rows)


def checks_tab(task: dict[str, Any]) -> None:
    checks = task.get("checks", [])

    if not checks:
        st.info("No sandbox checks were run (blocked or failed before validation).")
        return

    rows = [
        {
            "": "✅" if check["passed"] else "❌",
            "check": check["check_type"],
            "exit": check["exit_code"],
            "seconds": check["duration_s"],
            "summary": check["summary"],
            "output hash": check["stdout_hash"],
        }
        for check in checks
    ]

    render_table(rows)

    for check in checks:
        passed = check["passed"]
        command_tail = " ".join(check["command"][-3:])

        with st.expander(f"{'✅' if passed else '❌'} {check['check_type']}: {command_tail}"):
            st.code(
                check["output_tail"] or "(no output)",
                language="text",
            )


def patch_tab(task: dict[str, Any]) -> None:
    attempts = task.get("attempt_log", [])

    if attempts:
        rows = [
            {
                "attempt": attempt["attempt_no"],
                "files": ", ".join(attempt["changed_files"]) or "-",
                "changed lines": attempt["changed_lines"],
                "confidence": attempt["confidence"],
                "error": attempt["error"] or "",
            }
            for attempt in attempts
        ]
        render_table(rows)

    patch = task.get("patch")

    if patch:
        st.code(patch, language="diff")
        st.download_button(
            "⬇️ Download patch.diff",
            patch,
            file_name=f"{task['task_id']}.diff",
        )
    else:
        st.info("No patch was produced for this task.")


def pr_tab(task: dict[str, Any]) -> None:
    pr_url = task.get("pr_url")

    if pr_url:
        if pr_url.startswith("file://"):
            st.info("Dry-run mode: the PR payload was written to disk instead of GitHub.")
            st.code(pr_url, language="text")
        else:
            st.link_button("Open pull request", pr_url)

    pr_body = task.get("pr_body")

    if pr_body:
        st.markdown(pr_body)
    else:
        st.info("PR evidence appears once all required checks are green.")


def audit_tab(task: dict[str, Any]) -> None:
    events = task.get("audit", [])

    if not events:
        st.info("No audit events yet.")
        return

    st.caption("Each event hashes its predecessor; the API re-verifies the chain on every read.")

    rows = [
        {
            "#": event["seq"],
            "event": event["event_type"],
            "actor": event["actor"],
            "hash": event["event_hash"],
            "time": event["timestamp"][11:19],
        }
        for event in events
    ]

    render_table(rows)

    with st.expander("Event payloads"):
        st.json(events)


def approval_panel(
    task: dict[str, Any],
    key_prefix: str,
) -> None:
    escalations = [decision for decision in task.get("policy", []) if decision["decision"] == "escalate"]

    reason = "; ".join(decision["rationale"] for decision in escalations) or "policy escalation"

    st.markdown(f"**Why approval is required:** {reason}")

    approve_column, reject_column = st.columns(2)

    if approve_column.button(
        "✅ Approve and open PR",
        key=f"{key_prefix}_approve",
        type="primary",
    ):
        submit_decision(task, approve=True)

    if reject_column.button(
        "❌ Reject",
        key=f"{key_prefix}_reject",
        type="secondary",
    ):
        submit_decision(task, approve=False)


def submit_decision(
    task: dict[str, Any],
    *,
    approve: bool,
) -> None:
    reviewer = st.session_state["reviewer"] or "reviewer"

    ok, result = api(
        "POST",
        f"/tasks/{task['task_id']}/decision",
        {
            "approve": approve,
            "reviewer": reviewer,
        },
    )

    if ok:
        st.rerun(scope="app")
    else:
        st.error(result)


def task_view(
    task_id: str,
    *,
    poll: bool = False,
) -> None:
    ok, result = fetch_task(task_id)

    if not ok:
        st.error(result)
        return

    task = result

    st.markdown(f"### {task['issue_title']}")
    st.caption(
        f"`{task['task_id']}` · repo `{task['repository']}`"
        + (f" · issue {task['issue_ref']}" if task.get("issue_ref") else "")
    )

    st.markdown(badge(task["state"]))
    outcome_banner(task)
    metrics_row(task)
    pipeline_view(task)

    if task["state"] == "AWAIT_APPROVAL":
        st.divider()
        approval_panel(task, "task")

    st.divider()

    tabs = st.tabs(
        [
            "🧠 Plan & evidence",
            "🛡️ Policy",
            "🧪 Checks",
            "🧩 Patch",
            "📄 PR evidence",
            "🧾 Audit",
            "Raw",
        ]
    )

    with tabs[0]:
        plan_tab(task)

    with tabs[1]:
        policy_tab(task)

    with tabs[2]:
        checks_tab(task)

    with tabs[3]:
        patch_tab(task)

    with tabs[4]:
        pr_tab(task)

    with tabs[5]:
        audit_tab(task)

    with tabs[6]:
        st.json({key: value for key, value in task.items() if key not in {"audit", "patch", "checks"}})

    # A polling fragment detected a terminal transition. Leave fragment mode
    # so approval controls and full-page navigation render correctly.
    if poll and task["state"] in TERMINAL_STATES:
        st.rerun(scope="app")


# --------------------------------------------------------------------- sidebar
def render_sidebar() -> None:
    with st.sidebar:
        st.title("🛠️ PatchPilot")
        st.radio("Navigate", PAGES, key="page")
        st.divider()

        ok, health = api("GET", "/health")

        if not ok:
            st.error(health)
        else:
            st.success("Control plane online")

            if not health.get("llm_ready", False):
                st.error(
                    "LLM provider is not configured in the API process. "
                    "Set `GROQ_API_KEY` and ensure `llm_factory` is provided."
                )

            sandbox = health.get("sandbox", "unknown")

            if sandbox == "local":
                st.warning(
                    "Sandbox: **local** (development only, not "
                    "network-isolated). Start the API with "
                    "`PATCHPILOT_SANDBOX=docker` for untrusted repositories."
                )
            else:
                st.info(f"Sandbox: **{sandbox}** · LLM: **{health.get('provider', 'unknown')}**")

        st.text_input("API base URL", key="api_base")
        st.text_input("Reviewer name", key="reviewer")
        st.toggle("Live auto-refresh", key="auto_refresh")

        if st.button("🔄 Refresh now"):
            st.rerun(scope="app")

        st.divider()
        st.caption(
            "No auto-merge · protected paths blocked · ephemeral sandbox · "
            "allowlisted commands · bounded repair · human approval for "
            "risky changes · tamper-evident audit."
        )


# ------------------------------------------------------------------- dashboard
@st.fragment(run_every=POLL_SECONDS)
def dashboard_body() -> None:
    tasks = fetch_tasks()

    if not tasks:
        st.info("No tasks yet. Use **Submit issue**, or send a GitHub webhook.")
        return

    columns = st.columns(5)

    columns[0].metric("Tasks", len(tasks))
    columns[1].metric(
        "Running",
        sum(bool(task.get("running")) for task in tasks),
    )
    columns[2].metric(
        "Awaiting approval",
        count_states(tasks, "AWAIT_APPROVAL"),
    )
    columns[3].metric(
        "PRs ready",
        count_states(tasks, "AWAIT_REVIEW", "MERGED"),
    )
    columns[4].metric(
        "Blocked / failed",
        count_states(tasks, *FAILED_STATES),
    )

    rows = [
        {
            "task": short_id(task["task_id"]),
            "repo": task["repository"],
            "issue": task["issue_title"],
            "state": (f"{STATE_META.get(task['state'], ('', '', ''))[0]} {task['state']}"),
            "risk": task.get("risk", "-"),
            "attempts": task.get("attempts", 0),
            "seconds": task.get("duration_s", 0),
            "created": task.get("created_at", "")[11:19],
        }
        for task in tasks
    ]

    render_table(rows)


def dashboard_page() -> None:
    if st.session_state["auto_refresh"]:
        dashboard_body()
    else:
        dashboard_body()

    tasks = fetch_tasks()

    if not tasks:
        return

    options = task_ids(tasks)

    selected_task = st.selectbox(
        "Open a task",
        options,
        format_func=lambda task_id: format_task_choice(task_id, tasks),
    )

    st.button(
        "Open task run →",
        on_click=goto,
        args=(P_TASK, selected_task),
        type="primary",
    )


# ---------------------------------------------------------------------- submit
def submit_page() -> None:
    st.subheader("Submit an issue")
    st.markdown(
        "PatchPilot indexes the repository, retrieves cited evidence, "
        "plans, patches, enforces policy, validates in the sandbox and "
        "opens a PR (dry-run unless a GitHub client is wired in)."
    )

    bug_column, blocked_column, _ = st.columns([1, 1, 2])

    bug_column.button(
        "🐞 Load demo: seeded bug",
        on_click=prefill_demo,
        args=("bug",),
        help="Expected: green checks, PR ready.",
    )

    blocked_column.button(
        "🚫 Load demo: protected path",
        on_click=prefill_demo,
        args=("blocked",),
        help="Expected: blocked at policy check.",
    )

    ok, repositories = api("GET", "/repos")
    repo_options = repositories if ok and isinstance(repositories, list) else []

    if not repo_options:
        st.warning(
            "No allowlisted repositories. Start the API from the project "
            "root, or set:\n\n"
            '`PATCHPILOT_REPOS=\'{"demo": "/absolute/path/to/repo"}\'`'
        )

    with st.form("submit_issue_form"):
        if repo_options:
            repository = st.selectbox(
                "Repository (allowlisted)",
                repo_options,
            )
        else:
            repository = st.text_input("Repository")

        title = st.text_input("Issue title", key="f_title")
        body = st.text_area(
            "Issue description / acceptance criteria",
            key="f_body",
            height=140,
        )

        ref_column, sha_column = st.columns(2)

        issue_ref = ref_column.text_input(
            "Issue reference (optional)",
            placeholder="#42",
        )

        base_sha = sha_column.text_input(
            "Base SHA (optional)",
            placeholder="pins retrieval/provenance to this commit",
        )

        submitted = st.form_submit_button(
            "🚀 Create task",
            type="primary",
        )

    if not submitted:
        return

    if not repository or not title.strip():
        st.error("Repository and issue title are required.")
        return

    payload: dict[str, Any] = {
        "repository": repository,
        "issue_title": title.strip(),
        "issue_body": body.strip(),
    }

    if issue_ref.strip():
        payload["issue_ref"] = issue_ref.strip()

    if base_sha.strip():
        payload["base_sha"] = base_sha.strip()

    ok, result = api("POST", "/tasks", payload)

    if not ok:
        st.error(result)
        return

    goto(P_TASK, result["task_id"])
    st.rerun(scope="app")


# -------------------------------------------------------------------- task run
def task_page() -> None:
    tasks = fetch_tasks()
    ids = task_ids(tasks)
    selected = st.session_state["sel_task"]

    if selected and selected not in ids:
        ids.insert(0, selected)

    if not ids:
        st.info("No tasks yet. Create one from **Submit issue**.")
        return

    choice = st.selectbox(
        "Task",
        ids,
        index=ids.index(selected) if selected in ids else 0,
        format_func=lambda task_id: format_task_choice(task_id, tasks),
    )

    st.session_state["sel_task"] = choice

    ok, probe = fetch_task(choice)

    if not ok:
        st.error(probe)
        return

    is_running = bool(probe.get("running")) and probe["state"] not in TERMINAL_STATES

    if is_running and st.session_state["auto_refresh"]:
        polling_task_view = st.fragment(run_every=POLL_SECONDS)(task_view)
        polling_task_view(choice, poll=True)
    else:
        task_view(choice, poll=False)


# ------------------------------------------------------------------- approvals
def approvals_page() -> None:
    st.subheader("Human approval queue")
    st.caption(
        "Medium/high risk, large changes, or negative coverage delta stop "
        "here. Approval is recorded in the audit trail before a PR is opened."
    )

    tasks = fetch_tasks()
    pending = [task for task in tasks if task["state"] == "AWAIT_APPROVAL"]

    if not pending:
        st.success("Nothing waiting for approval.")
        return

    for brief in pending:
        ok, result = fetch_task(brief["task_id"])

        if not ok:
            st.error(result)
            continue

        task = result

        with st.expander(
            f"⏸️ {short_id(task['task_id'])} · {task['issue_title']} · risk {task.get('risk', '-')}",
            expanded=True,
        ):
            metrics_row(task)
            policy_tab(task)
            checks_tab(task)
            patch_tab(task)
            st.divider()
            approval_panel(task, f"queue_{task['task_id']}")

            st.button(
                "Open full run →",
                key=f"open_{task['task_id']}",
                on_click=goto,
                args=(P_TASK, task["task_id"]),
            )


# ------------------------------------------------------------------------ main
def main() -> None:
    render_sidebar()

    st.title("🛠️ PatchPilot Control Plane")
    st.caption("The LLM proposes; deterministic policy, sandboxing, tests and human approval decide.")

    page = st.session_state["page"]

    if page == P_DASH:
        dashboard_page()
    elif page == P_SUBMIT:
        submit_page()
    elif page == P_TASK:
        task_page()
    else:
        approvals_page()


main()
