# Copyable runtime commands for the primary

PRD §21 ships six prompt blocks the primary session can paste to steer the
dispatcher. Each block below is the verbatim prompt, followed by the concrete
`ladd` commands (or `dispatch_*` MCP tools) that implement it. The MCP tools
do the same thing over the socket; use whichever surface you're in.

## Enable team

> Enable team mode through the dispatcher. Discover available workers, delegate independent supporting tasks, and keep implementing the main task yourself. Keep the primary backend reserved. Do not resume paused jobs without reviewing their scope and base.

```sh
ladd mode <project> team            # e.g. ladd mode demo team
ladd workers                        # who is actually free right now
ladd submit "<objective>" <base-sha> cc-worker \
    --scope src --file src/app.py \
    --criterion "<acceptance criterion>" \
    --backend <worker-backend> \
    --project <project>
```

- The primary backend (`is_primary = true`) is never worker-scheduled — no
  flag needed, it is excluded by code.
- `--backend` pins a specific worker; omit it to let the scheduler pick
  from the profile's `backend_ids`.

## Return to single, draining

> Switch to single mode through the dispatcher. Stop new submissions and launches, pause queued jobs, let running jobs finish, and preserve their results. Continue implementing alone and review completed work at safe checkpoints.

```sh
ladd mode <project> single --policy drain
ladd status                        # queued jobs are paused (drain); running ones finish
```

## Return to single immediately

> Switch to single mode now. Cancel running worker tasks, pause queued jobs, and preserve worktrees, logs, and checkpoints. Do not merge partial work automatically. Continue alone where safe.

```sh
ladd mode <project> single --policy cancel
```

- Running attempts are killed (grace period → SIGKILL); worktrees, streams,
  and checkpoints are left in place under the state dir for review.

## New backend available

> Refresh worker availability; I started the additional backend. Keep the current mode unchanged. If we are in team mode, use it for suitable independent work while continuing your implementation.

```sh
ladd backend enable <id>           # if it was disabled in config/DB
ladd workers --refresh             # force probes now
```

- Enabling a backend that an interactive session is using is the user's
  decision; the shipped config marks such backends `manually_reserved` and
  reasserts that on daemon start.

## Resume selected work

> Review paused jobs against the current integration commit and task scope. Resume only jobs whose original base and contracts remain appropriate; replace stale assignments with revised jobs. Keep implementing the main task.

```sh
ladd --json status                 # list jobs; find the paused ones
ladd job <job_id>                  # scope, base_commit, criteria, integration state
ladd resume <job_id>               # re-validates base before it launches
```

- A resume that finds a diverged base blocks with `base_diverged` instead of
  launching; the honest move is a revised job on the new base.

## Recover a worker

> Inspect the failed job and its artifacts. If recovery is safe and within limits, request a fresh-context attempt from its checkpoint and current diff. Do not resume an oversized transcript or duplicate a still-running process. Otherwise split the task or complete it yourself.

```sh
ladd job <job_id> --full           # failure_class, attempts, artifact paths
ladd --json events --subject-type job --subject-id <job_id>
```

- Artifacts live under `<state>/artifacts/<job_id>/attempt-<n>/` —
  `stream.log`, `checkpoint.json`, `result.json`, `diff.patch`.
- A `blocked` job with `recovery_exhausted` needs a decision: `ladd resume`
  (fresh context from checkpoint + diff, same worktree) or a revised job.
- `ladd reconcile` frees quarantined capacity from dead attempts.

## View job history

```sh
ladd status                        # bounded default: active + blocked jobs in full,
                                   # terminal jobs collapsed to a count + last five
ladd --json status --all-jobs      # full list of every job, including terminal ones
```

- The default view is bounded forever: terminal jobs (succeeded/failed/
  cancelled) collapse to `jobs_terminal: {count, recent: last 5}` so the
  response can never outgrow the 8k cap as history accumulates.
- `--all-jobs` is the explicit full dump (note the global flag position:
  `ladd --json status --all-jobs`); it is exempt from the 8k response cap.
  The MCP surface never sends `all_jobs`, so `dispatch_status` always gets
  the bounded default.

## Backend probing and recheck

The daemon probes every configured backend's `/v1/models` at startup and
again whenever a probe is older than `probe_ttl_s` (30s by default). A
backend is schedulable only when it is `enabled`, not reserved, and
**online with a fresh probe** — so after a boot change (dual-boot box), a
restart, or a llama-server stop/start, scheduling follows the backend that
is actually answering within one probe cycle, with no action needed.

To recheck immediately instead of waiting up to 30s:

```sh
ladd workers --refresh             # force probes on all backends now
ladd backend check <id>            # force one probe, show that backend's row
```

Mid-job behavior:

- **You stop a backend a running job is using** — the worker's API calls
  fail, the adapter classifies the attempt `backend_disconnect` (retryable),
  and the next attempt may launch on another online backend from the
  profile. Exhausted attempts end `blocked`.
- **You start a backend a queued job is waiting on** — the job sits at
  `status_detail = backend_offline` until a probe flips the backend online;
  `ladd workers --refresh` skips the wait.
