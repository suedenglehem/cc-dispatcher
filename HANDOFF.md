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
- P3 (recovery/memory/failure injection; all 20 acceptance tests): **complete (2026-10-17)** —
  steps 1–5: step 1 = live 65% context warning + overflow evidence + both config knobs
  wired; step 2 = crash / checkpoint-then-crash / backend-disconnect / tests-fail recovery e2e
  (`test_i_crash_recovery.py`, 4 tests, packet-level proof via `FAKE_ECHO_PACKET`); step 3 =
  a10 green with a REAL `dispatcherd` subprocess (SIGKILL mid-job → restart → adopt, no
  duplicate, capacity reconciled, flock negative control); step 4 = mycelium client (long-lived
  stdio child, respawn-once, single-flight, outage-degrade), `ladd promote`, and a14/a15/a16
  green with a REAL `mycelium serve` under `MYCELIUM_CONFIG` → temp store (user store untouched)
  — **20/20 acceptance tests green**; step 5 (hardening) = the `startup_reconcile` orphan-kill
  scoped to the instance's own state dir (`DISPATCHER_STATE_DIR` worker tag; foreign markers
  recorded, never killed) — **full suite 92 passed / 0 skipped**
- P4 (Cline/docs/hardening): **live smokes + fixes done (2026-10-17)** — Cline adapter
  verified against `cline --help` 3.0.56 (found + fixed two draft bugs: `-p` is PLAN mode,
  removed; `--auto-approve false` would hang, now `true`; added `-t` remaining-budget and
  per-attempt `--data-dir` isolation) + 4 tests driving it through the real daemon with a
  fake binary. Full docs set written. User freed both backends → the two gated live smokes
  ran:
  - **Claude Code smoke on `ak` (ex-i7): PASSED** — `j_1bd3187818` succeeded on the first
    attempt (~2.5 min, real model), valid result.json + diff (added `triple(a)`), commit
    97240a9, tests via `uv`. Host renamed i7→ak, IP 192.168.0.100→**192.168.0.33** (config +
    `/etc/hosts` note updated; served id == label so no case gate).
  - **Cline smoke on tr4-secondary (strata): first attempt FAILED, root-caused,
    dispatcher-side fix, RE-RAN — PASS** (2026-10-17, after the user freed the backend).
    First attempt (`j_bc96773bee`) exit 1, class `crash`: the 401 came from
    **`api.openai.com`, not strata** — strace proved Cline 3.0.56's `openai-compatible`
    provider reads the **base URL only from its stored provider config** (ignores
    `OPENAI_API_BASE`). Fix = the `ccline` launcher (below). Re-run
    **`j_bba5405826` PASS**: attempt 1 exit 0 in ~1m45s; `ccline-auth.log` "Provider
    configured: openai-compatible (swift-1.5-iq3_xxs)"; run dialed the seeded
    `http://127.0.0.1:8080/v1` (run_result `finishReason: completed`, 15 iterations,
    142845 input tokens); valid `result.json` (triple + test_triple, pytest 3 passed);
    job `succeeded` with `scope_review_required` (worker's out-of-scope `.venv/`, 1982
    paths, zero outside it — a18 semantics, correctly flagged). tr4-secondary
    re-reserved after. **`ClineAdapter.smoke_verified = True`** (PRD §9: flipped on a
    real pass). Verbatim evidence in `docs/cline-status.md`.
  - **Resource-group cap demo on real backends — DONE, both jobs SUCCEEDED (clean re-run,
    2026-10-17 20:31–20:37 CEST, post-fix daemon pid 33270)**: group `tr4` (max 1) spans
    tr4-secondary + ak. Job A2 `j_3092d3777e` (is_even) ran on tr4-secondary 18:31:21→18:33:26Z
    holding BOTH the `tr4-secondary` backend AND the `tr4` group reservation; job B `j_a3b9717dda`
    (cube) pinned to ak sat `queued|no_capacity` for ~105s **while ak was physically idle**
    (`ladd workers`: ak `state=busy health=online`, zero reservations — busy purely from group
    consumption, PRD §7.5); B launched at 18:33:26.218Z — 11ms after A2's slot freed — and
    succeeded on ak (3m31s). Both results valid, commits `a1427ff` / `f6576d1`, and both workers
    cited numeric Mycelium ids (`[190, 201, 204, 205]` / `[190, 204, 205]`) accepted by the live
    validator — the `memory_references` fix proven end-to-end with real models.
  - **Two real bugs found + fixed + regression-tested** by the live runs: (1) FK crash on a
    config backend rename (i7→ak) — `backends.sync()` now keeps a removed-but-referenced
    backend as an inert tombstone instead of `DELETE`-ing an FK-referenced row
    (`test_i_backend_rename.py`, 2); (2) `memory_references` contract gap — a worker that
    cited real Mycelium ids `[190, 201]` (ints) was rejected as "must be a list of strings";
    the field is now documented in the packet and the validator accepts a memory id as number
    or string (`test_u_results.py`, +4).
  **full suite 102 passed / 0 skipped**

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

