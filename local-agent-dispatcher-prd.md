# PRD: Local Coding Agent Dispatcher

Status: Ready for implementation  
Working name: Local Dispatcher  
Primary interface: Claude Code through MCP  
Design principle: Small, deterministic orchestration service; agents perform reasoning and implementation.

## 1. Product objective
Build a local system that lets an existing Claude Code session switch between single mode (the primary implements alone) and team mode (the primary implements the main task while delegating independent supporting tasks).
The primary remains the lead implementer because it has the greatest capacity. Switching modes preserves its conversation, model configuration, working directory, and ongoing implementation. The system tolerates unavailable backends, worker crashes, context exhaustion, and switching back to single mode without losing partial work.

## 2. Deployment context
| Resource | Existing setup | Intended use |
|---|---|---|
| TR4 primary | Qwen 27B served by vLLM, used by Claude Code | Main implementation and coordination |
| TR4 secondary | Qwen Flash Next IQ3 served on another port, used by Cline | Supporting tasks |
| Optional i7 | Another Qwen 27B instance | Additional implementation or review |
Model IDs, server names, ports, API protocols, and context limits are configurable. Preserve the user's working provider settings; do not infer exact model identifiers from these descriptions.
Initial topology: all agent processes, worktrees, logs, and dispatcher state run on TR4. The i7 serves inference only. Shell commands and file edits therefore remain on TR4.
Each secondary backend supports one active worker job. Backends sharing constrained hardware can share a resource group with an additional concurrency limit. Different ports do not prove independent capacity. Reserve the primary backend exclusively for the primary.
External interactive clients can bypass dispatcher limits. Provide a manual reservation/disable operation for backends used outside the dispatcher. Do not claim global inference serialization without routing all clients through a common admission mechanism.

## 3. Goals and non-goals
Goals: explicit in-session switching; productive primary implementation; enforced capacity; isolated edits; durable jobs and artifacts; fresh-context recovery; shared Mycelium integration; concise status; Claude Code first and Cline through a verified adapter.
Non-goals for MVP: controlling existing IDE windows; adopting arbitrary interactive sessions; distributed agent execution; Redis/Celery/Kubernetes; automatic merging; recursive delegation; model installation or loading; web dashboard; hostile-code sandboxing.

## 4. User experience
Configure the dispatcher MCP connection before starting the primary session. Worker availability changes behind that connection without requiring primary restart.
- Enable team: primary sets team mode, discovers workers, submits independent tasks, and continues implementing.
- Return to single with drain: immediately disable submissions and launches, pause queued jobs, allow running jobs to finish, and retain result access.
- Return to single with cancel: pause queued jobs, terminate worker process trees with a grace period, preserve all artifacts, and never integrate partial edits automatically.
- Optional i7 appears: probe its enabled configuration and expose availability. Availability never changes mode automatically.
- No workers available: primary proceeds alone; requested team mode remains unchanged.
MVP uses primary-initiated status checks at natural milestones. Do not assume MCP can inject unsolicited messages into an idle primary.

## 5. Architecture
Use one local dispatcher service containing a scheduler, SQLite store, subprocess supervisor, backend registry, worktree manager, MCP interface, and small administrative CLI.
Recommended stack: Python 3.11+, SQLite, standard-library process management, official MCP SDK. Prefer a single asynchronous scheduler process. Use a filesystem-protected Unix socket on Linux/WSL or an authenticated loopback alternative where needed. A thin stdio MCP adapter forwards to the service; additional clients must not spawn competing schedulers.
| Information | Authority |
|---|---|
| Mode, queue, reservations, execution state | Dispatcher SQLite |
| Source changes | Git worktrees and commits |
| Logs, checkpoints, reports | Local artifact files |
| Shared project knowledge | Existing Mycelium store |
| Final integration | Primary/user |
Mycelium is never the queue or locking service.

## 6. Data model
Project: stable ID, canonical repository path, integration location, Mycelium project key, mode, mode-change time, optional primary-session ID. MVP permits one coordinating primary per project; projects share global backend capacity.
Backend: ID, endpoint/protocol, model ID, credential environment references, enabled state, health and probe time, concurrency (default 1), resource group, configured context limit, supported profiles, manual reservation.
Worker profile: adapter, trusted executable/argument configuration, environment overrides, backend association, capabilities, permissions, runtime/turn/output limits, Mycelium configuration. A profile launches fresh sessions rather than representing a permanent conversation.
Job: ID, project ID, idempotency key, objective, acceptance criteria, base commit, allowed scope, relevant files/contracts, dependencies, requested capability/profile, selected backend, status/timestamps, attempt count, limits, artifacts, result/failure classification, integration disposition (pending/accepted/rejected/superseded).
Attempt: ID, job ID, process identity including start identity, reservations, start/end time, exit status, failure class, checkpoint/log paths, available usage metrics. Retries retain previous attempts.

