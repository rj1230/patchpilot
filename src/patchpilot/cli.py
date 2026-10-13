"""CLI: `patchpilot run`, `patchpilot bench`, `patchpilot verify-audit`."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .agents.llm import GroqProvider
from .agents.workflow import Workflow
from .domain.audit import verify_chain
from .domain.models import AuditEvent, Task
from .domain.policy import PolicyEngine, load_policy
from .integrations.github import DryRunPRClient
from .sandbox.runner import DockerBackend, Sandbox


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="patchpilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run PatchPilot on a local repo (needs GROQ_API_KEY)")
    r.add_argument("--repo", required=True)
    r.add_argument("--title", required=True)
    r.add_argument("--body", default="")
    r.add_argument("--policy", default="policies/default.yaml")
    r.add_argument("--artifacts", default="artifacts")
    r.add_argument(
        "--model", default=None, help="Groq model id (default: $GROQ_MODEL or openai/gpt-oss-120b)"
    )
    r.add_argument(
        "--docker",
        action="store_true",
        help="use the Docker sandbox backend (recommended for untrusted repos)",
    )
    b = sub.add_parser("bench", help="run the scripted demo benchmark (no API key needed)")
    b.add_argument("--policy", default="policies/default.yaml")
    b.add_argument("--out", default="artifacts/bench.md")
    v = sub.add_parser("verify-audit", help="verify an audit.jsonl hash chain")
    v.add_argument("file")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        repo = Path(a.repo).resolve()
        wf = Workflow(
            GroqProvider(a.model),
            PolicyEngine(load_policy(a.policy)),
            Sandbox(DockerBackend() if a.docker else None),
            Path(a.artifacts),
            DryRunPRClient(Path(a.artifacts) / "prs"),
        )
        res = wf.run(Task(repository=repo.name, repo_path=str(repo), issue_title=a.title, issue_body=a.body))
        print(json.dumps(res.summary(), indent=2))
        return 0 if res.state.value in ("AWAIT_REVIEW", "AWAIT_APPROVAL") else 2
    if a.cmd == "bench":
        from .evaluation.bench import run_case, summarize, to_markdown
        from .evaluation.demo_cases import demo_cases

        with tempfile.TemporaryDirectory() as td:
            outs = [run_case(c, Path(a.policy), Path(td))[0] for c in demo_cases()]
        s = summarize(outs)
        md = to_markdown(outs, s)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(md)
        print(md)
        return 0 if s["task_resolution_rate"] == 1.0 and s["correct_block_rate"] == 1.0 else 1
    if a.cmd == "verify-audit":
        evs = [
            AuditEvent.model_validate_json(ln) for ln in Path(a.file).read_text().splitlines() if ln.strip()
        ]
        ok = verify_chain(evs)
        print("audit chain OK" if ok else "audit chain BROKEN")
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
