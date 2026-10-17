# Cline adapter — status and compatibility diagnostics

*Updated 2026-10-17 (P4). Verdict: **interface identified, adapter built and
faked-tested, one real smoke attempted — failed at Cline's provider base-URL
layer, root-caused, fix is on the Cline settings side**. Per PRD §9 the
adapter does not claim Cline automation is complete — that claim requires a
real pass, not yet recorded.*

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

## Real smoke — 2026-10-17 attempt (verbatim, per PRD §9)

Backend gate was opened by the user (tr4-secondary / strata freed,
released via `ladd backend release tr4-secondary`); the model entry had
also been aligned — the stream shows the run reaching the model layer
with the right id (`swift-1.5-iq3_xxs`, provider `openai-compatible`).
**The run failed at the first model call. Job `j_bc96773bee`,
attempt 1, exit code 1, classified `crash`, job `blocked`
(`recovery_exhausted`, max_attempts=1). stream.log tail, verbatim:**

```
{"ts":"2026-10-17T17:44:17.825Z","type":"agent_event","event":{"type":"error","error":{"name":"Error","message":"Incorrect API key provided: local. You can find your API key at https://platform.openai.com/account/api-keys.","stack":"..."},"errorClass":"unknown","recoverable":false,"iteration":1}}
{"ts":"2026-10-17T17:44:17.959Z","type":"run_result","finishReason":"error","iterations":1,"usage":{"inputTokens":0,...,"durationMs":305,"text":"Incorrect API key provided: local. ...","model":{"id":"swift-1.5-iq3_xxs","provider":"openai-compatible"}}
```

No `result.json`, empty `diff.patch` — the packet arrived (argv
contract proven by the faked tests) but the model call never happened.

**Root cause (diagnosed via `strace` on a manually launched Cline with
the same env, NOT guessed):** Cline's `openai-compatible` provider reads
the API key from `OPENAI_API_KEY` (which the dispatcher sets correctly —
strata accepts `Bearer local` and even no key), but takes the base URL
**only from the stored provider config** — its default
`https://api.openai.com`. It ignores `OPENAI_API_BASE` and
`OPENAI_BASE_URL` env vars. Strace of the failing run: 38× TCP :443,
3× DNS :53, **0× :8080**, TLS SNI = `api.openai.com`. The 401 comes
back from OpenAI's real API, not from strata.

**The fix is on the Cline side (user action, `~/.cline` is off-limits
to the dispatcher by redaction policy):** in Cline settings →
Providers → `openai-compatible`, set **Base URL** to
`http://127.0.0.1:8080/v1` (Model `swift-1.5-iq3_xxs`). Until then the
dispatcher-side path is proven end-to-end except the last network hop.
This is the PRD's "report unsupported clearly" path, not a dispatcher
defect; `ClineAdapter.smoke_verified` stays `False`.

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
  **Caveat (strace-verified 2026-10-17):** Cline 3.0.56's
  `openai-compatible` provider reads the key from `OPENAI_API_KEY` but
  the base URL only from its stored provider config — the
  `OPENAI_API_BASE` export is harmless but ineffective there. The base
  URL must live in Cline's provider settings.

## Real smoke protocol (re-run after the Cline Base URL fix)

```sh
# 1. user: Cline settings → Providers → openai-compatible →
#    Base URL = http://127.0.0.1:8080/v1  (Model swift-1.5-iq3_xxs).
#    NOTE: a daemon restart re-asserts the config reservation, so:
# 2. dispatcher: ladd backend release tr4-secondary
# 3. dispatcher: submit + watch, same as before:
ladd mode <project> team
ladd submit "tiny verifiable task" <base-sha> cline-worker --scope src
ladd job <job-id>                          # or watch the artifacts:
#    ~/.local/share/local-dispatcher/artifacts/<job>/<n>/stream.log
# 4. record the outcome here VERBATIM (exit code, stdout tail, result.json)
#    — pass or fail. Never summarize a pass that didn't happen.
#    On a real pass: flip ClineAdapter.smoke_verified = True.
# 5. re-reserve immediately:
ladd backend reserve tr4-secondary --note "Cline interactive session"
```

Until a real pass is recorded in this file, `ClineAdapter.smoke_verified`
stays `False` in code and the Claude Code adapter remains the supported
worker path (PRD §15: do not delay a working release for unavailable Cline
automation).
