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
| P3 | Recovery, restart reconcile, mycelium memory, failure injection → 20/20 acceptance | ✅ done (2026-10-17) |
| P4 | Cline adapter, resource-group demo, hardening, full docs + install | ✅ done (2026-10-17): live smokes run — ak PASS, Cline root-caused (user-side fix), group cap proven live |

## Acceptance tests (PRD §14)
- **GREEN: 20/20** — a10 with a REAL `dispatcherd` subprocess (SIGKILL mid-job → restart →
  adopt, no duplicate, capacity reconciled, flock negative control); a14/a15/a16 with a REAL
  `mycelium serve` subprocess under `MYCELIUM_CONFIG` → temp store (user store untouched)
- Non-acceptance P3: `test_i_context_warning.py` (2) + `test_i_crash_recovery.py` (4), green
- Suite: **104 passed, 0 skipped** (~95s) — 6 added with the P4-live bug fixes
  (FK tombstone `test_i_backend_rename.py` ×2, `memory_references` numeric-id contract
  `test_u_results.py` ×4) + 2 for the ccline wrapper (`test_i_cline_adapter.py`)

## P3 (recovery / restart / memory / failure injection) — COMPLETE
Goal: all 20 acceptance tests green, using real subprocesses.
Status: **ALL 5 STEPS DONE (2026-10-17) — 20/20 acceptance green, 92 passed / 0 skipped**.
Step 1 — stream-json usage tracking was already wired
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
4. ~~mycelium client + packet memory injection + envelope + outage degrade + promote~~
   ✅ **done (2026-10-17)** — new `local_dispatcher/mycelium.py`: `MyceliumClient` = one
   long-lived `mycelium serve` stdio child (raw newline-JSON-RPC 2.0, single-flight,
   lifetime-1 respawn — second death ⇒ permanently unavailable; `_call` never raises;
   `memory_section` = ≤3 bounded recalls ≤4000 chars, partial-OK on mid-section outage;
   `ensure()` is the `memory_required` gate). Config: `MyceliumConfig` with `enabled` derived
   from PRESENCE of the `[mycelium]` table (absent section can never spawn against the user's
   `~/.mycelium`). Scheduler: `memory_required` + client down ⇒ `blocked
   (memory_required_unavailable)`. `ladd promote` / MCP promote (succeeded + integrated_commit
   ⇒ pinned `[decision]` save). Fake worker: parses `MYCELIUM_PROJECT:`/`BASE_COMMIT:` and,
   when `FAKE_MYCELIUM_CMD`/`FAKE_MYCELIUM_CONFIG` are set, saves the `[worker-checkpoint]`
   envelope through its own stdio child (failed save swallowed). `make_mycelium_store(root)`
   points ALL mycelium paths (storage db/backups + foundry log/db — foundry is on by default)
   at the pytest tmp root. a14 = primary-saved fact in the packet (`MEMORY_RECALLED`);
   a15 = SIGKILL the daemon's child twice: job1 healthy → job2 respawn → job3 degrades (no
   section, still succeeds) → job4 blocked per `memory_required`; a16 = daemon WITHOUT a
   `[mycelium]` section (isolation), worker's own child saves under the project key, primary
   side recalls the job id.
5. ~~Hardening: scope `startup_reconcile` orphan-kill to the instance's own db/lock~~ ✅
   **done (2026-10-17)** — workers tagged `DISPATCHER_STATE_DIR=<state dir>` (both adapters);
   `scan_marker_processes()` reports the tag (stays global/DB-agnostic); pure
   `marker_owned(state_dir, own_state_dir, attempt_in_db)` decides — own iff tag matches, or
   legacy untagged worker whose attempt row is in the OWN db (rows never deleted); the
   orphan-kill applies to own markers only; foreign markers get a `foreign_marker_ignored`
   event and are NEVER killed. Tests: `test_u_marker_scope.py` (2: truth table +
   real-process scan tag) + `test_i_orphan_scope.py` (1: two REAL daemons, different state
   dirs — foreign worker survives startup + kill grace, no `orphan_killed`, owner's job
   still succeeds). `Env._write_toml(root=...)` for the second state dir. Suite 92 passed /
   0 skipped (~92s).

