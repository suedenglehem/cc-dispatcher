# Local Coding Agent Dispatcher

A small, deterministic local orchestration service for running **independent
supporting coding tasks as supervised worker agents** while the primary
session keeps working.

The primary session (a Claude Code — or Cline — session on this machine)
switches a project between **single** and **team** mode. In team mode, jobs
are executed by **fresh headless worker sessions** (Claude Code or Cline)
running on the machine's spare local inference backends (vLLM / llama.cpp).
Capacity, isolation, durability, and recovery are enforced **in code**
(scheduler, SQLite, subprocess supervisor, git worktrees); the agents do the
reasoning.

Built to the binding spec in `local-agent-dispatcher-prd.md` (§5–15; §17–21
shipped verbatim as `prompts/`). Status: **complete** — all 20 PRD §14
acceptance tests green, both real-model live smokes passed (Claude Code and
Cline adapters).

## What it does, in one picture

```
primary session ──(MCP: dispatch_*)──> dispatcher-mcp (stdio→socket forwarder)
                                         │
                                   dispatcherd (asyncio daemon, flock, SQLite WAL)
                                   ├─ scheduler: 1s tick, launch gate, deadlines, probes
                                   ├─ supervisor: spawn/attach/reap, process-group kills
                                   ├─ worktrees:  git worktree per job, branch dispatch/<job>, scope check
                                   ├─ mycelium:   one stdio child, recall → task-packet injection
                                   └─ api:        newline-JSON over Unix socket (8k caps, redaction)
                                         │
                    ┌────────────────────┴────────────────────┐
              claude -p worker                        ccline worker
        (worktree, isolated env, mycelium MCP,      (Cline headless, per-run
         stream-json, result JSON contract)          --data-dir + seeded baseUrl)
                                         │
                    backends: tr4-primary (never worker-scheduled)
                              tr4-secondary, i7/ak … (worker capacity, probed)
```

Core guarantees (enforced in the daemon, not by convention):

- **No-new-launch** — a launch commits in one `BEGIN IMMEDIATE` transaction
  that re-reads the project mode, so a launch can never commit after
  single-mode does (PRD §7.4).
- **Restart reconciliation** — after a crash, live worker processes are
  **re-adopted, never re-spawned** (no duplicate execution); capacity that
  can't be confirmed is quarantined, not guessed.
- **Context-overflow recovery** — overflow/crash retries get a
  checkpoint + bounded diff (`RECOVERY_CONTEXT`), fresh session, bounded
  attempts, then `blocked` for a human decision.
- **Reservations** — backend and resource-group capacity is held in the DB
  and released only after confirmed process-group termination.
- **Primary backend is never worker-scheduled.**
- **Bounded permissions** — workers get an explicit env (never the primary
  session's env), a strict tool allowlist, only the mycelium MCP (no
  dispatcher tools, no memory-maintenance tools), and a deadline.

## Requirements

- Linux, Python 3.12, a systemd user session
- `claude` CLI (headless, `-p`) — for Claude Code workers
- `cline` CLI (headless, `--json`) — for Cline workers; the repo ships
  `bin/ccline`, a launcher that seeds the provider baseUrl into each run's
  isolated `--data-dir` (Cline reads the baseUrl only from stored config —
  see `docs/cline-status.md`)
- Local inference backends exposing OpenAI-compatible `/v1/models`
- Optionally: Mycelium memory (`mycelium serve`) for shared memory injection
  into task packets — **off unless configured**
  https://github.com/constant-itis/mycelium-memory

## Install

```sh
cd /dd2/andrei/claude/dispatcher
./install.sh
```

Safe to re-run. It: (1) creates `.venv` + editable install → console scripts
`local-dispatcher` (daemon), `ladd` (CLI), `dispatcher-mcp` (MCP forwarder);
(2) copies `config.example.toml` to
`~/.config/local-dispatcher/config.toml` **only if absent**; (3) installs the
systemd **user** unit + `daemon-reload`; (4) copies `bin/ccline` to
`/usr/local/bin/ccline`. It never touches `~/.cline`, `~/.mycelium`, or
running interactive sessions.

Survive logout (one-time): `loginctl enable-linger $USER`. Without linger the
daemon dies at logout; restart reconciliation makes any later start safe, but
running jobs pause until then.

## Configuration

`~/.config/local-dispatcher/config.toml` (start from `config.example.toml`,
which encodes the verified TR4 topology). Sections:

