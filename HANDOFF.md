# HANDOFF — Local Coding Agent Dispatcher

## Goal
Build the dispatcher per `local-agent-dispatcher-prd.md` (plan: `~/.claude/plans/partitioned-wibbling-flamingo.md`).
Deterministic local orchestration: the primary Claude session switches single/team mode; in team mode
independent tasks run as supervised headless workers on spare inference backends. Capacity/isolation/
durability/recovery enforced **in code** (scheduler, SQLite, subprocess supervisor, git worktrees);
agents do the reasoning.

## Phase status
- P1 (core daemon + ladd CLI, e2e on scratch `demo-repo`): **complete**
- P1 real-model smoke (user freed backends 2026-10-17): **complete** — `j_7e53e663b7` (tr4-secondary,
  real claude worker) succeeded ~97s: valid result.json + checkpoint + diff.patch, commit c78a36d, scope clean;
  i7 label-case verified (`local-metrics` served under label `qwen3.8-27b`)
- P2 (MCP + primary UX): **complete** — mcp_adapter 8 tools done+tested, `claude mcp add dispatcher` done,
  full primary↔worker loop demonstrated live (submit → worker runs → primary ff-merges → `ladd integrate`
  accepted → drain to single). Backends restored to safe state. Suite 72 passed / 4 P3-skip.
- P3 (recovery/memory/failure injection; all 20 acceptance tests), P4 (Cline/hardening): **not started**

## Worker death 143 (external SIGTERM) — SUSPENDED per user 2026-10-17
- Symptom: 4 daemon-spawned workers on the OLD daemon (pid 16445) died exit 143 (SIGTERM, class=crash),
  attempt 1 only, ~26 min apart, on BOTH backends. **Zero deaths on the NEW daemon (pid 43684) across 5+
  instrumented (strace-wrapped) + real runs** — killer not currently reproducible.
- Exonerated with evidence: dispatcher daemon (all 4 of its SIGTERM paths require a status/log marker —
  `cancelled` / `deadline_exceeded` / `orphan_killed` / `re_adopted` — none present on the deaths);
  systemd-oomd + kernel OOM (they send SIGKILL=137, not SIGTERM=143; zero kill events in journal);
  a process I started (only daemon + workers exist); Cline hub daemon (still running, no deaths on the new
  daemon; user confirmed nothing in Cline capable of killing was run).
- Conclusion: external userspace SIGTERM tied to the old daemon's lifetime / a transient process active
  00:07–00:33 UTC that has since exited. **User: "stop digging till it happens (if it ever happens)."**
- **Re-arm if it recurs:** wrapper `/tmp/ladd-real/claude-wrap.sh` (strace-as-parent, yama-safe) is kept;
  re-add a `cc-worker-strace` profile to `~/.config/local-dispatcher/config.toml` pointing at it, submit on
  the affected backend, read `si_pid`/`si_code` from the wrap log. Daemon logs kept at `/tmp/ladd-real/daemon.log`
  (old, has the deaths) + `daemon2.log` (new).

## Known footgun (harden in P3)
- `scheduler.startup_reconcile` scans ALL of `/proc/*/environ` for the `DISPATCHER_ATTEMPT_ID` marker and
  kills any unmatched marker process group. Two dispatcher instances with DIFFERENT state dirs (different
  DBs) would each see the other's workers as orphans and kill them. Scope the orphan-kill to the instance's
  own db/lock (flock already guarantees one per state dir, but the marker scan is global). Not the 143 cause
  (no second instance was running) — a latent safety issue for future multi-instance use.

## Key decisions (final, per plan)
- Worker permission: `bypassPermissions` + strict `--allowedTools` allowlist
- Integration: explicit `ladd integrate <job> --commit <sha>` (primary is the authority)
- `invalid_result`: no auto-retry (needs `ladd resume` decision)
- 8th MCP tool `dispatch_integrate` added (PRD list was "proposed")
- Deadline = total job budget from first spawn; no attempt start with <60s left
- SIGTERM: stop scheduler, leave workers running (re-adopted on restart)
- Runtime `ladd backend ...` changes live only; config reasserts on next start (safety property)
- API wire: newline-JSON over Unix socket (0600); redaction replaces raw + escaped credential variants;
  8k cap raises an actionable error instead of truncating

## Verified environment (2026-10-16/17)
- tr4-primary `:8091` = vLLM (qwen3.8-27b-fp8, ctx 262144) = THIS primary session, `is_primary`,
  never worker-scheduled