## Current milestone: P4 (Cline, resource groups, full docs) — COMPLETE
Status: **done (2026-10-17)** — all three gated live smokes were run once the user freed
both worker backends (strata :8080 and ak); backends re-reserved/disabled after each.

1. ~~Cline adapter~~ ✅ (2026-10-17) — flags verified against `cline --help` 3.0.56
   (positional prompt = act mode; two draft bugs fixed: `-p` is PLAN mode → removed,
   `--auto-approve false` would hang unattended → `true`); added `-t` = remaining job
   budget and per-attempt `--data-dir <artifact_dir>/cline-data` (worker never touches
   `~/.cline/data`; dispatcher never reads it — provider key stays in Cline's process);
   `tests/test_i_cline_adapter.py` (4: e2e through the real daemon with a fake binary
   speaking the argv-prompt contract, build() contract, timeout math, classify).
   `ClineAdapter.smoke_verified` stays `False` — PRD §9: no real smoke, no "complete".
2. ~~Full docs~~ ✅ — `docs/setup.md`, `docs/recovery.md`, `docs/troubleshooting.md`,
   `docs/mycelium-notes.md`, `docs/cline-status.md` (the PRD §9 verdict + exact unblock
   protocol); `docs/commands.md` already shipped verbatim §21; `config.example.toml`
   gained the commented `cline-worker` profile; `install.sh` stale "Phase 2" note fixed.
   All doc facts cross-checked against the live tree (CLI help, FailureClass + RETRYABLE,
   26 event names, status_detail values, console scripts, live config shape).
3. Suite: **96 passed, 0 skipped** (~95s); 20/20 acceptance still green.

Live smokes — all run 2026-10-17 on freed backends, recorded verbatim, then re-secured:
- **ak Claude Code smoke — PASS** (`j_1bd3187818`, first attempt, real model, ~2.5 min):
  worker added `triple(a)` + test, 3 tests passed, commit `97240a9`. Proved the exact
  claude flag combo (stream-json + stdin + `--allowedTools` MCP names + result-file
  contract) against a real llama.cpp backend.
- **Cline smoke — first attempt FAILED, root-caused, then FIXED dispatcher-side**
  (`j_bc96773bee`, attempt 1, exit 1, class `crash`, `recovery_exhausted`): Cline's
  `openai-compatible` provider reads its base URL **only from the stored provider
  config** in its `--data-dir`, ignoring `OPENAI_API_BASE`/`OPENAI_BASE_URL` — strace
  shows 38×TCP :443 + SNI `api.openai.com`, 0× :8080. Fix (2026-10-17): new
  **`ccline` launcher** (`bin/ccline` → `/usr/local/bin/ccline`, installed by
  `install.sh`) — runs `cline auth openai-compatible -b "$OPENAI_API_BASE" -m <model>
  -k ... --data-dir <dir>` (the sanctioned baseUrl setter) into each attempt's isolated
  data-dir, then `exec cline <argv>`; `cline-worker` profile executable now `ccline`.
  Proven against real cline 3.0.56 (refused-port oracle: run dials the seeded URL,
  setting survives the run's save). No user Cline-UI change is needed. Full evidence +
  protocol in `docs/cline-status.md`. `ClineAdapter.smoke_verified` stays `False`
  until the re-run passes.
- **Resource-group cap demo — serialization proven live, BOTH jobs SUCCEEDED**
  (group `tr4`, max_concurrency=1; clean re-run 2026-10-17 20:31–20:37 CEST, post-fix
  daemon): job A2 (is_even, `j_3092d3777e`) on tr4-secondary 18:31:21→18:33:26Z held both
  the `tr4-secondary` backend AND the `tr4` group reservation; job B (cube, `j_a3b9717dda`)
  pinned to ak sat `queued|no_capacity` for ~105s while ak was physically idle — `ladd
  workers` showed ak `state=busy health=online` with zero reservations (busy purely from
  group budget consumption, exactly the PRD §7.5 semantics); B launched 11ms after A2's slot
  freed and succeeded on ak (3m31s). Commits `a1427ff` / `f6576d1`; both workers cited
  numeric Mycelium ids (`[190, 201, 204, 205]` / `[190, 204, 205]`) accepted by the live
  validator — the `memory_references` fix proven end-to-end with real models. (a04 covers
  this with fakes; this is the real-backend confirmation.)

Two bugs found + fixed during the live smokes (both regression-tested, suite 96→102):
1. **FK tombstone** (`backends.py`): a config backend rename is remove+add; `sync()` now
   keeps a removed backend that jobs/attempts still reference as an inert tombstone
   (`enabled=0, manually_reserved=1, reserved_note='removed from config; …'`) instead of
   raising IntegrityError, and deletes only unreferenced removed backends.
   `tests/test_i_backend_rename.py` (2).
2. **`memory_references` contract** (`results.py` + `packets.py`): recall output carries
   numeric Mycelium ids, so workers cite them as ints (`[190, 201]`) and were rejected by
   a "list of strings" check. Now `_is_mem_ref` accepts a number OR string (rejects bool/
   float/dict/blank), and the packet RESULT_CONTRACT documents the field.
   `tests/test_u_results.py` (+4).

Next unfinished task: **re-run the Cline smoke — the only gate is the user freeing
tr4-secondary** (base-URL blocker removed by the `ccline` launcher): `ladd backend
release tr4-secondary` (AFTER any daemon restart, which re-asserts the reservation),
submit a tiny `cline-worker` job, record the outcome VERBATIM (exit code, stdout
tail, result.json); flip `ClineAdapter.smoke_verified = True` only on a real pass;
re-reserve tr4-secondary after. (Housekeeping: ~13 stale worktrees under
`~/.local/share/local-dispatcher/worktrees/` from earlier demo jobs +
`/tmp/cline-seed-test*` scratch dirs; pruning deferred.)

## Decisions log (condensed; full list in HANDOFF.md)
- Worker permission: `bypassPermissions` + strict `--allowedTools` allowlist (PRD §10 concedes "validation, not OS isolation")
- Integration: explicit `ladd integrate <job> --commit <sha>` — the primary is the integration authority
- `invalid_result`: no auto-retry (needs a `ladd resume` decision)
- 8th MCP tool `dispatch_integrate` added (PRD's tool list was "proposed")
- Deadline = total job budget from first spawn; no attempt may start with <60s remaining
- SIGTERM (`systemctl --user stop`; there is no `ladd stop`): stop the scheduler,
  **leave workers running** (re-adopted on restart)
- Runtime `ladd backend ...` changes are live-only; config reasserts on the next daemon start (safety property)
- API wire: newline-JSON over a Unix socket (0600); credential redaction; 8k response cap
- **Worker-death 143 (external SIGTERM): SUSPENDED** — not reproducible on the current daemon;
  dispatcher / systemd-oomd / kernel OOM / Cline all exonerated with evidence. See HANDOFF.md + mycelium memory #195.

## Live environment (as of 2026-10-17)
- tr4-primary `:8091` (vLLM, qwen3.8-27b-fp8) = the primary coordinating session, never worker-scheduled.
- tr4-secondary `:8080` (strata, `swift-1.5-iq3_xxs`) = RESERVED for the Cline interactive session.
- ak `192.168.0.33:8080` (llama.cpp, `qwen3.8-27b` Q6_K, n_ctx 122880) = the second worker
  backend, **disabled** by default. Host was renamed i7 → ak and moved 192.168.0.100 → .33
  on 2026-10-17; `/etc/hosts` still points the name at the OLD .100, so **use the IP, not the
  name**. Enable only when free: `ladd backend enable ak` (then `ladd backend check ak`).
- Both worker backends are enabled only when the user frees them; re-reserve/disable after any demo.
  Config is the source of truth and re-asserts these safe states on every daemon start.
