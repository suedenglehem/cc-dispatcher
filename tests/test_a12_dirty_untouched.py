"""PRD §14 test 12 — a dirty primary checkout (uncommitted, untracked
files) is never touched: the file stays in the main repo, is absent from
the job's worktree, and the job still succeeds."""

import subprocess
from pathlib import Path

from tests.conftest import make_repo, repo_head
from tests.helpers import wait_attempts, wait_job


def test_dirty_primary_checkout_untouched(env):
    dirty = make_repo(env.tmp_path / "dirty-repo", dirty=True)
    dirty_file = dirty / "src" / "dirty_note.txt"
    assert dirty_file.exists()
    head = repo_head(dirty)

    env.backend(id="b1")
    h = env.daemon(projects=[env.project(id="p1", repo=dirty)])
    c = h.client()
    c.set_mode("team", project_id="p1")

    jid = c.submit("p1", objective="job on dirty repo", base_commit=head,
                   allowed_scope=["src"], profile_id="w")["job_id"]
    v = wait_job(c, jid, "succeeded")
    assert v["failure_class"] is None

    wt = wait_attempts(h, jid, 1)[0]["worktree_path"]
    assert wt.endswith(jid)
    # the uncommitted file is NOT in the worktree (worktree is at base)
    assert not (Path(wt) / "src" / "dirty_note.txt").exists()
    # and the primary checkout's file survived the whole run, unmodified
    assert dirty_file.read_text(encoding="utf-8") == "local uncommitted work\n"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=dirty, check=True,
        capture_output=True, text=True).stdout
    assert "dirty_note.txt" in status  # still untracked, never committed away
