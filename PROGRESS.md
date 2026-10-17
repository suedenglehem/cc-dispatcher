# PROGRESS — Local Coding Agent Dispatcher

- Repo: `/dd2/andrei/claude/dispatcher` → `git@github.com:suedenglehem/cc-dispatcher.git`
- Spec: `local-agent-dispatcher-prd.md` (§5–15 binding; §17–21 shipped verbatim as `prompts/`).
- Plan: `~/.claude/plans/partitioned-wibbling-flamingo.md`. Working state: `HANDOFF.md`.
- What it is: a deterministic local orchestration service (single `dispatcherd` asyncio process)
  that lets the primary Claude session switch **single/team** mode; in team mode independent tasks
  run as supervised headless worker agents on spare inference backends. Capacity/isolation/
  durability/recovery are enforced **in code**; agents do the reasoning.

## Milestones

| Phase | Scope | Status |
|---|---|---|
| P1 | Core daemon + `ladd` CLI; e2e on scratch `demo-repo` | ✅ done |
| P1 | Real-model smoke (real claude worker on a freed backend) | ✅ done |
| P2 | MCP adapter (8 tools) + primary UX; live primary↔worker demo | ✅ done (`b3a1ef6`) |
| P3 | Recovery, restart reconcile, mycelium memory, failure injection → 20/20 acceptance | 🔶 in progress |
| P4 | Cline adapter, resource-group demo, hardening, full docs + install | ⬜ not started |

## Acceptance tests (PRD §14)
- **Green (19):** a01–a13, a17–a20 — a10 now with a REAL `dispatcherd` subprocess
  (SIGKILL mid-job → restart → adopt, no duplicate, capacity reconciled, flock negative control)
- **P3-gated (3, need real `mycelium serve` under `MYCELIUM_CONFIG` → temp store):** a14/a15/a16
- Non-acceptance P3: `test_i_context_warning.py` (2) + `test_i_crash_recovery.py` (4), green
- Suite: **86 passed, 3 skipped** (skips = the 3 P3-gated above)

## Current milestone: P3 (recovery / restart / memory / failure injection)
Goal: all 20 acceptance tests green, using real subprocesses.
Status: **step 1 done (2026-10-17)** — stream-json usage tracking was already wired
(`adapter.parse_line` → `StreamInfo.last_context_tokens`); this step made it observable and
configurable: `results.context_pct()` helper, one-shot per-attempt `context_warning` event from
the tick (`Scheduler._context_watch`, deduped via `Child.context_warned`), `daemon.context_warn_pct`
and `daemon.context_overflow_usage_pct` now actually drive the warning and the overflow classifier
(was hardcoded 0.95), live `context_pct`/`context_warning` in the `job` view for running attempts,
durable `usage_json` = `{last_context_tokens, ctx_limit, context_pct, num_turns, session_id}`.
New tests: `test_u_ctx.py` (+7 unit: context_pct math/boundary/unknown, classify threshold),
`test_i_context_warning.py` (+2 integration: live view + one-shot event + durable evidence;
below-threshold negative). Fake worker gained `high_context` mode (+`FAKE_USAGE_TOKENS`,
`FAKE_CTX_HOLD_S`).
**Step 2 done (2026-10-17)** — recovery e2e for the previously-undriven fake modes,
`tests/test_i_crash_recovery.py` (4): `crash_once` → `crash` → retry in the SAME worktree
with an UNCERTAIN recovery context (`uncertain=true`, no checkpoint) → attempt 2 succeeds;
`checkpoint_then_crash` → attempt 2 gets a CERTAIN context (`uncertain=false`,
`checkpoint.status=in_progress`) → succeeds; new `backend_down` mode → `backend_disconnect`
(API-error text pattern) → retried like a crash → succeeds; `tests_fail` → `tests_failed`,
non-retryable (`allow_test_repair=0`) → job failed on attempt 1, result preserved. The
profile toggle `FAKE_ECHO_PACKET` makes attempt 2 dump its received packet, so the tests
assert the recovery section (checkpoint / "NO CHECKPOINT WAS SAVED" + `from_attempt`)
reached the worker's STDIN, not just the DB row.
Remaining:
1. ~~stream-json usage tracking + 65% context warning + overflow evidence~~ ✅
2. ~~checkpoint contract + context-overflow / crash / backend-disconnect recovery~~ ✅
   (a09 = overflow; step 2 = crash / checkpoint-then-crash / backend-disconnect / tests-fail)