## Known footgun — RESOLVED in P3 step 5 (2026-10-17)
- `scheduler.startup_reconcile` scans ALL of `/proc/*/environ` for the `DISPATCHER_ATTEMPT_ID` marker and
  used to kill ANY unmatched marker process group. Two dispatcher instances with DIFFERENT state dirs
  (different DBs) would each see the other's workers as orphans and kill them. Fixed: workers are tagged
  `DISPATCHER_STATE_DIR=<state dir>` by both adapters; `scan_marker_processes()` reports the tag (scan
  stays global/DB-agnostic); the orphan-kill applies only to OWN markers (tag match, or legacy untagged
  worker whose attempt row is in the own DB — attempt rows are never deleted); foreign markers get a
  `foreign_marker_ignored` event and are never killed. Proven by `test_i_orphan_scope.py` (two real
  daemons, different state dirs, foreign worker survives) + `test_u_marker_scope.py` (truth table).
  (Was not the 143 cause — no second instance was running then.)

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
- **ak** (host renamed from i7 on 2026-10-17) `192.168.0.33:8080` (llama.cpp, served id
  `qwen3.8-27b` Q6_K, n_ctx 122880, ctx_max 110592) = 2nd worker backend → `enabled=false`
  in config (RESTORED to disabled after the 10-17 demo). **Use the IP, not the name** — the
  `/etc/hosts` entry still points at the stale `192.168.0.100`.
- claude **2.1.302** (needs `--verbose` for stream-json; vLLM rejects default effort `high` → `--effort xhigh`)
- **Never read/echo `~/.cline/data`** (provider secret). Never touch the two interactive sessions.
- **Post-restart ops:** config reasserts on daemon start — after ANY restart, re-release tr4-secondary and
  re-enable ak if workers should run on them. `pgrep -af local-dispatcher` finds the daemon (not `dispatcherd`).
- **No hot-reload (learned the hard way 2026-10-17):** editing `local_dispatcher/*.py` on disk does
  NOT affect the running daemon. After any code change that must be live (e.g. the `memory_references`
  fix — a real-model job cited numeric ids and the OLD daemon still rejected them), restart:
  `systemctl --user restart local-dispatcher`. Check freshness with
  `systemctl --user show local-dispatcher -p MainPID,ActiveEnterTimestampMonotonic` vs file mtimes.

## Changed files (2026-10-17 — live smokes + two bug fixes)
- `local_dispatcher/backends.py` — `sync()` removal path: a backend dropped from config is
  kept as an **inert tombstone** (`enabled=0`, `manually_reserved=1`,
  `reserved_note='removed from config; tombstone for history'`) when any job/attempt row
  still references it, instead of `DELETE`-ing an FK-referenced row (which raised
  `sqlite3.IntegrityError` at daemon start after the i7→ak rename); an unreferenced removed
  backend is still deleted.
- `tests/test_i_backend_rename.py` (NEW, 2) — referenced-removed→tombstone, unreferenced-
  removed→deleted. `_resync` opens its OWN `Database` (sqlite connections are thread-bound;
  the test thread ≠ the daemon thread).
- `local_dispatcher/results.py` — `_is_mem_ref()` helper (int-or-str memory id, rejects
  bool/float/dict/empty); `memory_references` split out of the free-text fields' "list of
  strings" check so a worker citing numeric Mycelium ids `[190, 201]` validates. The three
  genuinely free-text fields (`remaining_work`/`blockers`/`assumptions`) stay strict.
- `local_dispatcher/packets.py` — `RESULT_CONTRACT` now tells the worker that
  `memory_references` lists the Mycelium memory ids (numbers) of recalled memories it relied
  on, else `[]`.
- `tests/test_u_results.py` (+4) — numeric/string/mixed ids accepted; bool/float/dict/empty/
  non-list rejected; absent field fine.
