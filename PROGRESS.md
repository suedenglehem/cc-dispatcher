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
| P4 | Cline adapter, resource-group demo, hardening, full docs + install | ✅ done (2026-10-17): live smokes run — ak PASS, **Cline PASS** (root-caused → `ccline` fix → real pass `j_bba5405826`), group cap proven live |
| P5 | SaladCloud backends: `ladd -a/-d URL`, local Anthropic→OpenAI proxy, keyed probes, heartbeat | ✅ done (2026-10-19): 129/129 tests; register-while-stopped verified (503 path); **live online worker smoke PASS** (`j_d12f1ae064`, attempt 1, ~13.5 min) after fixing the proxy's clean-empty-stream bug. Committed `3314331` |

## Acceptance tests (PRD §14)
- **GREEN: 20/20** — a10 with a REAL `dispatcherd` subprocess (SIGKILL mid-job → restart →
  adopt, no duplicate, capacity reconciled, flock negative control); a14/a15/a16 with a REAL
  `mycelium serve` subprocess under `MYCELIUM_CONFIG` → temp store (user store untouched)
- Non-acceptance P3: `test_i_context_warning.py` (2) + `test_i_crash_recovery.py` (4), green
- Suite: **129 passed, 0 skipped** (~110s, 2026-10-19) — the 21 salad test cases added
  with P5 (9 unit + 12 integration cases):
  feature: `tests/test_u_salad.py` (9 unit: gateway parsing, nonce, config-append/remove
  splice + idempotence, profile-list rewrite, port allocation, keyfile, `_iter_sse`
  anomaly sentinels, `StreamBuilder` empty-vs-content incl. tool-only-is-not-empty) +
  `tests/test_i_salad.py` (12 integration: add-while-stopped, online/stop/start flips via
  real ticks, key-rejected 403, error paths, a real worker job on a salad backend,
  30s heartbeat on the real tick path, remove + tombstone, orphan-proxy adoption,
  `ladd -a/-d` CLI shorthand, parametrized real-proxy `test_stream_failure_surfaced`
  ×3 SSE modes). Earlier: 6 with the P4-live bug fixes
  (`test_i_backend_rename.py` ×2, `test_u_results.py` ×4) + 2 for the ccline wrapper

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
   `tests/test_i_cline_adapter.py` (6: e2e through the real daemon with a fake binary
   speaking the argv-prompt contract, build() contract, timeout math, classify,
   ccline wrapper ×2). `ClineAdapter.smoke_verified = True` — real model-level
   pass recorded 2026-10-17 (`j_bba5405826`, `docs/cline-status.md`).
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
  protocol in `docs/cline-status.md`.
  **Re-run 2026-10-17 — PASS** (`j_bba5405826`, attempt 1, exit 0, ~1m45s, after the
  user freed tr4-secondary): `ccline-auth.log` "Provider configured: openai-compatible
  (swift-1.5-iq3_xxs)"; run dialed the seeded `http://127.0.0.1:8080/v1` (run_result
  `finishReason: completed`, 15 iterations, 142845 input tokens); valid `result.json`
  (outcome completed, `triple(a)` + `test_triple`, pytest 3 passed); job `succeeded`
  with `scope_review_required` — the worker's out-of-scope `.venv/` (1982 paths, zero
  outside it) was correctly flagged (a18 semantics). tr4-secondary re-reserved
  after. `ClineAdapter.smoke_verified = True`.
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