3. ~~procfs startup reconcile + quarantine — a10: real `dispatcherd` SIGKILL mid-job → restart →
   adopt (no duplicate), capacity reconciled~~ ✅ **done (2026-10-17)** —
   `tests/test_a10_restart_no_dup.py` rewritten as a real-subprocess test: daemon #1 launches a
   `hold`-mode fake worker, a flock negative control (second daemon must exit 1), daemon #1
   SIGKILLed (worker survives — own session), daemon #2 on the SAME state dir adopts via
   `startup_reconcile` (same pid, `re_adopted` event), the adopted worker finishes after its
   supervisor is gone → on-disk result file is the authority (`exit_code=None` path in
   `_finalize`, the fix this step) → job succeeds on the SAME attempt; launch log (FAKE_LAUNCH_LOG)
   is the no-duplication oracle (exactly one start/end pair, stable pid); reservations released +
   a second job proves the freed slot works. Two bugs found + fixed along the way: (a)
   re-adopted children finalized as terminal `exit_unknown` despite a valid result — `_finalize`
   now judges by the on-disk report (mirrors `_finalize_dead_attempt`), and `exit_unknown` is
   retryable (crash-like; the process is confirmed dead before finalize so a retry cannot
   duplicate); (b) `scan_marker_processes` read field 4 (ppid) instead of field 5 (pgrp) — after
   the daemon died the worker re-parents to init (ppid=1), so every restart-adoptation looked
   like PID reuse and was quarantined. Now uses `pgid_of` (field 5), which is stable across
   re-parenting because the worker is its own group leader.
4. mycelium client + packet memory injection + envelope + outage degrade + promote
   (a14/a15/a16; real `mycelium serve` under a temp store, user store untouched)
5. Hardening: scope `startup_reconcile` orphan-kill to the instance's own db/lock
   (the global `/proc` marker-scan footgun — see HANDOFF.md "Known footgun")

Next unfinished task: P3 step 4 — mycelium (a14/a15/a16) with a real `mycelium serve` subprocess
under `MYCELIUM_CONFIG` → temp store (user store untouched): worker recalls a primary-saved fact
(a14), outage degrades or blocks per `memory_required` (a15), shared memory key (a16). Then
step 5 (scope the orphan-kill).

## Decisions log (condensed; full list in HANDOFF.md)
- Worker permission: `bypassPermissions` + strict `--allowedTools` allowlist (PRD §10 concedes "validation, not OS isolation")
- Integration: explicit `ladd integrate <job> --commit <sha>` — the primary is the integration authority
- `invalid_result`: no auto-retry (needs a `ladd resume` decision)
- 8th MCP tool `dispatch_integrate` added (PRD's tool list was "proposed")
- Deadline = total job budget from first spawn; no attempt may start with <60s remaining
- SIGTERM / `ladd stop`: stop the scheduler, **leave workers running** (re-adopted on restart)
- Runtime `ladd backend ...` changes are live-only; config reasserts on the next daemon start (safety property)
- API wire: newline-JSON over a Unix socket (0600); credential redaction; 8k response cap
- **Worker-death 143 (external SIGTERM): SUSPENDED** — not reproducible on the current daemon;
  dispatcher / systemd-oomd / kernel OOM / Cline all exonerated with evidence. See HANDOFF.md + mycelium memory #195.

## Live environment (as of 2026-10-17)
- tr4-primary `:8091` (vLLM, qwen3.8-27b-fp8) = the primary coordinating session, never worker-scheduled.
- tr4-secondary `:8080` (strata, `swift-1.5-iq3_xxs`) = RESERVED for the Cline interactive session.
- i7 `192.168.0.100:8080` (llama.cpp, `local-metrics`) = disabled (second interactive Claude session).
- Both worker backends are enabled only when the user frees them; re-reserve/disable after any demo.
