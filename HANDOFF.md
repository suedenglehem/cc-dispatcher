# HANDOFF — Local Coding Agent Dispatcher

## Goal
Build the dispatcher per `local-agent-dispatcher-prd.md` (plan: `~/.claude/plans/partitioned-wibbling-flamingo.md`).
Phase 1 = core daemon + ladd CLI, end-to-end on a scratch demo repo.

## Phase status
- P1 core code: **complete** (all modules import-verified, CLIs run)
- P1 tests + e2e demo: **in progress**
- P2 (MCP + primary UX), P3 (recovery/memory/failure injection, all 20 acceptance tests), P4 (i7/Cline/hardening): not started

## Key decisions (final, per plan)
- Worker permission: `bypassPermissions` + strict `--allowedTools` allowlist
- Integration: explicit `ladd integrate <job> --commit <sha>` (primary is the authority)
- `invalid_result`: no auto-retry (needs `ladd resume` decision)
- 8th MCP tool `dispatch_integrate` added (PRD list was "proposed")
- Deadline = total job budget from first spawn; no attempt start with <60s left
- SIGTERM: stop scheduler, leave workers running (re-adopted on restart)
- Runtime `ladd backend ...` changes live only; config reasserts on next start (safety property)
- API wire: newline-JSON over Unix socket (0600); redaction replaces raw + escaped credential variants; 8k cap raises actionable error instead of truncating

## Verified environment (2026-10-16)
- tr4-primary `:8091` (vLLM qwen3.8-27b-fp8) = THIS session, `is_primary`, never worker-scheduled
- tr4-secondary `:8080` (llama.cpp `swift-1.5-iq3_xxs`, claude label `qwen`) = Cline interactive → `manually_reserved`
- i7 `192.168.0.100:8080` (llama.cpp `local-metrics`, alias `qwen3.8-27b`) = 2nd Claude → `enabled=false`
- claude 2.1.301; cline 3.0.56; no turn-limit flag (bounded by deadline+context, documented in packet LIMITS)
- **Never read/echo `~/.cline/data`** (provider secret). Never touch the two interactive sessions.

## Changed files (this phase)
- `local_dispatcher/` full package: config, db(+migrations/0001_init.sql), models, procfs, worktrees,
  backends, packets, results, supervisor, jobs, scheduler (launch gate + startup reconcile),
  api (ApiServer/ApiClient), adapters/{base,claude,cline}, daemon (flock+signals), cli (ladd),
  __main__, mcp_adapter (P1 stub)
- `prompts/` — PRD §17/§18/§19 verbatim + §20 packet template + result-schema.json
- `config.example.toml` (loads+validates; mirrors verified topology)
- `systemd/local-dispatcher.service`, `install.sh`, `pyproject.toml`, `.gitignore`

## Test results
- All P1 modules import OK; `ladd --help`, `local-dispatcher --help` verified
- `config.example.toml` parses to correct topology (3 backends, group tr4=1, cc-worker profile)
- pytest suite: **not yet written** (next step)

## Next steps
1. `tests/conftest.py` (temp state/config/fake backends/repo fixture/in-proc daemon/ApiClient)
2. `tests/fakes/fake_worker.py` (modes: normal/slow/hang/checkpoint_then_crash/overflow/invalid_report/scope_violation/tests_fail/echo_packet; atomic launch counter) + `fake_backend.py`
3. Unit tests (ctx derivation ×3 probe shapes, packet renderer, result validator, migrations)
4. Acceptance tests a01–a08, a11–a13, a17–a20 (PRD §14)
5. P1 exit: scratch `demo-repo` e2e via `ladd` with fake worker; then real claude worker (gated on a freed backend — both currently busy; ask user before live smoke)
6. Commit per phase with test results
