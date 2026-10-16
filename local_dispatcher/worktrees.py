"""Git worktree management.

Each editing job gets a unique worktree + branch from its recorded base
commit. The primary checkout is never reset, cleaned, switched, or
committed to. Scope checking is validation, not OS isolation (PRD §10).
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import subprocess

from .models import ApiError, ErrorCodes


async def _git(*args: str, cwd: str, timeout: float = 60.0) -> tuple[int, str, str]:
    loop = asyncio.get_running_loop()

    def run() -> tuple[int, str, str]:
        p = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    return await loop.run_in_executor(None, run)


def worktree_path_for(worktree_root: str, job_id: str) -> str:
    return os.path.join(worktree_root, job_id)


def branch_for(job_id: str) -> str:
    return f"dispatch/{job_id}"


async def commit_exists(repo_path: str, ref: str) -> bool:
    rc, _, _ = await _git("cat-file", "-e", f"{ref}^{{commit}}", cwd=repo_path)
    return rc == 0


async def resolve_ref(repo_path: str, ref: str) -> str | None:
    rc, out, _ = await _git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=repo_path)
    return out if rc == 0 and out else None


async def is_ancestor(repo_path: str, ancestor: str, descendant: str) -> bool:
    rc, _, _ = await _git("merge-base", "--is-ancestor", ancestor, descendant, cwd=repo_path)
    return rc == 0


async def ensure_worktree(job_id: str, repo_path: str, worktree_root: str,
                          base_commit: str) -> str:
    """Create (or reuse) the job's worktree at base_commit.

    Reuse is safe: attempts of the same job share the worktree on purpose
    (recovery continues in the same checkout). The branch is created from
    base_commit on first use; subsequent uses leave the working tree exactly
    as it was.
    """
    path = worktree_path_for(worktree_root, job_id)
    branch = branch_for(job_id)
    if os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git")):
        # worktree exists — verify it belongs to this repo (sanity only)
        rc, out, _ = await _git("rev-parse", "--git-dir", cwd=path)
        if rc == 0:
            return path
        raise ApiError(ErrorCodes.INTERNAL, f"worktree exists but is not a git dir: {path}")
    os.makedirs(worktree_root, exist_ok=True)
    rc, _, err = await _git("worktree", "add", "-b", branch, path, base_commit, cwd=repo_path)
    if rc != 0:
        # Branch may already exist from a crashed prior run whose worktree was
        # removed; retry attaching the existing branch.
        rc2, _, _ = await _git("branch", "--list", branch, cwd=repo_path)
        if rc2 == 0 and branch in _:
            rc, _, err = await _git("worktree", "add", path, branch, cwd=repo_path)
        if rc != 0:
            raise ApiError(ErrorCodes.INTERNAL, f"git worktree add failed: {err}")
    return path


async def capture_changes(worktree_path: str) -> tuple[str, list[str]]:
    """Return (diff patch, changed+untracked path list) relative to HEAD.

    Untracked files are listed (and their content not inlined); the patch
    covers tracked changes only. Callers cap the patch size.
    """
    rc, diff, _ = await _git("diff", "HEAD", cwd=worktree_path)
    if rc != 0:
        diff = ""
    rc, names, _ = await _git("diff", "--name-only", "HEAD", cwd=worktree_path)
    changed = [x for x in names.splitlines() if x] if rc == 0 else []
    rc, untracked, _ = await _git("ls-files", "--others", "--exclude-standard", cwd=worktree_path)
    untracked = [x for x in untracked.splitlines() if x] if rc == 0 else []
    return diff, changed + untracked


def check_scope(changed_paths: list[str], allowed_scope: list[str]) -> list[str]:
    """Changed paths not covered by any allowed-scope pattern.

    Patterns are git-style globs matched against the repo-relative path
    (fnmatch with ** handled segment-wise). A pattern 'src' covers everything
    under src/.
    """
    violations = []
    for p in changed_paths:
        if not any(_scope_matches(p, pat) for pat in allowed_scope):
            violations.append(p)
    return violations


def _scope_matches(path: str, pattern: str) -> bool:
    pattern = pattern.rstrip("/")
    if path == pattern:
        return True
    if path.startswith(pattern + "/"):
        return True
    if "**" in pattern or "*" in pattern or "?" in pattern:
        return fnmatch.fnmatch(path, pattern)
    return False


async def remove_worktree(repo_path: str, path: str) -> str:
    """Remove a worktree directory. Never used by the dispatcher for
    unresolved work (PRD: never delete unresolved worktrees) — exposed for
    explicit cleanup of terminal jobs by an administrator."""
    rc, _, err = await _git("worktree", "remove", "--force", path, cwd=repo_path)
    if rc != 0 and os.path.isdir(path):
        # fall back to prune + manual removal of the (unmodified) checkout
        import shutil
        shutil.rmtree(path, ignore_errors=True)
    await _git("worktree", "prune", cwd=repo_path)
    return err


async def current_head(repo_path: str) -> str | None:
    return await resolve_ref(repo_path, "HEAD")
