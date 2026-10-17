# Cline adapter — status and compatibility diagnostics

*Updated 2026-10-17 (P4). Verdict: **COMPLETE — real model-level smoke
PASSED (job `j_bba5405826`, 2026-10-17, strata `swift-1.5-iq3_xxs`);
`ClineAdapter.smoke_verified = True`.** The earlier smoke failure (Cline's
provider base-URL layer) was root-caused via strace and fixed
dispatcher-side by the `ccline` launcher (`bin/ccline` →
`/usr/local/bin/ccline`), which the passing smoke exercised end-to-end.
All outcomes recorded verbatim per PRD §9.*

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
  `tests/test_i_cline_adapter.py` (6 tests, green) drives it through the
  real daemon — build/spawn/stream/result-validation — with a fake binary
  speaking Cline's argv-prompt contract (`FAKE_PROMPT_FROM=argv`), plus
  two tests that run the real `bin/ccline` wrapper against a fake `cline`
  shim (auth args + unchanged-argv forwarding).

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

**The fix — dispatcher-side, the `ccline` launcher (proven, 2026-10-17):**
worker runs no longer depend on the user's `~/.cline` provider settings
at all (and the dispatcher still never reads them). The profile's
executable is now `ccline` (`bin/ccline` → `/usr/local/bin/ccline`,
installed by `install.sh`), which, for each attempt:

1. runs `cline auth openai-compatible -b "$OPENAI_API_BASE" -m <model>
   -k "$OPENAI_API_KEY" --data-dir <artifact_dir>/cline-data` — the
   sanctioned baseUrl setter — writing `settings.providers
   .openai-compatible.{baseUrl, model, apiKey}` into the run's own
   isolated data-dir (auth output → `cline-data/ccline-auth.log`);
2. `exec cline <original argv unchanged>`.

**Why the env var was not enough — and why seeding the file by hand
is not either (all strace-proven in scratch data-dirs, `~/.cline`
untouched):**

| Attempt | What dialed | Outcome |
|---|---|---|
| env `OPENAI_API_BASE` only | 38× :443, SNI `api.openai.com`, 0× :8080 | 401 from OpenAI's real API (the failed smoke above) |
| hand-written `providers.json` with baseUrl + `--provider/--model` flags | 44–46× :443, 0× seeded port | Cline's startup save **replaces** the settings with the flag-derived `{provider, model}` — baseUrl clobbered |
| hand-written file, no flags | `cline` provider (Cline-account auth) | "Unauthorized … re-authenticate your Cline account" |
| **`cline auth`-seeded file (what ccline does)** | **3× the seeded URL** | "Cannot connect to API … (ConnectionRefused)" against a deliberately refused port — i.e. the run **dialed the configured baseUrl**, and the setting survived the run's own save |

The oracle run (through `/usr/local/bin/ccline`, real cline 3.0.56,
`OPENAI_API_BASE=http://127.0.0.1:9/v1` — nothing listens on :9): exit 1,
3 connects to :9, 0× :8080; post-run `providers.json` still held
`baseUrl: http://127.0.0.1:9/v1` + `apiKey: local`. With strata's real
base URL (`http://127.0.0.1:8080/v1`) the same path reached the model —
see the passing smoke below.

## Real smoke — PASS (2026-10-17, job `j_bba5405826`)

Gate opened by the user (tr4-secondary / strata freed); released via
`ladd backend release tr4-secondary`, one-token probe confirmed
`swift-1.5-iq3_xxs` online, then a tiny job submitted on the
`cline-worker` profile — same task shape as the ak Claude Code smoke
(`triple(a)` + `test_triple`). tr4-secondary was re-reserved
immediately after.

- Attempt `j_bba5405826-a1`: **exit code 0**, started 19:37:17Z → ended
  19:39:02Z (~1m45s). `ccline-auth.log`, verbatim: `Provider configured:
  openai-compatible (swift-1.5-iq3_xxs)`. The run's `providers.json`
  held `baseUrl: http://127.0.0.1:8080/v1` — the model call dialed
  strata, not api.openai.com.
