# Troubleshooting

## Where to look

```sh
ladd status                 # projects, jobs, workers, reservations
ladd job <id>               # status, status_detail, failure_class, context_pct
ladd events --limit 50      # audit trail: submitted/launched/finished/quarantined/...
ladd workers                # backend health + active/quarantined reservations
ls ~/.local/share/local-dispatcher/artifacts/<job>/<n>/
#   stream.log  checkpoint.json  result.json  diff.patch
```

`status_detail` on a non-running job tells you *why it isn't running*
(see recovery.md for blocked vs queued values).

## Failure classes

| `failure_class` | Symptom | Daemon behavior | You |
|---|---|---|---|
| `context_overflow` | model hit its context limit mid-task (text patterns or `usage ≥ 95%` of `ctx_limit`) | retryable: same worktree, fresh session, checkpoint+diff in the packet | nothing, unless it exhausts → `recovery_exhausted` |
| `crash` | non-zero exit, no overflow/disconnect evidence | retryable | check `stream.log` tail if it repeats |
| `backend_disconnect` | connection-refused / fetch-failed evidence | retryable | `ladd backend check <id>`; is the backend actually up? |
| `exit_unknown` | re-adopted child died without a valid report (daemon was killed mid-attempt) | retryable — the process is confirmed dead, so a retry cannot duplicate | nothing |
| `invalid_result` | exit 0 but no valid `result.json` at `RESULT_PATH` | **not** auto-retried | read `stream.log` + `result.json`; fix the task, `ladd resume` |
| `tests_failed` | worker reported `outcome=failed` (its tests fail) | not retried unless profile `allow_test_repair = true` | review the diff; the code may be wrong, or the criterion too strict |
| `scope_violation` | changed files outside `allowed_scope` | job fails; `scope_violations` recorded on the job; worktree preserved | tighten the task or the scope; re-submit |
| `deadline_exceeded` | budget ran out while running | attempt killed at expiry; job fails | raise `deadline_s` and re-submit |
| `spawn_failed` | Popen itself failed (bad executable, env, worktree) | job back to `queued(spawn_failed)` | check profile `executable`, `[worker]` section |
| `cancelled` | `ladd cancel` | process group killed, capacity released, artifacts kept | — |
| `orphaned` | startup reconcile found an unmatchable own marker that also died during the scan | finalized, capacity released | — |

## Operational issues

**"Address already in use" / daemon won't start (exit 1, flock).**
Another `dispatcherd` holds `run/dispatcher.lock`. That is correct
behavior (no competing schedulers). Find it:
`systemctl --user status local-dispatcher`; if the lock is stale after a
crash, the lock is released with the process — a stale *socket* file is
unlinked on start, not a problem.

**Backend shows offline / probe fails.**
`ladd backend check <id>` forces a probe and prints the row. Then
`curl -s <endpoint>/v1/models`. llama.cpp/vLLM down, model unloaded, or
wrong port. Jobs stay `queued(backend_offline)` and launch when it
returns — nothing to fix in the dispatcher.

**i7 "model not found" after enable.**
The served id (`local-metrics`) and the configured `model_label`
(`qwen3.8-27b`) can disagree in case. Fix the label in the config (or
runtime via the backend row) and re-`check`; a 1-token check is the
cheap verification (PRD risk item).

**`context_warning` event on a running job.**
Not a failure — one-shot notice that live usage crossed
`context_warn_pct` (default 65%). If the job then fails with
`context_overflow`, attempt 2 carries the checkpoint (that is the
recovery path working, test 9).

**Job `blocked(memory_required_unavailable)`.**
The daemon's `mycelium serve` child died twice (lifetime-1 respawn
policy ⇒ permanently unavailable until the daemon restarts). Fix
Mycelium, restart the daemon, `ladd resume <job>`. Non-`memory_required`
jobs never block on this — they launch without the memory section.

**Worker never appears to have dispatcher tools.**
By design: workers get only Mycelium recall/save/context/recent
(+ the standard file/test tools). If a worker *does* try to submit
jobs, it cannot — and if you find it can, that is a bug to report, not
work around.

**Cline jobs fail at the first model call.**
See cline-status.md: the stored `openai-compatible` model entry must
match the served model id, and the backend must be free + released.
The Claude Code adapter is the supported worker path until the Cline
smoke passes.

**Two instances, one repo.**
Allowed, but each instance needs its own `[state] root` (separate
db/lock/socket/worktrees). Foreign workers are never killed
(`foreign_marker_ignored`), so two daemons can coexist on one box.

## Not bugs (safety properties)

- Releasing a backend at runtime does **not** survive a daemon restart —
  the config file reasserts reserved/disabled states.
- `single` mode pauses queued jobs and **never auto-resumes** them in
  team mode (`ladd resume` is explicit and re-validates the base).
- `invalid_result` is never auto-retried — a silent retry of a
  contract-violating worker is how bad results get integrated.
