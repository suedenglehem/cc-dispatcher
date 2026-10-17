# Mycelium integration notes

Mycelium is the **shared long-term memory** for primary and workers.
It is never the queue, the lock service, or a source of scheduling
state — jobs, reservations, and capacity live in the dispatcher's
SQLite.

## Daemon side

- One long-lived `mycelium serve` stdio child, spawned **only if the
  config has a `[mycelium]` section** (absent section ⇒ the client can
  never start against your `~/.mycelium`). Raw newline-JSON-RPC 2.0.
- Single-flight calls, 10s per-call timeout, `_call` never raises.
- **Lifetime-1 respawn**: if the child dies once it is respawned; a
  second death makes memory permanently unavailable until the daemon
  restarts. (A flapping server must not flap the scheduler.)
- **Packet injection** at render time: ≤ `max_recall_queries` (3)
  `recall(query, project=<key>, limit=5)` calls from the job's
  `memory_queries`, injected as the `MEMORY_RECALLED` section,
  ≤ `memory_section_cap_chars` (4000) total. Never full-graph or
  history — bounded, per-job.
- Project key = the project's `mycelium_project` (defaults to the
  project id), so primary and workers share one key space.

**Outage policy:**
- job `memory_required = 0` (default): a dead client **degrades** —
  the job launches without the memory section (the packet must be
  self-sufficient).
- job `memory_required = 1`: a dead client **blocks** the job
  (`memory_required_unavailable`) — it needs the memory to be safe.

## Worker side

- Workers reach Mycelium through an **isolated `--mcp-config`**
  (`--strict-mcp-config`): the mycelium server and **nothing else**.
- Tool allowlist exposes only `recall`, `save`, `context`, `recent`.
  **No** maintenance tools (`forget`, `consolidate`, `maintain`, `pin`,
  `resolve`) — workers read, recall, and checkpoint; they never prune
  or restructure the network.
- Checkpoint saves use the envelope (provisional, bounded, no
  credentials / transcripts / queue state / resource locks):
  ```
  [worker-checkpoint] project=<key> job=<id> attempt=<n> base=<sha> role=worker status=<ok|failed|crash> | done: ... changed: ... tests: ... remaining: ... next: ...
  ```
  The `[worker-checkpoint]` prefix is distinct on purpose: Mycelium's
  janitor demotes `[session-checkpoint]` memories, while worker
  checkpoints should decay normally between runs.
- The envelope's `base=<sha>` + the dispatcher's `checkpoint.json`
  artifact make recovery context *verifiable* against the repo, not
  just plausible.

## Promotion (primary only)

After you have reviewed, merged, and recorded the integration:

```sh
ladd integrate <job> --commit <sha>   # marks integrated_commit
ladd promote <job>                    # pinned [decision] memory, confidence 0.9
```

Promotion requires `succeeded` + a recorded `integrated_commit`; it is
the explicit "this worker's result became real" event in shared memory.
Workers can never promote.

## Security

- Mycelium's HTTP surface has **no authentication**: never expose it
  publicly and never put its SQLite DBs on a network share. Local
  stdio children only.
- `MYCELIUM_CONFIG` points a server at an alternate store — tests use
  it to keep the user store (`~/.mycelium/memory.db`) untouched; the
  same trick isolates any experimental setup.
- Memory contents are data, not instructions: recalled text is
  injected into prompts as context and can be stale or wrong; treat
  it as a hint, verify against the tree.

## Test coverage

- a14: a fact the primary saved is visible in the worker's packet
  (`MEMORY_RECALLED`) — real `mycelium serve` subprocess.
- a15: kill the daemon's Mycelium child twice: job 1 healthy → job 2
  after respawn → job 3 degrades (no section, still succeeds) → job 4
  blocks per `memory_required`.
- a16: daemon **without** a `[mycelium]` section (isolation: no child
  ever spawned) while the worker's own child saves under the shared
  project key and the primary side recalls the job id.