## 7. Scheduling and lifecycle
States: queued, paused, starting, running, succeeded, failed, cancelled, blocked, interrupted.
Succeeded means execution completed with a valid report; it does not mean accepted or integrated. Record each acceptance criterion separately. Zero process exit status alone does not establish task success. Blocked requires a decision or exhausted recovery. Interrupted means execution ownership became uncertain.
Requirements:
1. FIFO among eligible jobs is sufficient; validate dependencies and reject cycles.
2. Atomically reserve backend and resource-group capacity.
3. Recheck mode and availability at launch; persist attempts before spawning.
4. Serialize mode changes and launches. After switch-to-single completes, no new launch may commit. Already committed launches follow drain/cancel policy.
5. Release reservations only after confirming process termination.
6. Return existing jobs for duplicate idempotency keys.
7. Never schedule workers on the primary backend.
8. Pause queued work on single mode. Team mode does not automatically resume stale paused jobs; require explicit review/resume.
9. Dependent implementation requiring accepted predecessor changes must start from the updated integration commit, not merely from a predecessor's successful status.

## 8. MCP and CLI interfaces
These are proposed tools to implement, not existing Claude Code commands.
| Tool | Purpose |
|---|---|
| dispatch_status | Mode, drain state, compact workers/jobs summary |
| dispatch_set_mode | Set single/team and running-job drain/cancel policy |
| dispatch_submit | Validate/persist task; return job ID immediately |
| dispatch_job | Bounded job status, report, artifact references |
| dispatch_cancel | Cancel while retaining artifacts |
| dispatch_resume | Explicitly resume selected paused jobs |
| dispatch_workers | Availability, capabilities, reservations |
Expose equivalent administrative CLI operations, including backend enable/disable/reservation. Bind project identity through configured context or validate an explicit project ID on every operation.
Use structured schemas/results, stable IDs, actionable errors, pagination, credential redaction, and approximately 8,000-character default response caps. Large content is returned by artifact reference. Task fields must not become shell commands. Distinguish offline, busy, disabled, reserved, and incompatible states. Cache inexpensive health probes briefly; avoid generation-heavy probes.

## 9. Worker adapters and permissions
Claude Code adapter: inspect the installed CLI help and verify noninteractive execution, provider environment, output parsing, supported turn limits, permissions, isolated MCP configuration, cancellation, process trees, and nested-launch restrictions. Construct worker environments explicitly; do not blindly inherit primary credentials, endpoint, session variables, or dispatcher access. Use only verified flags.
Cline adapter: identify and document a supported programmatic/headless interface in the installed version. If absent, report unsupported clearly, do not automate editor clicks, and allow another compatible harness to use that backend. Do not claim Cline automation is complete without a real smoke test.
Workers receive only required tools plus Mycelium access. They do not receive dispatcher submission tools. Permission prompts must not hang unattended jobs indefinitely; use an explicit bounded policy without globally disabling safety controls. Avoid deployment and production credentials.

## 10. Repository workflow
Give each editing job a unique worktree and branch from its recorded base commit. Never reset, clean, switch, or implicitly commit the primary checkout. A committed base is required; provide interface contracts for relevant uncommitted primary work.
Check changed paths against allowed scope and flag violations. This is validation, not OS isolation. Preserve tracked and untracked changes, logs, and checkpoints on failure. Never auto-merge, auto-cherry-pick, or delete unresolved worktrees. The primary reviews, integrates, and runs combined validation.

## 11. Context management and recovery
Send bounded task packets: objective, criteria, base commit, scope, paths, design decisions, contracts, exclusions, artifact destinations, and relevant Mycelium queries/references. Never forward the complete primary transcript by default.
Workers atomically write checkpoints at meaningful milestones: completed work, changed files, actual test results, remaining work, blockers, next action, assumptions. The supervisor independently preserves worktrees/logs because prompts cannot guarantee checkpoints.
Defaults: one job/backend; 30-minute job deadline; two attempts total; 65% context warning threshold when measurable; 10-second termination grace; 8,000-character status cap. Make these configurable. Explain whether a deadline applies across attempts; default to a total job deadline.
Account for system prompts, tool schemas, history, tool output, and output reserve. With no reliable usage measurement, show unknown rather than fabricated percentages and rely on smaller tasks, bounded outputs, deadlines, and supported turn limits.
Overflow recovery: classify from adapter evidence; stop old execution; preserve state; start a fresh session with original task, checkpoint, and current diff; inspect existing work before editing; block after the attempt limit. Never repeatedly resume the oversized conversation. If no checkpoint exists, recover from task data, diff, and bounded logs, marking uncertainty.

