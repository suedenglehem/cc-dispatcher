# Setup

Single daemon (`dispatcherd`), one user, one machine. Installable,
restartable, inspectable via `ladd` and the MCP forwarder. Nothing here
touches `~/.cline`, `~/.mycelium`, or running interactive sessions.

## Install

```sh
cd /dd2/andrei/claude/dispatcher
./install.sh
```

What it does (safe to re-run):
1. venv at `.venv` + editable install → console scripts `local-dispatcher`
   (daemon), `ladd` (CLI), `dispatcher-mcp` (MCP forwarder).
2. `~/.config/local-dispatcher/config.toml` from `config.example.toml`
   **only if absent** — never overwrites an existing config.
3. systemd **user** unit at `~/.config/systemd/user/local-dispatcher.service`
   (ExecStart rewritten to the venv path) + `systemctl --user daemon-reload`.

Survive logout (one-time): `loginctl enable-linger $USER`. Without linger
the user session (and the daemon) dies at logout; restart reconciliation
makes any later start safe (see recovery.md), but jobs pause.

## Start / stop

```sh
systemctl --user start local-dispatcher
systemctl --user status local-dispatcher
ladd status
```

**SIGTERM semantics (deliberate):** `systemctl --user stop` (or SIGTERM)
stops the scheduler and the API but **leaves worker processes running** —
they are re-adopted on the next start, never killed and never duplicated.
Hard stop for a specific job: `ladd cancel <job>`.

## Configuration

`~/.config/local-dispatcher/config.toml` (start from
`config.example.toml`, which encodes the verified TR4 topology):

- `[state]` — state root, `worktree_root` (point at a big disk if wanted),
  socket path.
- `[daemon]` — tick, probe TTL, kill grace/confirm, min start budget
  (60s), response cap (8000 chars), `context_warn_pct` (0.65 → one-shot
  `context_warning` event), `context_overflow_usage_pct` (0.95),
  `diff_cap_bytes` (40960), `log_tail_lines`.
- `[worker]` — defaults for deadlines/limits, executables
  (`claude_executable`, `cline_executable`), `allowed_tools` allowlist,
  env derivation (90% of probed context, `CLAUDE_CODE_MAX_OUTPUT_TOKENS`,
  autocompact pct), `anthropic_api_key = "local"`,
  `permission_mode = "bypassPermissions"`.
- `[mycelium]` — **optional**; the daemon spawns a `mycelium serve` stdio
  child **only if this section is present**. Absent section ⇒ memory
  injection off, and the client can never start against your
  `~/.mycelium`.
- `[[resource_groups]]` — total concurrency caps across member backends.
- `[[backends]]` — `endpoint`, `model_id` (served id), `model_label`
  (what `--model` receives), `is_primary` (never worker-scheduled),
  `manually_reserved`, `enabled`, `concurrency`, `resource_group`,
  `ctx_override`.
- `[[profiles]]` — `adapter` (`claude`|`cline`), `executable`, `args`,
  `env_overrides`, `backend_ids`, `allowed_tools`, `deadline_s`,
  `max_attempts`, `output_limit_chars`, `allow_test_repair`.
- `[[projects]]` — `id`, `repo_path`, `integration_ref`,
  `mycelium_project`; or add at runtime with `ladd project add`.

**Safety property:** runtime `ladd backend ...` changes are **live-only**.
On the next daemon start this file is the source of truth and reasserts
the reserved/disabled states. If you release a backend for a demo,
re-reserve it — and know that a daemon restart will re-reserve it
anyway (or remove the release from the file if you changed it).

## First run

```sh
ladd project list                                   # from config or:
ladd project add demo /dd2/andrei/claude/dispatcher/demo-repo
ladd workers                                        # who is free RIGHT NOW
ladd backend list
ladd mode demo team                                 # only when a worker backend is free
ladd submit "add a --version flag to src/app.py" <base-sha> cc-worker \
    --scope src --criterion "python -m src.app --version prints a version"
ladd job <job-id>
ladd events --limit 20
# review the worktree branch, merge it yourself, then:
ladd integrate <job-id> --commit <merge-sha>
ladd promote <job-id>                               # optional: pinned [decision] memory
```

## MCP (primary session)

```sh
claude mcp add dispatcher -- /dd2/andrei/claude/dispatcher/.venv/bin/dispatcher-mcp
```

Eight tools: `dispatch_status`, `dispatch_set_mode`, `dispatch_submit`,
`dispatch_job`, `dispatch_cancel`, `dispatch_resume`, `dispatch_workers`,
`dispatch_integrate`. The forwarder is stateless (strips inherited env,
talks the same Unix socket the CLI uses); workers get only Mycelium
tools, never these.

## Backend enablement protocol (hard rule)

The two worker backends are reserved/disabled because live interactive
clients bypass dispatcher limits. Enable one **only** when the user has
freed it, and re-reserve/disable it right after:

```sh
ladd backend release tr4-secondary   # was: manually_reserved
ladd backend enable i7               # was: enabled=false
# ...demos...
ladd backend reserve tr4-secondary --note "Cline interactive session"
ladd backend disable i7
```

Before enabling i7, run `ladd backend check i7` — the served id
(`local-metrics`) may be labeled differently than configured; a 1-token
check catches a label-case mismatch before it burns a job.