- `docs/cline-status.md` — the 2026-10-17 real-smoke failure recorded VERBATIM (job
  j_bc96773bee, exit 1, stream tail, strace root cause, the user-side Base URL fix, updated
  re-run protocol, `OPENAI_API_BASE`-ineffective caveat); verdict header updated.
- `~/.config/local-dispatcher/config.toml` (LIVE, not in repo) — `ak` backend block (IP
  .33, group `tr4`, `enabled=false` default) + header i7→ak note; `cc-worker` profile
  `backend_ids = ["tr4-secondary", "ak"]`; `[[resource_groups]] tr4 max_concurrency=1` spans
  both worker backends.

## Changed files (P4 — Cline adapter + full docs)
- `local_dispatcher/adapters/cline.py` — rewritten on the `cline --help` 3.0.56 evidence:
  prompt = positional argv (act mode; NO `-p` plan-mode flag), `--auto-approve true`,
  `--retries 1`, `--thinking low`, `--compaction basic`, `-t <remaining job budget>`
  (`_timeout_s`, fallback = effective deadline), `--data-dir <artifact_dir>/cline-data`
  (per-attempt state isolation; `--config` stays default so the provider baseUrl resolves;
  the dispatcher NEVER reads `~/.cline/data`), no `--worktree`, no `-s`; env = explicit
  `OPENAI_API_BASE`/`OPENAI_API_KEY` + marker vars incl. `DISPATCHER_STATE_DIR`;
  `classify` fixed (was an unused-loop-var pattern) + `fetch failed`/`etimedout` patterns
- `tests/fakes/fake_worker.py` — `FAKE_PROMPT_FROM=argv` contract (prompt = last argv
  element) alongside the stdin contract; one fake serves both adapters
- `tests/test_i_cline_adapter.py` (NEW, 4 tests) — e2e (fake cline through the real daemon
  → succeeded; the valid result file proves the argv packet carried `RESULT_PATH`),
  build() contract, `-t` remaining-budget math, classify table
- `docs/setup.md` (NEW) — install/config/systemd/first-run/MCP + the backend enablement
  protocol; `docs/recovery.md` (NEW) — reconcile table incl. foreign-marker scoping,
  retryable classes, quarantine/reconcile, blocked-job table, SIGKILL drill;
  `docs/troubleshooting.md` (NEW) — 11 failure classes + operational issues;
  `docs/mycelium-notes.md` (NEW) — daemon client, worker allowlist, envelope, promote,
  security (no auth → never expose, DBs off network shares); `docs/cline-status.md` (NEW) —
  the PRD §9 verdict: verified flags, NOT real-model-verified, both gates, smoke protocol
- `config.example.toml` — commented `cline-worker` profile; `install.sh` — stale "Phase 2"
  MCP note replaced with the real `claude mcp add` line + docs pointers
- Docs ground-truth cross-checked against the live tree: `ladd`/`ladd backend`/`ladd
  project` help, FailureClass enum + RETRYABLE set, all 26 emitted event names,
  `status_detail` values, console scripts (`ladd`, `local-dispatcher`, `dispatcher-mcp`),
  live `~/.config/local-dispatcher/config.toml` `[mycelium]` shape (example now matches)

## Changed files (P3 step 5 — orphan-kill scoping)
- `local_dispatcher/adapters/{claude,cline}.py`: worker env gains
  `DISPATCHER_STATE_DIR=<instance state dir>` (the ownership tag)
- `local_dispatcher/procfs.py`: `scan_marker_processes()` reports `state_dir`
  per marker (`""` for legacy untagged workers); scan stays global + DB-agnostic
- `local_dispatcher/scheduler.py`: pure `marker_owned(state_dir, own_state_dir,
  attempt_in_db)` + `Scheduler._marker_owned` (legacy fallback = attempt row in
  the OWN db); `startup_reconcile` orphan-kill now only kills OWN unmatched
  markers, records `foreign_marker_ignored` (detail: pid/pgid/state_dir) for
  foreign ones; return gains a `foreign_ignored` key
- `tests/conftest.py`: `Env._write_toml(root=...)` override for a second state dir
- `tests/test_u_marker_scope.py` (NEW, 2): marker_owned truth table; real-process
  scan reports the state_dir tag (and `""` for legacy)
- `tests/test_i_orphan_scope.py` (NEW, 1): two REAL dispatcherd instances on
  different state dirs, same repo/backend — the foreign instance's worker
  survives the other's startup + kill grace, `foreign_marker_ignored` lands in
  the other's events, no `orphan_killed`, and the owner's job still succeeds