## 12. Shared Mycelium integration
Reuse the user's existing installation: https://github.com/constant-itis/mycelium-memory . Do not create a replacement memory store.
The repository README documents project-scoped recall, shared MCP access, semantic memory, checkpoint conventions, local multi-client SQLite WAL access, and HTTP access for multiple machines. See Sources. Inspect AGENTS.md, agent-index.json, and actual tool schemas before wiring calls; do not assume undocumented parameters.
Every job carries a stable Mycelium project key independent of its temporary worktree path. At startup, recall only task-relevant conventions and decisions using bounded results. Do not load the full graph or unrestricted session history.
Workers may save evidenced findings, blockers, provisional observations, and compact continuity checkpoints. Include project/job/attempt IDs, base commit, role, and verification status, using a consistent text envelope if dedicated metadata fields are unavailable. Only the primary promotes accepted outcomes into authoritative project decisions.
Never save credentials, complete logs/transcripts, large source files, unverified claims as established facts, queue state, or resource locks. Workers must not maintain, delete, or consolidate shared memories.
Local files remain the recovery authority. Memory outages do not destroy progress: continue when the task packet suffices, otherwise block with the missing requirement. Preserve the existing trusted transport. The documented Mycelium HTTP server has no authentication: never expose it publicly or put its SQLite databases on a network share.

## 13. Failures and restart safety
| Failure | Required behavior |
|---|---|
| Offline before launch | Keep queued; report unavailable |
| Backend disconnect | Preserve attempt; bounded recovery |
| Context overflow | Fresh-context recovery |
| Deadline exceeded | Terminate process tree; preserve work |
| Invalid final report | Preserve output; classify invalid result |
| Failed tests | Report evidence; bounded repair if allowed |
| Dispatcher restart | Reconcile execution before rescheduling |
| Memory unavailable | Degrade safely or block missing essentials |
| Scope violation | Flag result for review |
Use process start identity or equivalent supervision; PID alone is insufficient after restart. If execution cannot be reliably identified/stopped, mark interrupted and quarantine capacity until reconciled. Avoid duplicate execution. Retry only repository-local work without irreversible external effects.

## 14. Acceptance tests
1. Single mode rejects submission without launching.
2. Team mode allows a worker while primary edits independently.
3. Two jobs on one backend never overlap.
4. Resource groups enforce shared capacity across endpoints.
5. Optional i7 availability updates without primary restart.
6. Drain pauses queued work and lets running work finish.
7. Cancel stops process trees and preserves partial artifacts.
8. Re-entering team does not silently resume paused work.
9. Simulated overflow uses a fresh attempt and preserved state.
10. Restart does not duplicate execution.
11. Primary/worker edits use separate worktrees.
12. Dirty primary changes are untouched.
13. Duplicate submission returns the same job.
14. Workers recall a project fact saved by primary.
15. Recovery works during Mycelium outage.
16. Worktrees share the same memory project key.
17. Zero exit without valid report is not success.
18. Out-of-scope changes are flagged.
19. Primary backend cannot receive worker jobs.
20. Concurrent mode-change/launch respects the no-new-launch guarantee.
Use deterministic fake adapters and a controllable fake endpoint, supplemented by real-model smoke tests.

## 15. Implementation phases and deliverables
Phase 1: SQLite, registry, modes, CLI, supervision, worktrees, verified Claude Code adapter.
Phase 2: MCP, primary instructions, compact reports, optional command shortcuts; demonstrate simultaneous primary implementation and secondary work.
Phase 3: checkpoints, context recovery, restart reconciliation, Mycelium integration, failure injection.
Phase 4: optional i7, resource groups, verified Cline adapter. Do not delay a working Claude Code release for unavailable Cline automation.
Deliver installable package, schema/migrations, MCP/CLI, verified adapters or explicit compatibility diagnostics, TR4/i7 example configuration, prompt templates, Mycelium instructions, automated tests, and setup/recovery/troubleshooting documentation. Preserve existing configurations.

