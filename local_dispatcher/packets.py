"""PRD §20 task-packet renderer.

The runner renders actual values into the packet — never unresolved
placeholders. Empty sections render as "(none)" so the worker can rely on
the presence of every field. The packet also carries the result/checkpoint
contracts the worker must honor (the runner validates the result, so the
worker must know its exact shape).
"""

from __future__ import annotations

import json

NONE = "(none)"

# PRD §20 result schema, verbatim, embedded so the worker reports in it.
RESULT_SCHEMA = """{
  "job_id": "<ATTEMPT_ID's job id>",
  "attempt_id": "<this attempt id>",
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
}"""

CHECKPOINT_SCHEMA = """{
  "status": "in_progress|done|blocked",
  "completed": "what is done, factually",
  "changed_paths": [],
  "tests": [{"command": "example", "status": "passed|failed|not_run"}],
  "remaining": [],
  "blockers": [],
  "assumptions": [],
  "next_action": "the next concrete step"
}"""


def _list(items) -> str:
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not items:
        return NONE
    if len(items) == 1:
        return items[0]
    return ";\n  ".join(items)


def _section(key: str, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return f"{key}: {NONE}"
    lines = text.splitlines()
    head = f"{key}: {lines[0]}"
    tail = "\n".join("  " + ln for ln in lines[1:])
    return f"{head}\n{tail}" if tail else head


def render_packet(*, project_row: dict, job: dict, attempt_no: int,
                  worktree: str, base_commit: str, artifact_dir: str,
                  deadline_expires_at: str | None,
                  deadline_remaining_s: int | None,
                  output_limit_chars: int,
                  memory_section: str | None = None,
                  recovery_context: str | None = None) -> str:
    """Render the §20 packet. `job` is the jobs row dict; JSON-list columns
    are already decoded by the caller (pass decoded lists, not raw strings)."""
    job_id = job["id"]
    attempt_id = f"{job_id}-a{attempt_no}"
    mycelium_project = job.get("mycelium_project") or \
        project_row.get("mycelium_project") or ""

    parts = [
        f"PROJECT_ID: {project_row['id']}",
        f"MYCELIUM_PROJECT: {mycelium_project or NONE}",
        f"JOB_ID / ATTEMPT_ID: {job_id} / {attempt_id}",
        f"OBJECTIVE: {job['objective'].strip()}",
        f"WORKTREE: {worktree}",
        f"BASE_COMMIT: {base_commit}",
        _section("ALLOWED_SCOPE", _list(job.get("allowed_scope"))),
        _section("RELEVANT_FILES", _list(job.get("relevant_files"))),
        _section("INTERFACE_CONTRACTS", job.get("contracts_text") or ""),
        _section("DECISIONS_AND_CONSTRAINTS", job.get("decisions_text") or ""),
        _section("ACCEPTANCE_CRITERIA",
                 _list([f"{i}. {c}" for i, c in enumerate(job.get("acceptance_criteria") or [], 1)])),
        _section("EXCLUSIONS", job.get("exclusions_text") or ""),
    ]

    limits = [f"deadline: job total, expires {deadline_expires_at or 'unset'}"]
    if deadline_remaining_s is not None:
        limits.append(f"remaining at render: {deadline_remaining_s}s")
    limits.append(f"output limit: {output_limit_chars} chars per report field")
    limits.append("turn limit: not supported by this adapter — bounded by "
                  "deadline + context budget instead")
    parts.append(_section("LIMITS", "\n".join(limits)))
    parts.append(_section("MEMORY_QUERIES", _list(job.get("memory_queries"))))
    if memory_section and memory_section.strip():
        parts.append(_section("MEMORY_RECALLED", memory_section.strip()))
    parts.append(f"CHECKPOINT_PATH: {artifact_dir}/checkpoint.json")
    parts.append(f"RESULT_PATH: {artifact_dir}/result.json")
    parts.append(_section(
        "RECOVERY_CONTEXT",
        recovery_context.strip() if recovery_context and recovery_context.strip()
        else "none (first attempt)"))

    parts.append(
        "CHECKPOINT_CONTRACT: after each meaningful milestone and before "
        "ending, atomically write (tmp file + rename) this JSON to "
        "CHECKPOINT_PATH:\n" + CHECKPOINT_SCHEMA)
    parts.append(
        "RESULT_CONTRACT: at the end of the run — whether completed, "
        "incomplete, blocked, or failed — atomically write (tmp file + "
        "rename) this JSON to RESULT_PATH. Record actual test commands and "
        "results only. Every acceptance criterion must be listed with a "
        "status and evidence reference. memory_references lists the Mycelium "
        "memory ids (the numbers of any recalled memories you actually relied "
        "on), as numbers (e.g. [190, 201]) — use [] if you relied on none:\n"
        + RESULT_SCHEMA)
    return "\n".join(parts) + "\n"