## Changed files (P3 step 4 — mycelium)
- `local_dispatcher/mycelium.py` (NEW): `MyceliumClient` — one long-lived `mycelium serve` stdio
  child; raw newline-JSON-RPC 2.0 (`initialize` → `notifications/initialized` → `tools/call`),
  responses correlated by id, reader thread + `queue.Queue`; **single-flight** (one in-flight
  call; future created BEFORE the request is written); **lifetime-1 respawn** (second death ⇒
  permanently `_unavailable`, no further spawns); `ensure()` is the memory_required gate (lazy
  client ⇒ `available` snapshot is a trap); `_call` returns None on ANY failure, never raises;
  `memory_section()` = ≤`max_recall_queries`(3) bounded recalls (limit 5 each) ≤ 4000 chars,
  partial-OK on mid-section outage; `save()` envelope helper, never raises
- `local_dispatcher/config.py`: `MyceliumConfig` (command/env/timeouts/caps); **safety
  property** — `enabled` derived from presence of the `[mycelium]` table (`m = raw.get(...)`
  with NO default) so an absent section can never spawn a child against the user's real
  `~/.mycelium`
- `local_dispatcher/scheduler.py`: memory gate in `_launch_prefilter`
  (`memory_required` ⇒ `await mycelium.ensure()` else `memory_required_unavailable` →
  **blocked**); `memory_section` rendered into the packet at render time
- `local_dispatcher/packets.py`: `MYCELIUM_PROJECT:` + `BASE_COMMIT:` packet lines (fallback
  `NONE = "(none)"`); `MEMORY_RECALLED` section only when non-empty
- `local_dispatcher/api.py` + `cli.py` + `jobs.py` + `models.py`: `ladd promote <job>` /
  `dispatch…promote` (pre-checks succeeded + `integrated_commit`; saves
  `[decision] … accepted` pinned, confidence 0.9) + error catalog entries
- `daemon.py`: `sync_projects_from_config` persists `mycelium_project` (restart never loses
  the project→key mapping; existing rows keep runtime mode)
- `tests/conftest.py`: full mycelium daemon wiring (`Env.daemon(mycelium=dict|None)` renders
  `[mycelium]` + `[mycelium.env]` into the temp TOML; `DaemonHandle.mycelium`; close on shutdown)
- `tests/helpers.py`: `MYCELIUM_BIN` + `make_mycelium_store(root)` — temp TOML whose
  `[storage]` db/backups AND `[foundry]` log/db all live under the pytest tmp root
  (foundry is on by default — its paths MUST be overridden or `~/.mycelium` gets written);
  all three tests `skipif(not exists(MYCELIUM_BIN))`
- `tests/fakes/fake_worker.py`: parses `MYCELIUM_PROJECT:`/`BASE_COMMIT:` from the packet;
  new `_mycelium_save_envelope` — stdlib-only sync JSON-RPC session against its own
  `mycelium serve` child (Popen + reader thread, 20s/call, kill in finally), gated by
  `FAKE_MYCELIUM_CMD`(JSON argv)+`FAKE_MYCELIUM_CONFIG`(TOML); a failed save is swallowed
  (PRD §12: memory outage never fails the job)
