# Recovery: crashes, restarts, quarantines

The dispatcher's core promise: **no duplicate execution, no lost result,
no capacity leak** — across daemon crashes, worker crashes, and
context-overflow failures. Everything below is enforced in code and
covered by the acceptance suite (20/20 green).

## Daemon crash or restart

**SIGTERM** (`systemctl --user stop`): scheduler + API stop, **workers
keep running** (own process groups). Next start re-adopts them.

**SIGKILL / power loss / OOM**: on next start, `startup_reconcile` runs
**before the socket comes up**. It scans `/proc/*/environ` for
`DISPATCHER_ATTEMPT_ID` marker processes and, per live marker:

| Case | Action | Event |
|---|---|---|
| pid+pgid+starttime matches a committed attempt | **re-adopt** (attach, never re-spawn) | `re_adopted` |
| pid gone, attempt row open | finalize: valid on-disk result ⇒ job **succeeded**; else `interrupted` (`exit_unknown`) + capacity released | `reconciled` / `released_after_reconcile` |
| pid alive but starttime mismatch (PID reuse) | attempt finalized `interrupted`, reservation **quarantined** — never kill a stranger | `quarantined_pid_reuse` |
| live marker, no matching attempt, **own** instance | orphan: SIGTERM → grace → SIGKILL the group | `orphan_killed` |
| live marker belonging to **another** instance (`DISPATCHER_STATE_DIR` tag, or no tag + no row in this db) | **never killed** | `foreign_marker_ignored` |

Ownership scoping (the multi-instance footgun, closed in P3): workers are
tagged `DISPATCHER_STATE_DIR=<state dir>` at spawn; two instances with
different state dirs never kill each other's workers.

Re-adopted children are judged by their **on-disk report** when they
finish after their supervisor died — a valid `result.json` is still
success on the *same* attempt (no re-run).

## Worker failure recovery

Attempt failure classes (see troubleshooting.md for the full table).
**Retryable**: `context_overflow`, `crash`, `backend_disconnect`,
`exit_unknown`. On retry (up to `max_attempts`, **same worktree/branch,
fresh session — never `--resume`**) the packet gains a recovery section:

- latest **valid checkpoint** + bounded diff (≤ `diff_cap_bytes`, 40KB);
- no checkpoint ⇒ log tail + `uncertain: true`;
- exhaustion ⇒ job `blocked(recovery_exhausted)` — a human decision,
  not a silent loop.

**Not** auto-retried: `invalid_result` (exit 0 without a valid report —
`ladd resume` after you inspect), `tests_failed` (unless the profile has
`allow_test_repair`), `scope_violation`, `deadline_exceeded`.

Context-overflow detection: error text patterns first, else
`usage.input_tokens ≥ context_overflow_usage_pct × ctx_limit` from the
stream-json usage channel. At `context_warn_pct` (default 0.65) the
running attempt emits a one-shot `context_warning` event and the live
job view shows `context_pct`.

## Quarantine and reconcile

A quarantined reservation is capacity held **uncertain** (process
identity unprovable). It is never released automatically, because
releasing capacity a stranger might still be using is how limits get
broken.

```sh
ladd workers        # shows quarantined reservations
ladd reconcile      # releases a quarantined reservation ONLY once the
                    # process group is confirmed dead
```

## Blocked jobs (a decision is needed, not a retry)

| `status_detail` | Meaning | Unblock |
|---|---|---|
| `recovery_exhausted` | retryable failures ran out attempts | inspect artifacts; fix & `ladd resume`, or cancel |
| `base_diverged` | project's `integration_ref` is not an ancestor of the job's base | re-submit against the new base (PRD §21) |
| `dependency_failed` | a dependency job failed | fix/cancel the dependency, re-submit dependent |
| `deadline_before_start` | < `min_start_budget_s` (60s) of budget left to start | extend the deadline / cancel |
| `memory_required_unavailable` | `memory_required=1` job but the Mycelium child died twice | fix Mycelium, `ladd resume` |

Queued (not blocked) `status_detail` values that clear on their own:
`no_capacity`, `backend_offline`, `waiting_dependency`,
`waiting_integration`.

## Modes and pauses

- `single` mode **pauses** queued jobs (`drain` policy lets running ones
  finish). Team mode never auto-resumes paused jobs — `ladd resume
  <job>` is explicit and **re-validates the base** first
  (no-silent-resume is acceptance test 8).
- Cancel: `ladd cancel <job>` kills the running attempt's process group
  (SIGTERM → grace → SIGKILL), releases capacity, preserves artifacts.

## Manual drill (run after any daemon update)

```sh
# mid-job:
kill -9 $(systemctl --user show -p MainPID --value local-dispatcher)
systemctl --user start local-dispatcher
ladd status                 # running job should be re-adopted, not re-spawned
ladd events --limit 20      # expect re_adopted / reconciled, no duplicate launches
ladd reconcile              # if anything is quarantined
```

Oracle for "no duplication": the worker's launch log (see
`FAKE_LAUNCH_LOG` in tests) or `ladd events` — exactly one `launched`
per attempt, stable pid across the restart.
