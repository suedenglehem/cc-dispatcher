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

## Salad backends (salad.cloud groups)

Workers can run on LLMs served by a SaladCloud group. You start/stop the
groups yourself; the dispatcher only registers the gateway URL, keeps a
local proxy alive, probes it, and schedules jobs on it when it is online.

```sh
ladd -a https://<group>.salad.cloud     # register (id = first host label)
ladd -d https://<group>.salad.cloud     # unregister (matched by gateway URL)
ladd workers --refresh                  # see its health / ctx
```

Full options (same API, explicit id/port/model/…):

```sh
ladd backend add-salad https://<group>.salad.cloud \
    [--id mygroup] [--port 18110] [--keyfile /usr/local/bin/salad_api.txt] \
    [--model qwen] [--thinking] [--concurrency 1] [--group tr4] [--notes "..."]
ladd backend remove-salad <id>
```

What happens on add:

- The backend block is appended to `config.toml` (atomic rewrite, validated
  before install) and the id is added to every **claude-adapter** profile's
  `backend_ids` — so jobs can pick it up immediately.
- A local `salad_proxy.py` is started on `127.0.0.1:18100-18199`
  (first free port) translating Anthropic→OpenAI and injecting the
  `Salad-Api-Key` from the keyfile. Workers point at the proxy, never at
  the gateway directly.
- A probe runs at once. A **stopped group is not an error**: the Cloudflare
  front answers 503, the backend is registered and `offline`, and it flips
  `online` on the next probe after you start the group — no re-add needed.
- If the gateway answers 403 the add still succeeds but reports
  `key_rejected: true` — the registered Salad key is wrong; fix the keyfile,
  then `ladd workers --refresh`.

On remove: the proxy is stopped, the block and profile-list entries are
removed from `config.toml`, and the DB row is deleted (kept as an inert
tombstone if jobs reference it). A backend with an active job cannot be
removed — cancel the jobs first.

Operational notes:

- Proxies survive daemon restarts (they are stateless and re-adopted by a
  per-backend nonce, never duplicated). `ladd backend check <id>` force-probes
  one backend; the proxy log is `<state>/run/salad_proxy_<id>.log`.
- While a worker is actually running on a salad backend, the daemon sends a
  1-token heartbeat every 30s to the group — so
  `IDLE_SHUTDOWN=heartbeat` groups survive the long gaps between a worker's
  LLM calls (e.g. during Bash). Real chat traffic already counts as
  activity; the heartbeat is just the floor. Set
  `salad_heartbeat = false` on the backend to opt out.
- Context limit: the probed `n_ctx` × 90%, same convention as the local
  backends.
- Live verification (2026-10-19): `ladd -a` against a **stopped** group
  registered cleanly — the gateway answered its 503 "group stopped" page and
  the backend came up `offline` with its proxy running, exactly the designed
  behavior for a dead URL.
- **Live online worker smoke — PASS (2026-10-19).** With the group running:
  idle TTFB ~1.7s (small prompt), ~93s for a worker-shaped ~5K-token prompt
  (the Cloudflare front sits in front of the origin, and one group instance
  serializes requests — real 12–16K-token worker prompts sit at/past the
  edge's ~100s origin timeout). The first live job (`j_8bf0ff2baf`) hit that
  edge: the upstream answered 200 but the stream ended with **zero content**,
  and the proxy rendered that as a clean empty turn → bogus `invalid_result`.
  Fixed in `salad_proxy.py`: a zero-content or anomalous stream end (in-band
  `{"error": …}` payload, non-SSE line, unparseable `data:`) now yields one
  `error` event, which the adapter classifies as retryable
  `backend_disconnect` — never a fake success. The re-submitted job
  (`j_d12f1ae064`) **succeeded on attempt 1** (~13.5 min, 8 turns,
  30K-token prompts) with the exact requested diff. Regression tests:
  `tests/test_u_salad.py` (`_iter_sse` anomaly sentinels, `StreamBuilder`
  empty-vs-content) and the parametrized real-proxy `test_stream_failure_surfaced`
  in `tests/test_i_salad.py`.
