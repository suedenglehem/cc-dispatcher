"""Unit tests — the PRD §20 task-packet renderer."""

from local_dispatcher.packets import render_packet

PROJECT = {"id": "p1", "repo_path": "/repo", "mycelium_project": "proj-key"}


def _job(**over) -> dict:
    job = {
        "id": "j1",
        "objective": "  make it work  ",
        "allowed_scope": ["src"],
        "relevant_files": ["src/app.py", "src/lib.py"],
        "contracts_text": "",
        "decisions_text": "",
        "acceptance_criteria": [],
        "exclusions_text": "",
        "memory_queries": [],
        "mycelium_project": None,
    }
    job.update(over)
    return job


def _render(job=None, **kw) -> str:
    return render_packet(
        project_row=PROJECT,
        job=job or _job(),
        attempt_no=kw.pop("attempt_no", 1),
        worktree="/wt/j1",
        base_commit="abc123",
        artifact_dir="/art/j1/1",
        deadline_expires_at="2026-01-01T00:05:00Z",
        deadline_remaining_s=kw.pop("deadline_remaining_s", 300),
        output_limit_chars=8000,
        memory_section=kw.pop("memory_section", None),
        recovery_context=kw.pop("recovery_context", None))


def test_packet_has_every_section():
    p = _render()
    for line in (
            "PROJECT_ID: p1",
            "MYCELIUM_PROJECT: proj-key",
            "JOB_ID / ATTEMPT_ID: j1 / j1-a1",
            "OBJECTIVE: make it work",
            "WORKTREE: /wt/j1",
            "BASE_COMMIT: abc123",
            "ALLOWED_SCOPE: src",
            "LIMITS:",
            "MEMORY_QUERIES: (none)",
            "CHECKPOINT_PATH: /art/j1/1/checkpoint.json",
            "RESULT_PATH: /art/j1/1/result.json",
            "RECOVERY_CONTEXT: none (first attempt)",
            "CHECKPOINT_CONTRACT:",
            "RESULT_CONTRACT:",
    ):
        assert line in p, line


def test_empty_sections_render_none():
    p = _render()
    assert "RELEVANT_FILES: (none)" not in p  # two files are listed
    p = _render(job=_job(relevant_files=[], acceptance_criteria=[]))
    assert "RELEVANT_FILES: (none)" in p
    assert "ACCEPTANCE_CRITERIA: (none)" in p
    assert "INTERFACE_CONTRACTS: (none)" in p
    assert "DECISIONS_AND_CONSTRAINTS: (none)" in p
    assert "EXCLUSIONS: (none)" in p


def test_multi_item_lists_and_numbered_criteria():
    p = _render(job=_job(acceptance_criteria=["first", "second"]))
    assert "ACCEPTANCE_CRITERIA: 1. first" in p
    assert "  2. second" in p
    assert "RELEVANT_FILES: src/app.py" in p
    assert "  src/lib.py" in p


def test_memory_and_recovery_sections():
    p = _render(memory_section="FACT: use the fast path")
    assert "MEMORY_RECALLED: FACT: use the fast path" in p
    p = _render(memory_section="   ")
    assert "MEMORY_RECALLED" not in p
    p = _render(recovery_context="NO CHECKPOINT WAS SAVED — inspect")
    assert "RECOVERY_CONTEXT: NO CHECKPOINT WAS SAVED" in p


def test_attempt_number_in_id():
    assert "JOB_ID / ATTEMPT_ID: j1 / j1-a3" in _render(attempt_no=3)


def test_limits_block():
    p = _render()
    assert "expires 2026-01-01T00:05:00Z" in p
    assert "remaining at render: 300s" in p
    assert "output limit: 8000 chars per report field" in p
    assert "turn limit: not supported by this adapter" in p


def test_result_and_checkpoint_schemas_embedded():
    p = _render()
    assert '"acceptance_criteria"' in p and '"checkpoint_path"' in p
    assert '"next_action"' in p
