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
- **Green with fakes (18):** a01–a09, a11–a13, a17–a20
- **P3-gated (4, need real subprocesses):** a10 (real `dispatcherd` SIGKILL → adopt, no duplicate),
  a14/a15/a16 (real `mycelium serve` under `MYCELIUM_CONFIG` → temp store)
- Suite: **72 passed, 4 skipped** (skips = the 4 P3-gated above)

## Current milestone: P3 (recovery / restart / memory / failure injection)
Goal: all 20 acceptance tests green, using real subprocesses. Remaining:
1. stream-json usage tracking + 65% context warning + overflow evidence (per-turn `usage`)
2. checkpoint contract + context-overflow / crash / backend-disconnect recovery
   (`RECOVERY_CONTEXT` = latest checkpoint + bounded diff; **fresh session, never `--resume`**)
3. procfs startup reconcile + quarantine — a10: real `dispatcherd` SIGKILL mid-job → restart →
   adopt (no duplicate), capacity reconciled
4. mycelium client + packet memory injection + envelope + outage degrade + promote
   (a14/a15/a16; real `mycelium serve` under a temp store, user store untouched)
5. Hardening: scope `startup_reconcile` orphan-kill to the instance's own db/lock
   (the global `/proc` marker-scan footgun — see HANDOFF.md "Known footgun")

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