| Section | What it sets |
|---|---|
| `[state]` | state root, `worktree_root` (point at a big disk if wanted), socket path |
| `[daemon]` | tick, probe TTL, kill grace/confirm, min start budget (60s), 8000-char response cap, `context_warn_pct` (0.65), `context_overflow_usage_pct` (0.95), diff cap (40KB), log tail |
| `[worker]` | deadline/attempt defaults, `claude_executable` / `cline_executable` (default `ccline`), tool allowlist, env derivation (90% of probed context, output tokens, autocompact pct), `anthropic_api_key = "local"`, `permission_mode = "bypassPermissions"` |
| `[mycelium]` | **optional** — the daemon spawns a `mycelium serve` child only if this section is present; absent ⇒ memory injection off |
| `[[resource_groups]]` | total-concurrency caps across member backends |
| `[[backends]]` | `endpoint`, `model_id` (served id), `model_label` (the `--model` label), `is_primary`, `enabled`, `manually_reserved`, `concurrency`, `resource_group` |
| `[[profiles]]` | per-profile adapter, extra args, `backend_ids`, deadlines, limits |
| `[[projects]]` | `repo_path`, `integration_ref` (default `main`), `mycelium_project` — or add at runtime with `ladd project add` |

Backend safety model: backends that serve **interactive** sessions ship
`manually_reserved` or `enabled = false`. Runtime changes via
`ladd backend …` apply to the live daemon only; **on the next daemon start
the config file is the source of truth and reasserts the safe state.**
Release a backend for work only when its interactive client is closed:

```sh
ladd backend release tr4-secondary      # when Cline is closed
ladd backend reserve tr4-secondary --note "Cline interactive session"   # after
```

## Run

```sh
systemctl --user start local-dispatcher
ladd status
```

**SIGTERM semantics (deliberate):** `systemctl --user stop` stops the
scheduler and API but **leaves worker processes running** — they are
re-adopted on next start, never killed, never duplicated. Hard stop for a job:
`ladd cancel <job>`.

### Day-to-day workflow (CLI)

```sh
ladd project add myrepo /path/to/repo        # or [[projects]] in config
ladd mode myrepo team                        # only when a worker backend is free
ladd submit "add function X with tests" <base-sha> cc-worker --scope src --scope tests
ladd job <job-id>                            # bounded view: status, artifacts, result
ladd integrate <job-id> --commit <sha>       # after YOU merged the worker branch
ladd promote <job-id>                        # save the pinned [decision] memory
ladd mode myrepo single                      # drain: queued paused, running finish
```

The primary is the **integration authority**: after the primary/user merges
the worker's `dispatch/<job-id>` branch, `ladd integrate` records the
commit — dependent jobs launch from it and launch only after it exists.
`docs/commands.md` is the full command reference (PRD §21 text).

### Day-to-day workflow (MCP, from the primary session)

Register the forwarder once (user scope):

```sh
claude mcp add dispatcher -- .venv/bin/dispatcher-mcp
```

The primary session then gets eight stateless tools: `dispatch_status`,
`dispatch_set_mode`, `dispatch_submit`, `dispatch_job`, `dispatch_cancel`,
`dispatch_resume`, `dispatch_workers`, `dispatch_integrate`. Workers get
**no dispatcher tools** — only the mycelium memory tools.

## State, artifacts, logs

Everything lives under `~/.local/share/local-dispatcher/`:

```
dispatch.db                     # SQLite (WAL): projects, backends, jobs, attempts, reservations, events
artifacts/<job>/attempt-N/      # stream.log, result.json, checkpoint.json, diff.patch
                                # (+ cline-data/ for Cline attempts)
worktrees/<job-id>/             # one worktree per job, branch dispatch/<job-id>
run/dispatcher.sock, dispatcher.lock
```

Events are an audit trail with bounded retention: `ladd events`.
`ladd reconcile` releases quarantined reservations after a bad restart.

## Tests

```sh
.venv/bin/python -m pytest            # 104 passed / 0 skipped (fakes; no backends needed)
```

`tests/test_a01…a20.py` map 1:1 to the 20 PRD §14 acceptance tests
(no-overlap, drain, no-silent-resume, overflow recovery, restart-without-
duplicate, scope flagging, no-new-launch, …). Real-model smokes are
opt-in, backend-freeing-gated, and recorded verbatim in `docs/`.

## Docs map

| Doc | Contents |
|---|---|
| `docs/setup.md` | install, start/stop, config reference, linger |
| `docs/commands.md` | full `ladd` + MCP reference (PRD §21) |
| `docs/recovery.md` | restart reconciliation, quarantine, reconcile |
| `docs/troubleshooting.md` | failure classes, what they mean, what to do |
| `docs/cline-status.md` | Cline adapter compatibility + verbatim smoke evidence |
| `docs/mycelium-notes.md` | memory integration, envelope format, outage behavior |
| `prompts/` | worker policy, task-packet template, result schema (PRD §17–19) |
| `HANDOFF.md`, `PROGRESS.md` | build history, decisions, current state |

## Safety notes

- The dispatcher **never reads or echoes** `~/.cline/data` (it holds a
  provider secret); Cline workers run with a per-attempt `--data-dir`.
- Workers never receive credentials beyond the backend's local key, never
  the primary session's env, and have no deployment credentials.
- Mycelium is a memory store only — never the queue, never the lock; the
  queue and locks are the daemon's SQLite.
- Per PRD §10, worker confinement is **validation, not OS isolation**
  (restricted tools + explicit env + bounded deadlines); the documented
  trade-off, accepted for a single-user local machine.
