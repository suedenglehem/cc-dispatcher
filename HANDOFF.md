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
  - **Claude Code smoke on `ak`: PASSED** — `j_1bd3187818` succeeded on the first
    attempt (~2.5 min, real model), valid result.json + diff (added `triple(a)`), commit
    97240a9, tests via `uv`. TOPOLOGY NOTE (corrected 2026-10-17, user-confirmed): this
    physically ran on 192.168.0.33 — the LINUX boot of the dual-boot box, named `i7` in
    config (the earlier "host renamed i7→ak, moved .100→.33" note was WRONG; see
    Verified environment).
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
- P5 (SaladCloud backends): **COMPLETE (2026-10-19, incl. live online worker smoke
  PASS)** — salad.cloud groups as first-class backends: `ladd -a/-d <url>` shorthand +
  `ladd backend add-salad/remove-salad`; vendored stdlib-only local proxy (Anthropic in /
  OpenAI out, rewrites every upstream request to the served alias, injects the
  `Salad-Api-Key` from the keyfile, keyed `/__upstream/` passthrough for ctx);
  orphan-proxy adoption by stable nonce (proxies survive daemon restarts, re-adopted,
  never duplicated — same philosophy as workers under the SIGTERM decision); 30s 1-token
  heartbeat per active salad reservation (keeps `IDLE_SHUTDOWN=heartbeat` groups alive);
  a stopped group (HTTP 503) is a normal `offline` state, NOT an error — register-while-
  stopped verified LIVE (2026-10-19): `ladd -a https://tamarind-caraway-1rpqcqcnbbkmqdbv.
  salad.cloud` → "added salad backend …: proxy 127.0.0.1:18100, health offline —
  currently offline: HTTP 503". **Live online smoke PASS (2026-10-19):** once the group
  was serving, probes went `available` with ctx; first live job `j_8bf0ff2baf` failed as
  bogus `invalid_result` — the upstream answered 200 but the stream ended with **zero
  content** (worker-shaped ~5K-token prompts take ~93s TTFB through the Cloudflare
  front + one serializing instance, right at the edge's ~100s origin timeout) and the
  proxy rendered that as a clean empty turn. **Fixed in `salad_proxy.py`:**
  `_iter_sse` yields `__proxy_anomaly__` sentinels (non-SSE line, unparseable `data:`,
  top-level error payload); `StreamBuilder` tracks `saw_content` + `anomalies`;
  zero-content/anomalous stream end emits ONE `error` event → adapter classifies
  retryable `backend_disconnect` (no adapter change). Re-submitted job
  **`j_d12f1ae064` SUCCEEDED attempt 1** (~13.5 min, 8 turns, 30K-token prompts, exact
  requested `halve()` diff, all artifacts). Live envelope: small-prompt TTFB ~1.7s,
  worker-shaped ~93s. Three bugs fixed along the way: `backends.sync()`
  never pruned stale DB rows when the config had zero backends (and the prune loop was
  latent-broken on HEAD's non-empty path) → restructured, guarded by
  `tests/test_i_backend_rename.py` (2); `_TABLE_HDR` regex missed `[[array-of-tables]]`
  headers so config splices skipped the backends/profiles tables; the clean-empty-stream
  bug above (+3 unit tests, +1 parametrized real-proxy integration test ×3 SSE modes).
  **Full suite 129 passed / 0 skipped** (salad: `test_u_salad.py` 9 unit +
  `test_i_salad.py` 12 integration cases, incl. a real worker job on a salad backend and
  the heartbeat on the real tick path).

- P6 (backend priority): **complete (2026-10-22)** — `priority` per `[[backends]]`
  (int ≥ 1, default 1). Semantics (user-specified): among free backends the
  HIGHEST priority gets the job first; ties keep the profile's `backend_ids`
  order; an explicit `backend_id` at submit still wins. Live config set:
  tr4-primary 3, i7 2, tr4-secondary/ak/salad 1; daemon restarted (schema v3).
  6 new unit tests; full suite re-run green.

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
- **Dual-boot box** (topology user-corrected 2026-10-17; the old "i7 renamed to ak,
  moved .100→.33" note was WRONG): ONE physical machine, two boots — `ak` =
  192.168.0.100:8080 = **WINDOWS** boot, `i7` = 192.168.0.33:8080 = **LINUX** boot;
  both serve `qwen3.8-27b` (Q6_K, n_ctx 122880, ctx_max 110592, served id == label,
  no case gate); only one boot (one IP) is up at a time. BOTH ship `enabled=true` in
  config (group `tr4`, max_concurrency=1); startup probes + the 30s TTL follow
  whichever boot is live. Verified after the 2026-10-17 restart: `i7 state=available
  health=online` (Linux boot up), `ak state=offline health=offline` (Windows boot down).
  Pinned as mycelium #215 (OVERRIDES-PRIOR).
- claude **2.1.302** (needs `--verbose` for stream-json; vLLM rejects default effort `high` → `--effort xhigh`)
- **Never read/echo `~/.cline/data`** (provider secret). Never touch the two interactive sessions.
- **Post-restart ops:** config reasserts on daemon start — ak/i7 now ship `enabled=true`
  in config (no re-enable needed after restart); tr4-secondary ships `manually_reserved`
  (re-release only while no Cline interactive session uses it). `pgrep -af
  local-dispatcher` finds the daemon (not `dispatcherd`).
- **No hot-reload (learned the hard way 2026-10-17):** editing `local_dispatcher/*.py` on disk does
  NOT affect the running daemon. After any code change that must be live (e.g. the `memory_references`
  fix — a real-model job cited numeric ids and the OLD daemon still rejected them), restart:
  `systemctl --user restart local-dispatcher`. Check freshness with
  `systemctl --user show local-dispatcher -p MainPID,ActiveEnterTimestampMonotonic` vs file mtimes.

## Changed files (2026-10-22 — backend priority)
- Feature: `priority` (int ≥ 1, default 1) per `[[backends]]`. `pick_backend`
  tries the highest priority first among free backends (stable sort — ties keep
  the profile's `backend_ids` order); preferred/submit-pinned id stays first overall.
- `local_dispatcher/config.py` — `BackendConfig.priority: int = 1`; TOML parse
  `int(b.get("priority", 1))`; `_validate` rejects `priority < 1`.
- `local_dispatcher/migrations/0003_priority.sql` (NEW) —
  `ALTER TABLE backends ADD COLUMN priority INTEGER NOT NULL DEFAULT 1`.
  **Gotcha:** the migration runner splits statements on EVERY `;` — a semicolon
  inside a SQL comment breaks the migration (hit on the first draft; comments
  must stay semicolon-free).
- `local_dispatcher/backends.py` — `sync()` persists priority (INSERT + ON
  CONFLICT); `pick_backend` sorts candidates by descending priority;
  `worker_availability()` entries gain `priority`.
- `local_dispatcher/models.py` / `api.py` / `cli.py` — `backend_add_salad`
  gains a `priority` param + `--priority` flag (default 1); `salad.py`
  `salad_block_text` writes `priority = N` only when ≠ 1. `cli.py` `_row`
  keep-tuple gains `priority` (shows in `ladd workers`).
- `tests/test_u_backend_priority.py` (NEW, 6) — higher-priority-first with
  fall-through as top candidates go offline; defaults keep declared order;
  preferred beats priority; sync persists + worker view exposes it; TOML parse
  (default/explicit); `priority = 0` rejected.
- Docs: `BACKENDS.md` (field reference line), `config.example.toml` (commented
  example on `ak`), `PROGRESS.md` (P6 milestone row + decisions log + live env).
- `~/.config/local-dispatcher/config.toml` (LIVE, not in repo) — priorities set
  per user: tr4-primary 3, tr4-secondary 1, ak 1, i7 2,
  tamarind-caraway salad 1. Validated with `load_config` before restart; daemon
  restarted with zero active jobs (all 7 jobs were blocked) → safe. Verified
  post-restart: `schema_version: 3`, `ladd workers` priorities
  {3, 1, 1, 2, 1} in JSON + text.
- Propagated to i7 (2026-10-22): pushed 1bab661 to origin, `git pull --ff-only`
  on i7's install (`/dd2/andrei/claude/dispatcher`, was at 6745ec0), daemon
  restarted with zero jobs/reservations → schema v3, worker row shows
  `priority`. Note: i7's venv has **no pytest** (deployment clone) — verified
  via daemon behavior, not the suite; `ladd` there is `~/.local/bin/ladd` (not
  on the non-interactive ssh PATH).
- i7 live config extended (2026-10-22, `~/.config/local-dispatcher/config.toml`
  ON I7; backup `config.toml.bak-2026-10-22`): added tr4's two servers as
  cross-host backends — `tr4-primary` (vLLM, http://192.168.0.30:8091,
  qwen3.8-27b-fp8) **priority 3**, `tr4-secondary` (strata
  swift-1.5-iq3_xxs, label qwen, http://192.168.0.30:8080) **priority 1**;
  `i7-local` raised to **priority 2**; `cc-worker` `backend_ids` now
  `["tr4-primary", "i7-local", "tr4-secondary"]`. tr4's LAN IP = 192.168.0.30
  (hostname b760one); both servers verified reachable from i7 keyless via
  `curl /v1/models` BEFORE the config change. No new resource group (per-backend
  concurrency 1 already caps each server at 1 job from i7). Validated with
  `load_config`, restarted idle → all three online, priorities {3, 2, 1}.
  User (2026-10-22): the two daemons never dispatch concurrently — only ONE
  primary works on a job at a time, so tr4's vLLM won't actually see load from
  both daemons; don't re-flag the combined-load question.

## Changed files (P5 — SaladCloud backends, 2026-10-19)
- `local_dispatcher/salad_proxy.py` (NEW, vendored, stdlib-only) — local 127.0.0.1 proxy:
  Anthropic `/v1/messages` in → OpenAI chat/completions out, streaming; rewrites every
  upstream request's model to the served alias (`--model`); injects `Salad-Api-Key` from
  the keyfile; GET `/v1/models` echoes the alias (claude CLI compatibility); `/healthz`
  echoes `--nonce`; GET `/__upstream/<path>` keyed passthrough (ctx via `/props`);
  `--allow-http-upstream` = TEST-ONLY, loopback hosts only (public Salad gateways are
  https; a plain-http upstream is a SystemExit otherwise). **Streaming-failure
  surfacing (fix for live `j_8bf0ff2baf`):** `_iter_sse` yields
  `__proxy_anomaly__` sentinels instead of silently dropping non-SSE lines /
  unparseable `data:` payloads (a top-level `{"error": …}` is terminal);
  `StreamBuilder` tracks `saw_content` (real text through the EmptyThinkStripper OR a
  registered tool call) + `anomalies`; the streaming handler emits ONE `error` event
  (`api_error`, "upstream stream produced no content: …") when the stream ends
  zero-content or anomalous — never a fake clean `message_stop` — so the worker CLI
  fails loudly and the adapter classifies retryable `backend_disconnect`.
- `local_dispatcher/salad.py` (NEW) — manager + config plumbing: `SaladProxyManager`
  (spawn/adopt by nonce `dispatcherd-<id>-<port>-<md5(endpoint)[:8]>` — stable across
  daemon restarts; `ensure`/`stop`/`stop_all`; `heartbeat_tick(db)`: 1-token
  POST /v1/completions every 30s per backend with an ACTIVE reservation and
  `salad_heartbeat=True`; the first tick after a reservation appears beats immediately
  — `_last_beat` starts empty). `normalize_gateway` (explicit http kept for
  127.0.0.1/localhost, https for public hosts, path dropped); `append_salad_backend` /
  `remove_salad_backend` (atomic tmp+validate+os.replace rewrite; id appended to every
  **claude-adapter** profile's `backend_ids` only — cline profiles untouched;
  idempotent); `find_salad_backend` (normalizes both sides); `allocate_salad_port`
  (18100–18199); `read_salad_key` (keyfile chmod-checked, NEVER echoed/logged).
- `local_dispatcher/api.py` — `backend_add_salad` / `backend_remove_salad` methods +
  `ApiClient` wrappers; `_commit_config` (atomic rewrite) + `_reload_config` (in-memory
  splice of the changed backends/profiles lists); add runs one probe immediately and
  reports `health` + `key_rejected` (403) without failing the add; remove refuses while
  a job references the backend.
- `local_dispatcher/backends.py` — salad probe path: `ensure()` (proxy-up precondition)
  → keyed GET `/v1/models` → `ProbeResult` (200/403/503) → ctx via
  `_salad_ctx_via_proxy` (`/__upstream/props` → `default_generation_settings.n_ctx`)
  × 90% via `_record`; `pick_backend` / `worker_availability` include salad rows
  (availability entry adds `kind` + `proxy`); **`sync()` prune fix** — the
  absent-from-config removal loop now runs for BOTH the empty-config and non-empty
  paths (HEAD only pruned the non-empty path, and never at all with zero config
  backends — the invalid `NOT IN ()` SQL was guarded behind `if ids:`); referenced rows
  become inert tombstones (disabled + reserved), unreferenced rows are deleted.
- `local_dispatcher/cli.py` — top-level `ladd -a <url>` / `ladd -d <url>` shorthand
  (`main()` uses `parse_known_args` → one-or-the-other enforcement → strict re-parse for
  subcommands); `backend add-salad` / `backend remove-salad` subcommands sharing the
  same API methods; delete-by-URL error paths: `salad_url_invalid` (unparseable) vs
  `salad_not_found` (lists known salad backends).
- `local_dispatcher/config.py` — `config_path` recorded on load (needed for the atomic
  rewrite); salad fields on `BackendConfig`. `local_dispatcher/models.py` — salad error
  codes + submit/params validation. `local_dispatcher/daemon.py` — `SaladProxyManager`
  constructed in `serve()`, startup `ensure()` loop for all salad backends, stop message
  "workers and salad proxies left running". `local_dispatcher/scheduler.py` —
  `salad.heartbeat_tick(db)` in `tick()`. `local_dispatcher/adapters/claude.py` —
  worker `ANTHROPIC_BASE_URL` via `worker_endpoint()` (salad → the local proxy, so the
  claude CLI sees a plain Anthropic endpoint). `local_dispatcher/migrations/0002_salad.sql`
  (kind, salad_keyfile, salad_proxy_port, salad_thinking, salad_heartbeat).
- `tests/test_u_salad.py` (9) — gateway normalization, nonce stability,
  config append/remove splice + idempotence, profile-list rewrite (claude-only),
  port allocation, keyfile handling, `_iter_sse` (normal stream + anomaly sentinels:
  non-SSE line, unparseable `data:`, terminal top-level error payload),
  `StreamBuilder` empty-vs-content (empty stream is empty; text or a tool-only turn is
  NOT). `tests/test_i_salad.py` (12 cases) — add-while-stopped (503 path, proxy up,
  nonce match), online→stop→start flips through real ticks, key rejected (403 →
  `key_rejected`), error paths, **a real worker job submitted and `succeeded` with
  `attempt.backend_id` = the salad backend**, heartbeat (completions 0 pre-reservation,
  ≥1 within 15s of a hold job on the real tick path, then cancel), remove
  (config/DB/proxy cleanup + tombstone + `salad_not_found`), orphan-proxy adoption
  (fresh manager re-adopts by nonce, no duplicate), CLI shorthand (id derivation +
  `salad_not_found`), **`test_stream_failure_surfaced` parametrized ×3** (regression for
  `j_8bf0ff2baf`: drives the REAL `salad_proxy.py` subprocess against the fake
  gateway's SSE modes — `stream` → clean finish + reassembled text; `stream_empty` and
  `stream_error_payload` → one `error` event, no `message_stop`, upstream error text
  surfaced in the detail). `tests/fakes/fake_salad_gateway.py`
  — keyed threaded http.server: 503 when stopped, `/v1/models` (llama.cpp shape),
  `/props`, `/v1/completions`, `/v1/chat/completions` (incl. `chat_mode` SSE modes:
  `stream` / `stream_empty` / `stream_error_payload`), call counters.
  `tests/conftest.py` — `SaladProxyManager` wired into the daemon fixture; `stop()`
  calls `salad.stop_all()`; fake gateways stopped with the daemon.
- Docs: `docs/commands.md` ("Salad backends" section: shorthand + full options, add/
  remove semantics, stopped-group = offline not error, 403 = `key_rejected`, proxy
  adoption, heartbeat, 90% ctx), `README.md` (SaladCloud paragraph + config-table row),
  `config.example.toml` (commented salad `[[backends]]` shape reference).

## Changed files (2026-10-17 — status-cap fix + dual-boot config)
- `local_dispatcher/api.py` — `status` response bounded forever: the default view keeps
  active + blocked jobs in full and collapses terminal (succeeded/failed/cancelled) jobs
  to `jobs_terminal: {count, recent: last 5}`; explicit `all_jobs=true` returns the full
  list and is EXEMPT from the 8k response cap (`_dispatch` passes
  `capped = not (method == "status" and params.get("all_jobs"))`; the MCP forwarder
  never sends `all_jobs`, so `dispatch_status` always gets the bounded default).
  Fixes live `ladd status` failure `response too large: 13494 > 8000`.
- `local_dispatcher/cli.py` — `ladd status --all-jobs` flag (note `--json` is global:
  `ladd --json status --all-jobs`).
- `tests/test_u_db_procfs_api.py` (+4) — collapse shape (count + last 5, exact recent
  keys), all_jobs full list, default-with-60-terminal-jobs stays under the 8k cap,
  all_jobs exempt from the cap while the bounded default coexisting still fits.
- `~/.config/local-dispatcher/config.toml` (LIVE, not in repo) + `config.example.toml` —
  corrected dual-boot topology: `ak` = 192.168.0.100 WINDOWS boot, `i7` =
  192.168.0.33 LINUX boot, one physical box, both `enabled=true` (group `tr4`);
  header comment fixed; `cc-worker` `backend_ids = ["tr4-secondary", "ak", "i7"]`.
  Daemon restarted; startup probes + 30s TTL follow the live boot; recheck anytime
  via `ladd workers --refresh` / `ladd backend check <id>` (already existed — no
  scheduler code changed).
- `docs/commands.md` — "View job history" (bounded default + `--all-jobs` cap
  exemption) and "Backend probing and recheck" (probe cadence, forced recheck,
  mid-job stop ⇒ `backend_disconnect` retry / start ⇒ `backend_offline` until probe).

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
- pytest: **153 passed, 0 failed** (2026-10-22, incl. 6 new backend-priority tests)
- pytest: **129 passed, 0 skipped** (2026-10-19) — **20/20 acceptance tests green**
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
- P5: `test_u_salad.py` (9 unit) + `test_i_salad.py` (12 integration cases) green — incl. a real
  worker job on a salad backend end-to-end, the 30s heartbeat asserted on the REAL tick
  path (no monkeypatching: first tick after a reservation beats immediately), and
  `test_stream_failure_surfaced` ×3 driving the REAL proxy subprocess against the fake
  gateway's SSE modes (regression for the live clean-empty-stream failure).
- Unit (57): ctx extraction ×3 probe shapes, `context_pct` math/boundary/unknown (4), claude
  `classify` overflow-threshold (3), packet renderer, result validator + evaluate_attempt decision
  table + recovery-context bounding, migrations, event pruning, procfs identity/liveness, API redaction +
  8k cap + submit-error JSON-serializability (2 regression tests for the ProfileConfig/BackendConfig leak)
  + status bounded view / all_jobs cap exemption (4)

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
5. Housekeeping — **DONE (2026-10-17):** all 18 stale demo worktrees removed
   (verified no active jobs/attempts first; `worktrees/` now empty), `/tmp/cline-seed-test*`
   + `/tmp/cline-cc-test` scratch dirs deleted; 3 stale `/tmp/ladd-e2e` registry entries
   pruned. The 21 `dispatch/*` branches were KEPT (integration still possible — results
   also live in `artifacts/<job>/attempt-1/diff.patch`); artifacts intact (7MB).
   **Integration DONE (2026-10-17, user-requested):** demo smoke results merged
   into demo-repo main — `j_1bd3187818` fast-forwarded to `97240a9` (triple),
   `j_3092d3777e` merged at `946119a` (is_even), `j_a3b9717dda` merged at
   `aea44a6` (cube); all three marked `accepted` via `ladd integrate`; tests kept
   in `tests/test_app.py`; demo-repo suite 5/5 passed on main. `j_bba5405826`
   (Cline) was a duplicate of the triple task with no commit on its branch —
   nothing to merge, not marked integrated.
6. Post-P4 (2026-10-17): **(a) `ladd status` 8k-cap failure — FIXED + verified live**
   (default bounded at 4095 chars with 20 jobs in history; `--all-jobs` returns the
   13494-char full dump, cap-exempt; MCP path bounded; +4 regression tests; suite
   108 passed). **(b) Dual-boot topology corrected per user** — ak=.100 WINDOWS /
   i7=.33 LINUX, one box, both enabled; NO code change needed: startup probes +
   30s TTL + `ladd workers --refresh`/`ladd backend check <id>` already provide the
   requested recheck, and a mid-job backend stop classifies `backend_disconnect`
   (retryable, may relaunch on the other online backend). Verified after restart:
   i7 available/online, ak offline. Mycelium #215 pinned (OVERRIDES-PRIOR).
7. ~~P5 live online smoke~~ ✅ **DONE (2026-10-19) — PASS.** The group booted (gateway
   200); `ladd workers --refresh` flipped the salad row `available` with ctx_limit
   (probed n_ctx × 0.90). First live job `j_8bf0ff2baf` failed as bogus
   `invalid_result`: the upstream answered 200 but the stream ended with zero content
   at the Cloudflare edge's ~100s origin timeout (measured TTFB: ~1.7s small prompt,
   ~93.3s for a ~5K-token worker-shaped prompt) and the old proxy rendered that as a
   clean empty turn. Fixed in `salad_proxy.py` (anomaly sentinels +
   `StreamBuilder.saw_content`/`anomalies` + one `error` event → retryable
   `backend_disconnect`; no adapter change), regression-tested (3 unit + 1
   parametrized real-proxy integration ×3), suite 129/129, live proxy restarted onto
   the fixed code (pid 86927). Re-submitted job **`j_d12f1ae064` SUCCEEDED on
   attempt 1** (~13.5 min: 03:02:44→03:16:24Z, 8 turns, 30K-token input, exact
   requested `halve()` diff in `src/app.py`, worker committed `6df3a7b`, all
   artifacts present, `attempt.backend_id = tamarind-caraway-1rpqcqcnbbkmqdbv`).
   Outcome recorded in `docs/commands.md` (live-smoke line), PROGRESS.md P5 §8–9.
   Backend left registered (the desired state — offline while the group is stopped
   is normal).
8. on_salad `sl_proxy.py` zero-content fix — **DONE (2026-10-19), ported + pushed.**
   User noted the on_salad project (`/dd2/andrei/docker/on_salad/claude/sl_proxy.py`,
   the original bridge this repo's `salad_proxy.py` was vendored from) had the
   identical pre-fix bug, affecting interactive `cl_salad`/`sl_chat.sh` sessions.
   Ported the same 4-part fix verbatim: `_iter_sse` anomaly sentinels,
   `StreamBuilder.saw_content`/`anomalies` + `is_empty()`, `finish(tail=None)`,
   and the streaming-path `anomalies or (not tail and is_empty())` → one `error`
   event. Verified offline (no live group — user stopped it): 12/12 logic checks
   pass over empty/think-only/non-SSE/in-band-error/unparseable/text/tool-only
   shapes; syntax clean; single `finish()` call site updated. `claude/README.md`
   latency note updated to say `sl_proxy.py` now handles it (not "if you
   extend"). Committed on_salad `f500326` (only the 2 changed files; user's
   `PROGRESS.md` left untouched), pushed to origin.
   **Test instructions left for the user (same day):** a deterministic offline
   regression suite `on_salad/tools/test_sl_proxy_offline.py` (**10/10**, no
   group/TLS/money — drives the real `_iter_sse`+`StreamBuilder` over in-memory
   streams; the proxy hard-exits on non-https so this is the logic gate) and
   `on_salad/claude/TESTS.md` (step 0 offline + 3 live steps: `sl_curl.sh`
   baseline → `cl_salad -p "…pong…"` no-over-trigger check → how the edge-kill
   failure now surfaces). `claude/README.md` gained a `TESTS.md` file-table row.
   Committed on_salad `5aa1b51` (only those 3 files; user's `PROGRESS.md`
   untouched), pushed. User runs the **live** steps tomorrow once the group is up.
9. Full-repo code review + fixes — **DONE (2026-10-19), working tree only (no git).**
   High-effort review (8 parallel reviewers + line-by-line + cross-file + sweep)
   over `local_dispatcher/`; all 27 verified findings fixed:
   - **salad lifecycle/config**: proxy nonce now covers model/thinking/token cap/
     key contents (rotated keys or config edits no longer adopt a stale proxy);
     per-backend `ensure()` lock (no duplicate spawns); adopted/orphaned proxies
     are found by nonce prefix in `/proc` and killed (stop works; leaked/hung
     spawns reaped); stale own proxies replaced; spawn errors returned instead of
     crashing startup; duplicate/zero proxy ports rejected at config load.
   - **runtime state**: `sync()` gained `preserve_runtime` (salad add/remove no
     longer undo `ladd backend set`) and `allow_empty` (a mistyped empty config
     no longer deletes/tombstones live rows); `_reload_config` carries runtime
     overrides; `_commit_config` handles relative and symlinked config paths and
     reuses the config it already validated.
   - **TOML editing**: profile `backend_ids` parsing handles multi-line arrays,
     inner and trailing comments, and indented table headers; still idempotent.
   - **proxy correctness**: streaming `message_delta` reports `input_tokens`;
     non-streaming 200 bodies with `error`/no `choices` become 502 errors; legal
     SSE `id:`/`retry:` lines accepted and `error: null` ignored; a complete
     answer with trailing junk is kept, while content without finish_reason/
     `[DONE]` is treated as truncated and retried; a client disconnect stops
     upstream polling; fixed the chunk-boundary newline leak in
     `EmptyThinkStripper` (fuzz-verified).
   - **scheduling**: heartbeat runs as a detached, non-overlapping task (slow
     gateways no longer delay reaping/deadlines/launches); the throttle fires the
     first beat on a young monotonic clock and stamps after the attempt.
   - **adapters/CLI/tests**: cline profiles may not use salad backends (validated
     at load + guarded in `pick_backend`); `-a/-d` reject a stray subcommand;
     `-d` ignores history tombstones and reports "already removed"; SO_REUSEADDR
     on port checks; conftest teardown always reaps proxies; migration test now
     validates numbering/names; dead code removed; `asyncio_default_fixture_loop_scope`
     pinned.
   Tests: **146 passed** (129 before + 17 new regression tests), warnings-as-errors
   run green; the live `~/.config/local-dispatcher/config.toml` validates under
   the new rules. All changes are uncommitted in the working tree.
10. clov auto-registers projects with the dispatcher (2026-10-19). Patched
    `/usr/local/bin/clov` on tr4 and i7 (backups: `clov.bak-2026-10-19`).
    Before exec'ing Claude Code, once per git repo (marker `<repo-root>/.ladd`):
    if the directory is not a repo it prompts `[y/N]` to `git init -b main`;
    then it asks the daemon whether this `repo_path` is already registered,
    otherwise derives an id from the directory name (short hash suffix on
    collision), runs `ladd project add <id> <repo-root>`, writes `.ladd`
    (project id + path), and adds `.ladd` to `.git/info/exclude` so a marker
    cannot be committed and wrongly suppress registration on the other host.
    Presence of `.ladd` skips the step. Daemon/ladd absence or add failure is
    non-fatal: warning + Claude still starts; the next run retries. Tested on
    both hosts with a mocked ladd: fresh repo, rerun, already-registered path,
    id collision, git-init yes/no, and add-failure paths.
    Also: the i7 install moved to **`/dd2/andrei/claude/dispatcher`** (git clone
    of a447edf + venv); systemd unit, user-scope `dispatcher` MCP server and
    `~/.local/bin/ladd` repointed; the temporary `~/claude/dispatcher` copy was
    removed. i7 config stays keyless (`i7-local`, alias `qwen3.8-27b`).