- tr4-secondary `:8080` = strata custom Python serving stack (`/dd2/strata/serve/server.py`, serves
  `swift-1.5-iq3_xxs`, claude label `qwen`, ctx_max 83491) = Cline interactive → `manually_reserved`
  (RESTORED to reserved after the 10-17 demo)
- i7 `192.168.0.100:8080` (llama.cpp `local-metrics`, label `qwen3.8-27b`, ctx_max 128563) = 2nd Claude →
  `enabled=false` in config (RESTORED to disabled after the 10-17 demo)
- claude **2.1.302** (needs `--verbose` for stream-json; vLLM rejects default effort `high` → `--effort xhigh`)
- **Never read/echo `~/.cline/data`** (provider secret). Never touch the two interactive sessions.
- **Post-restart ops:** config reasserts on daemon start — after ANY restart, re-release tr4-secondary and
  re-enable i7 if workers should run on them. `pgrep -af local-dispatcher` finds the daemon (not `dispatcherd`).

## Changed files (P1 + P2)
- `local_dispatcher/` full package: config, db(+migrations/0001_init.sql), models, procfs, worktrees,
  backends, packets, results, supervisor, jobs, scheduler (launch gate + startup reconcile),
  api (ApiServer/ApiClient), adapters/{base,claude,cline}, daemon (flock+signals), cli (ladd), __main__,
  **mcp_adapter (P2: 8 tools, stateless stdio→socket forwarder)**
- `prompts/` — PRD §17/§18/§19 verbatim + §20 packet template + result-schema.json
- `config.example.toml`, `systemd/local-dispatcher.service`, `install.sh`, `pyproject.toml`, `.gitignore`
- P2 new: `tests/test_mcp_adapter.py` (8 tools), `docs/commands.md` (§21 text)
- P2 bug fixes: jobs.py PROFILE/BACKEND_UNKNOWN `known=` now serializes ids (was leaking live pydantic
  objects into ApiError → json.dumps crash in `_send`, dropped the connection with no job row); cli
  backend-check workers path; adapters/claude.py `--verbose` + effort handling; api.py redaction + 8k cap

## Test results
- pytest: **72 passed, 4 skipped** (~55s) — skips = a10 (real dispatcherd restart) + a14/a15/a16 (real
  mycelium subprocess) — all P3-gated
- Acceptance a01–a09, a11–a13, a17–a20 (18): green with fakes; P2 mcp_adapter (8): green
- Unit (46): ctx extraction ×3 probe shapes, packet renderer, result validator + evaluate_attempt decision
  table + recovery-context bounding, migrations, event pruning, procfs identity/liveness, API redaction +
  8k cap + submit-error JSON-serializability (2 regression tests for the ProfileConfig/BackendConfig leak)

## Demo (scratch `demo-repo`, real backends, 2026-10-17) — COMPLETE
- Team mode, real claude worker on tr4-secondary: `j_7e53e663b7` (multiply) succeeded → primary ff-merged
  dispatch/j_7e53e663b7 (c78a36d) to main → `ladd integrate j_7e53e663b7 --commit c78a36d` →
  `integration_disposition=accepted` → drained to single (policy=drain). Backends restored
  (tr4-secondary RESERVED, i7 disabled).
- Several additional diagnostic jobs (add/mul/sub) succeeded on the new daemon (exit 0); left un-merged
  (diagnostic only). A few earlier diagnostic jobs failed/blocked (143 SIGTERM, exit=1 fast-fail) on the
  old daemon — superseded; see the 143 section.

## Next steps
1. **P2 commit** (immediate): all P2 files above; message = 72 passed/4 P3-skip + real-smoke findings
   (missing `--verbose`, effort `high`→`--effort xhigh`, backend-check workers bug, ProfileConfig/BackendConfig
   API-crash bug + 2 regression tests, 143 SUSPENDED).
2. P3: stream-json usage tracking + 65% warning, checkpoint/overflow recovery, procfs startup reconcile +
   quarantine (a10 real dispatcherd SIGKILL; also scope startup_reconcile orphan-kill to own instance — see
   Known footgun), mycelium client + packet injection + envelope + outage degrade + promote (a14/a15/a16 real
   `mycelium serve` under `MYCELIUM_CONFIG`→temp store) → ALL 20 acceptance green.
3. P4: Cline smoke → pass or `docs/cline-status.md` (never faked), resource-group demo, full docs + install.sh.
