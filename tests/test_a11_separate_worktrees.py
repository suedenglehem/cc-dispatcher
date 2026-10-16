"""PRD §14 test 11 — every editing job gets its own worktree and branch
(dispatch/<job_id>); the primary checkout is never modified."""

import os
import subprocess

from tests.helpers import wait_attempts, wait_job


def test_jobs_get_separate_worktrees(env):
    env.backend()
    h = env.daemon()
    c = h.client()
    c.set_mode("team", project_id="p1")

    ids = [c.submit("p1", objective=f"job {i}", base_commit=env.repo_head,
                    allowed_scope=["src"], profile_id="w")["job_id"]
           for i in (1, 2)]
    for jid in ids:
        wait_job(c, jid, "succeeded")

    paths, branches = [], []
    for jid in ids:
        a = wait_attempts(h, jid, 1)[0]
        paths.append(a["worktree_path"])
        branches.append(a["branch"])
        assert os.path.isdir(a["worktree_path"])
        # worktree is named after the job, under the configured root
        assert a["worktree_path"].endswith(jid)
        assert a["branch"] == f"dispatch/{jid}"

    assert paths[0] != paths[1]
    assert branches[0] != branches[1]

    # primary checkout untouched: HEAD still the initial commit
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=env.repo, check=True,
        capture_output=True, text=True).stdout.strip()
    assert head == env.repo_head
