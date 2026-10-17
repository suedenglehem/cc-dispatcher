# Cline adapter — status and compatibility diagnostics

*Updated 2026-10-17 (P4). Verdict: **interface identified, adapter built and
faked-tested, NOT real-model-verified**. Per PRD §9 the adapter does not
claim Cline automation is complete — that claim requires a real smoke,
which is gated (below).*

## Verified on this machine (evidence)

- Cline **3.0.56** at `/usr/local/bin/cline` (`cline --version`, 2026-10-17).
- The headless interface exists; the full flag set was verified against
  `cline --help` (read-only — no network calls, no config/data-dir reads):
  - the prompt is a **positional argument**; a bare prompt starts in
    **act mode with auto-approve** enabled (the unattended shape workers need);
  - `-p/--plan` is **PLAN mode** (read-only planning) — never for workers;
  - `--json`, `--auto-approve <bool>`, `--retries [n]`,
    `--thinking none|low|medium|high|xhigh`,
    `--compaction agentic|basic|off`, `-P/--provider`, `-m/--model`,
    `-t/--timeout <s>`, `--config <path>` (default `~/.cline`),
    `--data-dir <path>` (default `~/.cline/data`), `--worktree`.
- `local_dispatcher/adapters/cline.py` is built using only verified flags.
  `tests/test_i_cline_adapter.py` (4 tests, green) drives it through the
  real daemon — build/spawn/stream/result-validation — with a fake binary
  speaking Cline's argv-prompt contract (`FAKE_PROMPT_FROM=argv`).

## Not verified (why this is not "done")

No real model-level smoke on a worker backend, for two independent gates:

1. **The backends are not free.** tr4-secondary (`:8080`) is
   `manually_reserved` for the live Cline interactive session; i7
   (`192.168.0.100:8080`) is `enabled=false` for the second interactive
   Claude session. Live smokes run only on a backend the user explicitly
   frees, and it is re-reserved immediately after.
2. **Cline's stored model entry ≠ the served model** (checked 2026-10-16).
   The `openai-compatible` provider points at `:8080/v1`, but its stored
   model id does not match what llama.cpp serves (`swift-1.5-iq3_xxs`).
   Until the user updates that entry, a run fails at the first model call.
   That failure is the expected, correctly-reported "unsupported" outcome —
   the PRD's "report unsupported clearly" path, not a dispatcher defect.

## Adapter design (what a live smoke would exercise)

```
cline --json --cwd <worktree> --auto-approve true --retries 1
      --thinking low --compaction basic -t <remaining job budget>
      --data-dir <artifact_dir>/cline-data
      --provider openai-compatible --model <served id>  <task packet>
```

- **Isolation semantics (discovered at implementation time):**
  - `--data-dir <artifact_dir>/cline-data` — per-attempt runtime state.
    Workers never write into `~/.cline/data`, so the live interactive
    session's state is untouched.
  - `--config` stays at the default `~/.cline`: the provider's baseUrl is
    resolvable only from the user's config, and Cline reading its own
    config is normal operation. The **dispatcher never reads
    `~/.cline/data`** (it holds a provider key); the key stays inside the
    Cline process and API-layer redaction keeps artifacts clean.
  - No `--worktree` — the dispatcher owns worktree placement via `--cwd`.
  - No `-s` system-prompt override — Cline's native system prompt is tuned
    to its own tool protocol; the worker policy and result contract travel
    inside the task packet instead.
- **No usage channel:** `--json` messages have no stable token-usage field
  across versions, so `context_pct` stays `unknown` for cline attempts
  (PRD: never fabricate). Overflow classification falls back to
  error-text patterns (`token limit` / `exceed` / `context_length`), and
  `-t` + the supervisor deadline are the context/budget backstops.
- **env:** explicit `OPENAI_API_BASE` (= backend `openai_base_url` or
  `<endpoint>/v1`) + `OPENAI_API_KEY` from the backend's
  `credential_env` (default `local`); never the primary session's env.

## Real smoke protocol (run only when both gates are open)

```sh
# 1. user: close the interactive session occupying the backend, then:
ladd backend release tr4-secondary          # or: ladd backend enable i7
# 2. user: make Cline's openai-compatible model entry match the served
#    model id (Cline settings → Providers → openai-compatible → Model),
#    e.g. swift-1.5-iq3_xxs for tr4-secondary
# 3. dispatcher: a cline profile (see config.example.toml) + a job:
ladd mode <project> team
ladd submit "tiny verifiable task" <base-sha> cline-worker --scope src
ladd job <job-id>                          # or watch the artifacts:
#    ~/.local/share/local-dispatcher/artifacts/<job>/<n>/stream.log
# 4. record the outcome here VERBATIM (exit code, stdout tail, result.json)
#    — pass or fail. Never summarize a pass that didn't happen.
# 5. re-reserve immediately:
ladd backend reserve tr4-secondary --note "Cline interactive session"
```

Until a real pass is recorded in this file, `ClineAdapter.smoke_verified`
stays `False` in code and the Claude Code adapter remains the supported
worker path (PRD §15: do not delay a working release for unavailable Cline
automation).