- `stream.log` final line (fields, verbatim): `{"type":"run_result",
  "finishReason":"completed","iterations":15,"usage":{"inputTokens":142845,
  "outputTokens":4274,"cacheReadTokens":133136,"cacheWriteTokens":0,
  "totalCost":0},"durationMs":101039,"text":"Task complete. ... added
  the function following the exist..."}` — 15 real tool-use iterations
  against the model.
- `result.json` (valid, PRD §20): outcome `completed`; changed
  `src/app.py` + `tests/test_app.py`; test `.venv/bin/python -m pytest
  -v` → `passed`; criterion `met` ("pytest -v: tests/test_app.py::
  test_triple PASSED; 3 passed in 0.01s"); `commit: null` — the worker
  left the changes uncommitted; the dispatcher captured them in
  `diff.patch` (exactly the two in-scope files).
- Job: `succeeded`, `status_detail: scope_review_required` — the worker
  created a `.venv/` in the worktree (system pip is PEP 668-managed) to
  run pytest, and the scope check correctly flagged all 1982 `.venv/**`
  paths as out-of-scope; **zero violations outside `.venv/`**. That is
  the a18 semantics working as designed: the result stands, the primary
  reviews the flag. (To keep the flag clean, a profile could ship a
  pre-provisioned venv or add `.venv` to the allowed scope.)

**`ClineAdapter.smoke_verified = True`** (flipped on this real pass, per
PRD §9).

## Adapter design (exercised by the live smoke)

```
ccline --json --cwd <worktree> --auto-approve true --retries 1
       --thinking low --compaction basic -t <remaining job budget>
       --data-dir <artifact_dir>/cline-data
       --provider openai-compatible --model <served id>  <task packet>
       # (ccline = `cline auth openai-compatible -b $OPENAI_API_BASE ...`
       #  into the run's data-dir, then exec cline with this argv)
```

- **Isolation semantics (discovered at implementation time):**
  - `--data-dir <artifact_dir>/cline-data` — per-attempt runtime state
    **and** the provider config location: `ccline` seeds
    baseUrl/model/key there via `cline auth` before the run. Workers
    never write into (or need) `~/.cline/data`, so the live interactive
    session is fully self-contained and untouched.
  - `--config` stays at the default `~/.cline`; the **dispatcher never
    reads `~/.cline/data`** (it holds a provider key) — the worker's key
    travels in its env and is stored only under the per-attempt artifact
    dir; API-layer redaction keeps artifacts clean.
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
  `<endpoint>/v1`; for strata that is `http://127.0.0.1:8080/v1`) +
  `OPENAI_API_KEY` from the backend's `credential_env` (default
  `local`); never the primary session's env. Both are consumed by the
  `ccline` launcher, which stores them in the run's data-dir — Cline
  3.0.56 itself reads the base URL only from that stored config
  (strace-verified 2026-10-17), so the wrapper is load-bearing, not a
  convenience.

## Real smoke protocol (first run done — kept as re-run reference)

First run 2026-10-17, PASSED (above). The base-URL blocker is gone
(ccline seeds it per attempt; no change to the user's Cline UI is
needed — that config is for the interactive session only).

```sh
# 1. user frees tr4-secondary (strata, http://127.0.0.1:8080):
#    dispatcher: ladd backend release tr4-secondary
#    (a daemon restart re-asserts the config reservation, so release
#     AFTER any restart)
# 2. submit + watch:
ladd mode <project> team
ladd submit "tiny verifiable task" <base-sha> cline-worker --scope src
ladd job <job-id>                          # or watch the artifacts:
#    ~/.local/share/local-dispatcher/artifacts/<job>/<n>/stream.log
#    (plus <n>/cline-data/ccline-auth.log for the provider setup step)
# 3. record the outcome here VERBATIM (exit code, stdout tail, result.json)
#    — pass or fail. Never summarize a pass that didn't happen.
#    On a real pass: flip ClineAdapter.smoke_verified = True.
# 4. re-reserve immediately:
ladd backend reserve tr4-secondary --note "Cline interactive session"
```

A real pass is now recorded in this file; `ClineAdapter.smoke_verified`
is `True`. The Cline adapter is a supported worker path alongside Claude
Code (PRD §15).