All gated P4 work is DONE — both live smokes passed (ak Claude Code `j_1bd3187818`;
Cline `j_bba5405826`), backends re-secured (tr4-secondary reserved, ak disabled).
Housekeeping **DONE (2026-10-17):** all 18 stale demo worktrees removed (verified no
active jobs/attempts first; `worktrees/` now empty), `/tmp/cline-seed-test*` +
`/tmp/cline-cc-test` scratch dirs deleted; the 21 `dispatch/*` branches kept for
possible integration (diffs also in `artifacts/<job>/attempt-1/diff.patch`);
artifacts intact (7MB). Demo smoke results **integrated into demo-repo main
(2026-10-17, user-requested):** `j_1bd3187818` fast-forwarded to `97240a9`
(triple), `j_3092d3777e` merged at `946119a` (is_even), `j_a3b9717dda` merged
at `aea44a6` (cube) — all three marked `integration_disposition: accepted`
via `ladd integrate`; tests kept in `tests/test_app.py`; demo-repo suite
5/5 passed on main. `j_bba5405826` (Cline) was a duplicate of the triple task
and its branch held no commit (worktree changes only) — nothing to merge, not
marked integrated. **Post-P4 (2026-10-17, both done, pushed as `283b632`):**
(a) `ladd status` 8k-cap failure FIXED — default view bounded forever
(terminal jobs collapse to `jobs_terminal {count, recent: last 5}`), explicit
`--all-jobs` full dump is cap-exempt (the MCP forwarder never sends
`all_jobs`); +4 regression tests. Verified live: bounded default 4095 chars
with 20 jobs in history, full dump 13494 chars. (b) Dual-boot topology
CORRECTED per user — ak = 192.168.0.100 WINDOWS boot, i7 = 192.168.0.33
LINUX boot, one physical box, both `enabled=true` in group tr4; the earlier
"host renamed i7→ak, moved .100→.33" note was WRONG. No scheduler code
change needed: startup probes + 30s TTL already follow the live boot;
recheck via `ladd workers --refresh` / `ladd backend check <id>`; a mid-job
backend stop classifies `backend_disconnect` (retryable). Verified after
restart: i7 available/online (Linux up), ak offline (Windows down). Mycelium
#215 pins the corrected topology. Pushed to origin
(github.com:suedenglehem/cc-dispatcher, `aa019c0..283b632`): tree clean, in
sync with GitHub. `run.txt` (2026-10-17, user-requested): short start→finish
command walkthrough — start/refresh → team submit → watch → integrate →
finish → using the dispatcher in a different project (`ladd project add`
runtime registration, e.g. vast; MCP tools auto-bind by cwd), plus
troubleshooting one-liners; every command checked against the live CLI flag
order. Committed + pushed as `4fabd0b`, then section 6 restructured and
pushed again as `437e92f` (one-time `ladd project add`, THEN just fire claude
code in that folder — dispatch_* MCP tools bind from cwd; the terminal ladd
loop demoted to "only if you are NOT inside a Claude Code session").
**vast wired end-to-end (2026-10-18):** `/dd2/andrei/docker/vast` had never
been a git repo — that was the `project_path_invalid` cause. Fixed:
`git init -b main` + `.gitignore` (.venv/, reference/, .resume) + initial
commit `69ccac4` (36 files); GitHub remote origin =
git@github.com:suedenglehem/d-vast.git pushed (`main`, tracking set, clean
first push to an empty repo); registered as a dispatcher project via
`ladd project add vast /dd2/andrei/docker/vast --integration-ref main`
(runtime add, no daemon restart; `ladd project list`: mode=single). A stale
non-repo copy at `/dd2/andrei/vast` (old stub) was left untouched. Next
unfinished task: none — clean closed state (no code changes since `283b632`,
so the last recorded suite result 108/108 + 20/20 acceptance still stands;
both live smokes passed and recorded, backends re-secured, repo pushed in
sync with GitHub). First real vast delegation awaits the user.

## P5 (SaladCloud backends) — COMPLETE (2026-10-19, incl. live online worker smoke)
Status: **code + tests done (129/129), live verification complete — register-while-stopped
(503 path) AND a real worker job on the online group (attempt 1, succeeded).** Workers can
run on llama.cpp models served by a salad.cloud group through a local Anthropic→OpenAI
proxy. The user starts/stops the groups themselves; the dispatcher only registers the URL,
keeps the proxy alive, probes with the key, and schedules on it when online.

1. `ladd -a https://<group>.salad.cloud` / `ladd -d <url>` — top-level shorthand (id =
   first dot-label of the host; `-d` matches by normalized-gateway comparison). Full options
   via `ladd backend add-salad URL [--id --port --keyfile --model --thinking --concurrency
   --group --notes]` / `ladd backend remove-salad <id>`; same API methods
   (`backend_add_salad` / `backend_remove_salad`).