- `tests/test_a14_worker_recalls_fact.py` / `test_a15_mycelium_outage.py` /
  `test_a16_shared_memory_key.py`: rewritten as real-subprocess tests (a15 kills
  `h.mycelium.pid` with SIGKILL twice — spawn-once + respawn-once makes the 4-job
  healthy/respawn/degrade/blocked sequence deterministic; a16 runs the daemon WITHOUT a
  `[mycelium]` section — the worker's own child proves the shared key end-to-end)

## Changed files (P1 + P2)
- `local_dispatcher/` full package: config, db(+migrations/0001_init.sql), models, procfs, worktrees,
  backends, packets, results, supervisor, jobs, scheduler (launch gate + startup reconcile),
  api (ApiServer/ApiClient), adapters/{base,claude,cline}, daemon (flock+signals), cli (ladd), __main__,
  **mcp_adapter (P2: 8 tools, stateless stdio→socket forwarder)**
- `prompts/` — PRD §17/§18/§19 verbatim + §20 packet template + result-schema.json
- `config.example.toml`, `systemd/local-dispatcher.service`, `install.sh`, `pyproject.toml`, `.gitignore`
- P2 new: `tests/test_mcp_adapter.py` (8 tools), `docs/commands.md` (§21 text)
- P3 step 2: `tests/fakes/fake_worker.py` (`backend_down` mode + `FAKE_ECHO_PACKET` toggle —
  echoes the received packet on the normal path so multi-attempt tests can inspect what
  attempt N got), `tests/test_i_crash_recovery.py` (new, 4: crash→uncertain-recovery→succeed,
  checkpoint-then-crash→certain-recovery→succeed, backend_down→backend_disconnect→retry→succeed,
  tests_fail→non-retryable failed)
- P3 step 1: `results.py` (`context_pct` helper), `supervisor.py` (`Child.context_warned`),
  `adapters/{base,claude,cline}.py` (`classify` gains `overflow_pct`), `scheduler.py`
  (`live_context` + `_context_watch` one-shot `context_warning` event; classify + `usage_json`
  enriched with `ctx_limit`/`context_pct`), `api.py` (live context on running attempts in the job view),
  `jobs.py` (`usage` decoded in the job view), `tests/fakes/fake_worker.py` (`high_context` mode +
  `FAKE_USAGE_TOKENS`/`FAKE_CTX_HOLD_S`), `tests/test_u_ctx.py` (+7), `tests/test_i_context_warning.py` (new)
- P3 step 3 (a10): `tests/test_a10_restart_no_dup.py` (full rewrite: real `dispatcherd`
  subprocesses — `RealDaemon` + `db_query` WAL-reader helpers; hold-mode worker, flock negative
  control, SIGKILL mid-job, adoption + no-dup + capacity assertions via `FAKE_LAUNCH_LOG`).
  Two production fixes found by the test:
  - `scheduler.py` `_finalize`: a **re-adopted** child (spawned daemon died ⇒ `exit_code=None`,
    the exit went to init) is now judged by its on-disk result file — a schema-valid report
    (any outcome) ⇒ `adapter_class=None` so `evaluate_attempt` decides on merits; only with no
    valid report is it `exit_unknown`. Previously `exit_code=None` was unconditionally
    `exit_unknown` → terminal failure for a normally-completing adopted worker.
  - `models.py` `RETRYABLE`: `EXIT_UNKNOWN` is now retryable (crash-like; the process is
    confirmed dead before finalize, so a retry cannot duplicate execution).
  - `procfs.py` `scan_marker_processes`: read `/proc/<pid>/stat` **field 4 (ppid)** where it
    meant **field 5 (pgrp)** — after the spawning daemon dies the worker re-parents to init
    (ppid=1), so every restart-adoptation looked like PID reuse and was quarantined
    (`quarantined_pid_reuse`). Now uses `pgid_of` (field 5 = pgrp), stable across re-parenting
    because the worker is its own group leader (`start_new_session`).
  - `tests/fakes/fake_worker.py`: `_emit` swallows `BrokenPipeError`/`OSError` — after the
    supervisor dies the stream pipe's read end is gone; the result contract is file-based
    (`RESULT_PATH`/`CHECKPOINT_PATH`), so the worker keeps going and the re-adopting daemon
    takes the result file as the authority.
- P4 cline base-URL fix: **`bin/ccline`** (new — launcher: `cline auth
  openai-compatible -b $OPENAI_API_BASE -m <model> [-k key] --data-dir <dir>`
  into the run's data-dir, then `exec cline <argv>`; auth log →
  `<data-dir>/ccline-auth.log`; key never echoed), `install.sh` (installs it to
  `/usr/local/bin`), `config.py` + `config.example.toml` + live `config.toml`
  (`cline_executable = /usr/local/bin/ccline`), `adapters/cline.py` (docstring:
  provider config comes from the ccline-seeded per-attempt data-dir, NOT
  `~/.cline`), `docs/cline-status.md` (evidence table + new re-run protocol),
  `tests/test_i_cline_adapter.py` (4→6)
- P2 bug fixes: jobs.py PROFILE/BACKEND_UNKNOWN `known=` now serializes ids (was leaking live pydantic
  objects into ApiError → json.dumps crash in `_send`, dropped the connection with no job row); cli
  backend-check workers path; adapters/claude.py `--verbose` + effort handling; api.py redaction + 8k cap

## Test results
- pytest: **102 passed, 0 skipped** (~95s) — **20/20 acceptance tests green**
- Acceptance a01–a20 (20): green — a10 with a REAL `dispatcherd` subprocess (~10s: two
  daemon lifetimes + SIGKILL mid-job); a14/a15/a16 with a REAL `mycelium serve` subprocess
  (temp store, user `~/.mycelium` untouched — 1.9s / 4.4s / 2.3s); P2 mcp_adapter (8): green
- P3 step 1: `test_i_context_warning.py` (2 integration: live view + one-shot `context_warning` event +
  durable `usage_json` evidence; below-threshold negative) green
- P3 step 2: `test_i_crash_recovery.py` (4 integration) green — incl. packet-level assertion that
  the RECOVERY_CONTEXT (checkpoint or uncertain marker + `from_attempt`) reached attempt 2's stdin
- P3 step 5: `test_u_marker_scope.py` (2 unit: marker_owned truth table, real-process scan tag) +
  `test_i_orphan_scope.py` (1 integration: foreign instance's worker survives) green
- P4: `test_i_cline_adapter.py` (6: e2e through the real daemon with a fake cline binary
  speaking the argv-prompt contract; build() contract — no plan-mode `-p`, `--auto-approve
  true`, `--data-dir` under the artifact dir, `-t` = remaining budget; classify;
  **ccline wrapper ×2 — real `bin/ccline` against a fake `cline` shim: auth args
  (-b baseUrl / -m model / -k key / --data-dir) + unchanged-argv forwarding +
  missing-data-dir / missing-OPENAI_API_BASE / no-key negatives**) green
- Unit (53): ctx extraction ×3 probe shapes, `context_pct` math/boundary/unknown (4), claude
  `classify` overflow-threshold (3), packet renderer, result validator + evaluate_attempt decision
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
1. ~~P3 step 3: a10 — real `dispatcherd` subprocess SIGKILL mid-job → restart → adopt, no
   duplicate, capacity reconciled~~ ✅ DONE — 19/20 acceptance green.
2. ~~P3 step 4: mycelium (a14/a15/a16 real `mycelium serve` under `MYCELIUM_CONFIG`→temp
   store, user store untouched) + promote~~ ✅ DONE (2026-10-17) — **ALL 20 acceptance green,
   suite 89 passed / 0 skipped.**
3. ~~P3 step 5 (hardening): scope `startup_reconcile` orphan-kill to the instance's own
   db/lock~~ ✅ DONE (2026-10-17) — `DISPATCHER_STATE_DIR` worker tag; foreign markers
   recorded + never killed; suite 92 passed / 0 skipped.
4. P4 live smokes (2026-10-17): **(a) Claude Code on ak — DONE, PASSED**
   (j_1bd3187818, commit 97240a9); **(c) resource-group cap demo on real backends — DONE,
   BOTH SUCCEEDED** (A2 j_3092d3777e on tr4-secondary 105s → B j_a3b9717dda queued
   `no_capacity` on idle ak → launched 11ms after the group slot freed → succeeded on ak;
   commits a1427ff / f6576d1); **(b) Cline on strata — attempted, FAILED at Cline's
   base-URL layer, root-caused via strace, recorded verbatim in `docs/cline-status.md`**.
   **FIXED (2026-10-17, dispatcher-side — no user Cline-UI change needed):** new
   `bin/ccline` launcher (installed `/usr/local/bin/ccline`; `install.sh` ships it):
   runs `cline auth openai-compatible -b "$OPENAI_API_BASE" -m <model> -k ...
   --data-dir <dir>` into the run's own isolated data-dir, then `exec cline <argv>`.
   Mechanism proven against the real cline 3.0.56 binary (refused-port oracle: the run
   dials the seeded URL; setting survives the run's own save). `cline-worker` profile
   executable now `ccline` (config re-asserted on daemon restart).
   **DONE (2026-10-17, user freed tr4-secondary): the real Cline smoke re-run PASSED** —
   `j_bba5405826`, attempt 1, exit 0, ~1m45s; ccline seeded the per-attempt baseUrl
   (`ccline-auth.log` + `providers.json` show `http://127.0.0.1:8080/v1`); run_result
   `finishReason: completed`, 15 iterations; valid result.json (triple + test_triple,
   pytest 3 passed); job `succeeded` with `scope_review_required` (worker's `.venv/`
   only — zero violations outside it). `ClineAdapter.smoke_verified = True`; outcome
   recorded verbatim in `docs/cline-status.md`; tr4-secondary re-reserved immediately
   after. Backends re-secured to safe defaults (tr4-secondary reserved, ak disabled).
   **Suite 104 passed / 0 skipped** (the 102 + 2 new ccline-wrapper tests).
5. Housekeeping (deferred): ~13 stale worktrees under
   `~/.local/share/local-dispatcher/worktrees/`; `/tmp/cline-seed-test*` scratch dirs.