## 16. Copyable implementation prompt — Claude Code or Cline
Use this to ask a coding agent to build the system. Supply this PRD file as context.
```text
Implement the Local Coding Agent Dispatcher specified in this PRD.
You are implementing the dispatcher, not starting production delegation yet.
Use KISS: Python 3.11+, SQLite, a single scheduler, subprocess workers,
and the official MCP SDK unless inspection establishes a concrete obstacle.
Inspect the repository and installed Claude Code/Cline versions first.
Verify supported CLI flags, provider configuration, permissions, MCP setup,
and cancellation behavior. Never invent an unsupported integration.
Preserve all existing user configuration and uncommitted source changes.
Read Mycelium AGENTS.md, agent-index.json, and actual tool schemas at
https://github.com/constant-itis/mycelium-memory before integrating it.
Reuse the existing store. Do not use memory as queue state or locking.
Implement phase 1 end-to-end, then phase 2, then recovery and memory, then
additional adapters. Continue through phases without repeated approval
unless a destructive action, credentials, or a genuine environmental blocker
requires user input. Do not run destructive setup or production deployments.
Keep the primary backend reserved. Enforce scheduling in code, not prompts.
Use fake adapters for automated tests and real smoke tests where available.
If Cline cannot be controlled through a supported interface, report that
specific limitation and continue the Claude Code path; never fake support.
Deliver runnable code, configuration examples, migrations, tests, prompt
files, and concise installation/recovery documentation. Report commands
actually executed and their results; distinguish mock tests from real tests.
Do not stop at an architecture proposal. Build the smallest working path.
```

## 17. Primary prompt — Claude Code CLAUDE.md
Append this to project instructions once the dispatcher exists. Tool names below are the proposed names in this PRD; installation may expose an MCP prefix.
```text
You are the lead implementer and coordinator. Keep core, complex, and tightly
coupled implementation work yourself. Delegate independent supporting work.
The dispatcher is authoritative for mode, availability, and job state.
Default new projects to single mode. On reconnect, inspect persisted mode;
do not silently reset an existing project's state.
Enable team mode only when requested. Call dispatch_set_mode, then inspect
workers. Submit bounded tasks with a committed base, allowed scope, relevant
interface contracts, acceptance criteria, limits, and a stable Mycelium key.
After dispatch, continue your own implementation immediately. Check progress
at natural milestones. Wait only for a real dependency; avoid rapid polling.
Never send the entire conversation to a worker. Never bypass the dispatcher
by launching worker processes yourself. Never use the primary backend for
workers. If no worker is available, continue alone where safe.
On single mode, disable submissions/launches and pause queued jobs. Drain
running jobs unless cancellation was explicit. Review paused jobs before
resuming them; update stale bases and contracts through a new job if needed.
Workers use separate worktrees. Do not assign overlapping active edits.
Review diffs, scope, assumptions, and test evidence before integration.
Run combined validation; successful worker execution is not acceptance.
For context failures, allow bounded fresh-session recovery from artifacts.
After recovery exhaustion, split the task, block it, or complete it yourself.
Recall relevant Mycelium knowledge. Treat worker memories as provisional.
Only save verified accepted decisions as authoritative project knowledge.
Never store credentials or transcripts in memory. Keep operational state
in the dispatcher and local artifacts. Do not perform memory maintenance.
```

## 18. Primary prompt — Cline alternative
Use this only if Cline is deliberately chosen as the coordinating primary. Do not run two coordinating primaries for one project in the MVP. Install using the verified project-instruction mechanism of the installed Cline version.
```text
You are the lead implementer for this project and its sole coordinator.
Follow the primary-agent policy in section 17 of this PRD, using the connected
dispatcher MCP tools. Keep implementing the main task while independent
workers run. Dispatcher state, not conversation memory, determines mode.
Do not treat another open editor or agent window as a registered worker.
Do not launch workers directly, schedule work on the reserved primary
backend, or bypass capacity controls. If dispatch tools are absent, explain
that delegation is unavailable and continue single-agent work where safe.
Use the existing Mycelium connection and stable project key. Review and
integrate worker results yourself, with combined validation afterward.
```

