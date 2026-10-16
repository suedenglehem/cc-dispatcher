# HANDOFF — Local Coding Agent Dispatcher

## Goal
Build the dispatcher per `local-agent-dispatcher-prd.md` (plan: `~/.claude/plans/partitioned-wibbling-flamingo.md`).
Phase 1 = core daemon + ladd CLI, end-to-end on a scratch demo repo.

## Phase status
- P1 core code: **complete** (all modules import-verified, CLIs run)
- P1 tests: **complete** — 64 passed, 4 skipped (a10/a14/a15/a16 are P3 stubs)
- P1 e2e demo on scratch `demo-repo` via `ladd`: **not yet run** (see next steps)
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
- pytest: **64 passed, 4 skipped in ~51s** (skips = a10 restart-no-dup, a14/a15/a16 mycelium — all P3)
  - Acceptance a01–a09, a11–a13, a17–a20 (18): green with fakes (in-proc daemon, fake backends/workers)
  - Unit (46): ctx extraction ×3 probe shapes, packet renderer, result validator + evaluate_attempt
    decision table + recovery-context bounding, migrations, event pruning, procfs identity/liveness,
    API redaction + 8k cap
- Bugs found/fixed by tests: scheduler missing `starting`→`running` transition (a05 blocker);
  `re.search` flags on compiled regex in claude adapter; procfs `pgid_of` off-by-one (read ppid,
  now pgrp field 5→idx 2) + same fix in `group_alive` member scan

## Next steps
1. P1 exit: scratch `demo-repo` e2e via `ladd` with fake worker (git init under `dispatcher/demo-repo`);
   then real claude worker smoke — **gated on a freed backend** (both currently busy; ask user before live smoke)
2. P2: api.py polish, mcp_adapter 8 real tools (`dispatch_*` + `dispatch_integrate`), `claude mcp add dispatcher`,
   prompts shipped, drain/cancel/pause/resume/idempotency polish, docs/commands.md (§21)
3. P3: stream-json usage tracking + 65% warning, checkpoint contract e2e, procfs startup reconcile + quarantine
   (a10 real dispatcherd SIGKILL), mycelium client + packet injection + envelope + outage degrade + promote
   (a14/a15/a16 real `mycelium serve` under `MYCELIUM_CONFIG`→temp store) → ALL 20 acceptance green
4. P4: i7 enable (1-token `ladd backend check` label-case), resource-group demo, Cline smoke → pass or
   docs/cline-status.md (never faked), full docs + install.sh