2. `salad_proxy.py` (vendored, stdlib-only): local 127.0.0.1 proxy, Anthropic API in /
   OpenAI out; rewrites every upstream request to the served alias (`--model`); injects
   `Salad-Api-Key` from the keyfile (default `/usr/local/bin/salad_api.txt`, never echoed);
   `/v1/models` echo, `/healthz` with a nonce, `/__upstream/<path>` keyed passthrough
   (ctx via `/props`). `--allow-http-upstream` is test-only, loopback hosts only.
3. `salad.py` manager: nonce `dispatcherd-<id>-<port>-<md5(endpoint)[:8]>` — stable across
   daemon restarts, so proxies are **re-adopted, never duplicated** (and survive a daemon
   restart by design, like workers under decision #8). Atomic config rewrite
   (tmp + validate + os.replace) then in-memory splice; the id is appended to every
   **claude-adapter** profile's `backend_ids` (cline profiles untouched). Removal stops the
   proxy, strips the block + profile entries, and tombstones the DB row when jobs reference
   it; a backend with an active job cannot be removed.
4. Scheduling: salad backends are first-class — probe = `ensure()` (proxy up) → keyed
   GET `/v1/models` (200/403/503) → ctx via `/__upstream/props` × 90%. A **stopped group is
   a normal `offline` state, not an error**: `ladd -a` on a 503 gateway registers fine and
   flips online on the next probe after the group starts. Worker env
   `ANTHROPIC_BASE_URL` = the local proxy, so `claude` runs with its Anthropic API shape
   unchanged.
5. Heartbeat: while a job has an active reservation on a salad backend, the daemon sends a
   1-token completion every 30s (`salad_heartbeat = false` opts out) — keeps
   `IDLE_SHUTDOWN=heartbeat` groups alive across long Bash gaps between LLM calls.
6. Bugs found & fixed while testing (all covered by the new tests):
   - `sync()` never pruned backends absent from config when the config had **zero** backends
     (invalid `NOT IN ()` SQL guarded behind `if ids:`) → removed backends stayed
     `enabled=1` in the DB; restructured so the prune loop runs in both branches. This
     regression-adjacent fix also repaired a latent bug in HEAD's non-empty path;
     `tests/test_i_backend_rename.py` (2) guards it.
   - `_TABLE_HDR` regex excluded `[[array-of-tables]]` headers → TOML appends never saw the
     profiles/backends tables.
   - Vendored proxy hard-rejected http upstreams → test-only loopback flag.
7. Live verification (2026-10-19): daemon restarted onto new code; `ladd -a
   https://tamarind-caraway-1rpqcqcnbbkmqdbv.salad.cloud` succeeded while the group was
   stopped — `proxy 127.0.0.1:18100, health offline — currently offline: HTTP 503`
   (exactly the designed path).
8. **Live online smoke — PASS (2026-10-19).** With the group running: idle TTFB ~1.7s
   (small prompt) but ~93.3s for a worker-shaped ~5K-token prompt (prompt_tokens 5189,
   [DONE] at 95.5s) — the Cloudflare front + one serializing group instance push
   real 12–16K-token worker prompts to/past the edge's ~100s origin timeout. First live
   job `j_8bf0ff2baf` hit that edge and failed as bogus `invalid_result` (bug below);
   the re-submitted job **`j_d12f1ae064` succeeded on attempt 1** (~13.5 min, 8 turns,
   30K-token input, exact requested `halve()` diff, all artifacts present).
   Bug found & fixed live:
   - **Proxy rendered zero-content/anomalous upstream streams as clean empty success.**
     The upstream answered 200 and the SSE stream ended with no content (edge/origin
     kill, in-band `data: {"error": …}` payload, or zero-token completion); the proxy
     emitted a normal `message_stop` with empty content → the worker CLI recorded an
     empty result → `invalid_result` (non-retryable) instead of a backend disconnect.
     Fix in `salad_proxy.py`: `_iter_sse` yields `__proxy_anomaly__` sentinels
     (non-SSE line, unparseable `data:`, top-level error payload — terminal);
     `StreamBuilder` tracks `saw_content` (text via `EmptyThinkStripper` OR a registered
     tool call) + `anomalies`; the streaming path checks `anomalies or is_empty()` after
     the (pre-flushed) tail and emits ONE `error` event (`api_error`, "upstream stream
     produced no content: …") instead of `finish()`. No adapter change — "api error"
     text already classifies `backend_disconnect` (retryable). Tests: 3 new unit
     (`test_u_salad.py`) + `test_stream_failure_surfaced` parametrized ×3 (ok /
     empty / in-band-error) driving the REAL proxy subprocess against the fake gateway's
     new SSE modes (`fake_salad_gateway.chat_mode`).
9. **Performance envelope (live, 2026-10-19)** — for planning on salad groups:
   small prompt TTFB ~1.7s; ~5K-token prompt ~93s; full worker job (8 turns,
   30K-token prompts) ~13.5 min end-to-end. Expect the same order for real tasks:
   a salad group is a latency-limited backend (one instance serializes; the
   Cloudflare edge can 524 a stalled origin), not a throughput backend.
10. **on_salad port + test instructions (2026-10-19, spillover).** The user's
    sibling project `/dd2/andrei/docker/on_salad` runs the same Anthropic→OpenAI
    bridge (`claude/sl_proxy.py` — the original this repo's `salad_proxy.py` was
    vendored from) and had the **identical** pre-fix zero-content bug, affecting
    interactive `cl_salad`/`sl_chat.sh` sessions. Ported the same 4-part fix
    verbatim (`_iter_sse` anomaly sentinels; `StreamBuilder` `saw_content` /
    `anomalies` / `is_empty`; pre-flushed `finish(tail)`; the streaming-path
    `anomalies or (not tail and is_empty())` → one `error` event). Then left test
    instructions in that project: `claude/TESTS.md` (offline suite + 3 live steps)
    and a deterministic offline regression suite `tools/test_sl_proxy_offline.py`
    (**10/10**, no group/TLS/money) in the project's `tools/test_*_offline.py`
    convention. on_salad commits `f500326` (fix + README note) and `5aa1b51`
    (test instructions + offline suite + README row), both pushed; the user's own
    `PROGRESS.md` left untouched. The user runs the **live** steps tomorrow (group
    up): `sl_curl.sh` baseline → `cl_salad -p "…pong…"` (proves no over-trigger) →
    the edge-kill failure now surfaces as an error instead of an empty answer.

Next unfinished task: none in the dispatcher (P5 closed). on_salad: the user runs
the live `claude/TESTS.md` steps tomorrow (salad group up); the offline suite
already passes 10/10.

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

## Live environment (as of 2026-10-19)
- tr4-primary `:8091` (vLLM, qwen3.8-27b-fp8) = the primary coordinating session, never worker-scheduled.
- tr4-secondary `:8080` (strata, `swift-1.5-iq3_xxs`) = RESERVED for the Cline interactive session.
- **Dual-boot box** (topology user-corrected 2026-10-17; the "renamed i7→ak,
  moved .100→.33" note was WRONG): ONE physical machine, two boots — `ak` =
  192.168.0.100:8080 = WINDOWS boot, `i7` = 192.168.0.33:8080 = LINUX boot;
  both llama.cpp serving `qwen3.8-27b` (Q6_K, n_ctx 122880); only one boot
  (one IP) is up at a time. Both ship `enabled=true` (group tr4, max 1);
  startup probes + 30s TTL follow whichever boot is live — verified after the
  2026-10-17 restart: i7 available/online (Linux up), ak offline (Windows
  down). Recheck anytime: `ladd workers --refresh` / `ladd backend check <id>`.
- tr4-secondary stays RESERVED for the Cline interactive session; release it
  (`ladd backend release tr4-secondary`) only while that session is off it,
  and re-reserve after any demo. Config is the source of truth and re-asserts
  these states on every daemon start.
- **SaladCloud**: `tamarind-caraway-1rpqcqcnbbkmqdbv` (qwen, model label
  `qwen`) registered live via `ladd -a` 2026-10-19 while the group was
  stopped (503 → `offline`, proxy on 127.0.0.1:18100), flipped `available`
  once the group started, and **served a real worker job to success**
  (`j_d12f1ae064`, attempt 1, ~13.5 min). The user manages group start/stop;
  the 30s probes follow the gateway. Known envelope: worker-shaped prompts
  take ~90s+ TTFB (Cloudflare edge + serial instance) — see P5 §8–9.