## 19. Shared worker prompt — Claude Code and Cline
The runner supplies this policy plus the task packet in section 20. Apply it in an isolated worker configuration; do not let workers inherit primary-coordinator instructions that authorize dispatch.
```text
You are a bounded implementation/review worker, not the coordinator.
Complete only the supplied task within its worktree and allowed scope.
Read the task packet, inspect the checkout, and recall only relevant Mycelium
facts for its project key. Treat recalled worker observations as provisional.
Do not delegate, call dispatch tools, merge into the primary branch, alter
other worktrees, change credentials, or perform external deployment actions.
If the task requires scope expansion or a missing decision, checkpoint and
report blocked. Do not silently expand the assignment.
Make small changes and run relevant tests. Record actual commands/results;
do not claim tests passed unless they ran successfully.
Write an atomic checkpoint after meaningful milestones and before ending:
completed work, changed paths, tests/results, remaining work, blockers,
assumptions, and the next concrete action. Keep logs and output bounded.
If context is becoming tight, checkpoint early and return an incomplete
report rather than repeatedly attempting to fit the same oversized context.
On recovery, inspect preserved changes before editing. Do not redo completed
work or discard partial changes without evidence that they are incorrect.
You may save concise evidenced findings to Mycelium labelled provisional,
including project, job, attempt, and base commit. Do not save logs, secrets,
or authoritative project decisions. Never run memory maintenance/deletion.
Finish with the structured result required by the runner. If incomplete,
state exactly what remains and preserve all work. Completion is a proposal
for review, not permission to merge or declare project-wide success.
```
Claude Code adapter: supply this through verified supported prompt/configuration mechanisms with restricted tools and isolated MCP settings.
Cline adapter: supply the identical policy through its verified programmatic interface; if no interface exists, this remains usable for manually assigned isolated tasks but does not satisfy automated adapter acceptance.

## 20. Task packet and result templates
The runner renders actual values into this packet, not unresolved placeholders.
```text
PROJECT_ID: <stable project ID>
MYCELIUM_PROJECT: <existing stable project key>
JOB_ID / ATTEMPT_ID: <identifiers>
OBJECTIVE: <one concrete outcome>
WORKTREE: <absolute path>
BASE_COMMIT: <resolved commit>
ALLOWED_SCOPE: <paths/modules>
RELEVANT_FILES: <bounded path list>
INTERFACE_CONTRACTS: <required signatures/behavior>
DECISIONS_AND_CONSTRAINTS: <necessary context only>
ACCEPTANCE_CRITERIA: <observable requirements and test expectations>
EXCLUSIONS: <explicit non-goals>
LIMITS: <deadline, supported turn limit, output limits>
MEMORY_QUERIES: <focused queries or references>
CHECKPOINT_PATH / RESULT_PATH: <artifact destinations>
RECOVERY_CONTEXT: <checkpoint/diff references, or none>
```
Result schema to implement and validate:
```json
{
  "job_id": "example-job",
  "attempt_id": "example-attempt",
  "outcome": "completed|incomplete|blocked|failed",
  "summary": "Short factual result",
  "changed_files": [],
  "commit": null,
  "tests": [{"command": "example", "status": "passed|failed|not_run", "evidence_path": "path"}],
  "acceptance_criteria": [{"criterion": "example", "status": "met|unmet|unknown", "evidence": "reference"}],
  "remaining_work": [],
  "blockers": [],
  "assumptions": [],
  "checkpoint_path": "path",
  "memory_references": []
}
```
Enum strings separated by vertical bars above document allowed alternatives; actual reports choose one value. Validate paths, identifiers, and schema. Preserve raw output when validation fails.

## 21. Copyable runtime commands for the primary
**Enable team:**
> Enable team mode through the dispatcher. Discover available workers, delegate independent supporting tasks, and keep implementing the main task yourself. Keep the primary backend reserved. Do not resume paused jobs without reviewing their scope and base.
**Return to single, draining:**
> Switch to single mode through the dispatcher. Stop new submissions and launches, pause queued jobs, let running jobs finish, and preserve their results. Continue implementing alone and review completed work at safe checkpoints.
**Return to single immediately:**
> Switch to single mode now. Cancel running worker tasks, pause queued jobs, and preserve worktrees, logs, and checkpoints. Do not merge partial work automatically. Continue alone where safe.
**New backend available:**
> Refresh worker availability; I started the additional backend. Keep the current mode unchanged. If we are in team mode, use it for suitable independent work while continuing your implementation.
**Resume selected work:**
> Review paused jobs against the current integration commit and task scope. Resume only jobs whose original base and contracts remain appropriate; replace stale assignments with revised jobs. Keep implementing the main task.
**Recover a worker:**
> Inspect the failed job and its artifacts. If recovery is safe and within limits, request a fresh-context attempt from its checkpoint and current diff. Do not resume an oversized transcript or duplicate a still-running process. Otherwise split the task or complete it yourself.

## Sources
Mycelium repository README inspected October 16, 2026: https://github.com/constant-itis/mycelium-memory
Implementation discovery: https://github.com/constant-itis/mycelium-memory/blob/main/AGENTS.md and https://github.com/constant-itis/mycelium-memory/blob/main/agent-index.json
Mycelium interface and concurrency statements are based on repository documentation. Verify the user's installed revision and actual tool schemas during implementation. Dispatcher interfaces and all prompts in this document are proposed requirements, not claims of existing built-in features.
