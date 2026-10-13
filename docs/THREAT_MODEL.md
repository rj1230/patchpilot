# Threat model (v1)

| Threat | Control | Where enforced | Test |
|---|---|---|---|
| Prompt injection in repo/issue text | Evidence tagged `<untrusted_evidence>`; injection scan logged; **permissions never come from model text** | `agents/prompts.py`, `domain/policy.py` | `test_prompt_injection_cannot_widen_scope` |
| Edit protected paths (CI, infra, auth, migrations, secrets) | Glob policy checked on plan, on proposed edit paths (before apply) and on the final diff | `PolicyEngine.check_scope` | `test_protected_path_in_plan_is_blocked` |
| Path traversal in patch | Resolved-path containment check | `agents/patching.py` | `test_path_traversal_rejected_then_blocked` |
| Secret exfiltration via diff | Secret regex gate on added lines; env scrubbed for child processes | `check_added_text`, `_scrubbed_env` | `test_secret_in_patch_blocked`, `test_secrets_not_leaked_to_children` |
| Arbitrary command execution | Model supplies a check *name*; argv comes from an allowlist | `sandbox/runner.py` | `test_arbitrary_command_rejected` |
| Runaway loops / cost | Attempts, runtime, LLM-call and diff-size budgets | `check_budget`, `check_scope` | `test_attempt_budget_exhausted_blocks_with_report` |
| Skipped/missing scanner treated as pass | Exit 127/124 are terminal blocks | `agents/workflow.py` | `test_missing_tool_is_failure_not_pass` |
| Forged webhooks / replays | HMAC-SHA256 constant-time verification, delivery-ID dedup, repo allowlist | `integrations/github.py`, `api/app.py` | `test_api_webhook_to_pr_and_dedup` |
| Audit tampering | Hash-chained events; `patchpilot verify-audit` | `domain/audit.py` | `test_audit_chain_detects_tampering` |
| Auto-merge | No merge code path exists; state machine has no route from patching to merge | `domain/state.py` | `test_policy_block_cannot_reach_pr` |

## Known limitations (honest list)
- `LocalBackend` is resource-limited and env-scrubbed but **not network- or filesystem-isolated**. Use `--docker` (DockerBackend, `--network none`, read-only, non-root, cap-drop ALL) for untrusted repos. The Docker path is built and its argv unit-tested; it has not been exercised against a live daemon in this build.
- Retrieval uses BM25 + feature-hashing vectors in memory. pgvector and a real embedding model are the production swap.
- Chunking uses Python `ast`; tree-sitter is needed for multi-language support.
- The GitHub client is tested against a fake transport only; GitHub App JWT/installation-token minting is not implemented (supply an installation token).
- The benchmark ships 3 scripted cases to prove the harness. The blueprint's 20-50 task corpus and real-model numbers still need to be produced by you.
- Dashboard (Streamlit), OpenTelemetry/Prometheus, Postgres/Redis queue and Temporal are not built; the API runs tasks in-process background tasks and keeps state in memory.
