"""PR worktree and lock management."""
from __future__ import annotations

import fcntl
import os
import re
import sys
import tempfile
from typing import Optional

from .config import LOOP_ALREADY_RUNNING_MESSAGE
from .errors import CommandError
from .process import _print_step, _run_command

def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return slug or "unknown"


class _LoopLock:
    """Advisory lock keyed by PR number (or branch fallback)."""

    def __init__(self, handle, path: str):
        self.handle = handle
        self.path = path

    def release(self):
        try:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        finally:
            try:
                self.handle.close()
            except OSError:
                pass
            try:
                os.unlink(self.path)
            except OSError:
                pass


def _acquire_loop_lock(
    *, pr_number: Optional[int], branch_fallback: Optional[str] = None
) -> Optional["_LoopLock"]:
    if pr_number is not None:
        key = "pr-{}".format(pr_number)
    elif branch_fallback:
        key = "branch-{}".format(_slug(branch_fallback))
    else:
        return None
    lock_path = os.path.join(
        tempfile.gettempdir(), "codex-ralph-loop-{}.lock".format(key)
    )
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    handle = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.stderr.write("{}\n".format(LOOP_ALREADY_RUNNING_MESSAGE))
            sys.stderr.flush()
            handle.close()
            raise SystemExit(0)
        handle.seek(0)
        handle.truncate(0)
        handle.write("{}\n".format(os.getpid()))
        handle.flush()
    except SystemExit:
        raise
    except BaseException:
        try:
            handle.close()
        except OSError:
            pass
        raise
    return _LoopLock(handle, lock_path)


def _worktree_path(*, worktree_root: str, pr_number: int, branch: str) -> str:
    return os.path.join(
        worktree_root,
        "pr-{}-{}".format(pr_number, _slug(branch)),
    )


def _pr_head_fetch_ref(pr_number: int) -> str:
    return "refs/remotes/origin/pr-{}-head".format(pr_number)


def _fetch_pr_branch_or_head(
    *, pr_number: int, branch: str, cwd: Optional[str] = None
) -> str:
    fetch_branch = _run_command(
        ["git", "fetch", "origin", branch],
        check=False,
        capture_output=True,
        cwd=cwd,
    )
    if fetch_branch.returncode == 0:
        return "origin/{}".format(branch)

    _print_step(
        "Origin branch '{}' was not fetchable; fetching PR #{} head ref".format(
            branch, pr_number
        )
    )
    pr_head_ref = _pr_head_fetch_ref(pr_number)
    _run_command(
        [
            "git",
            "fetch",
            "origin",
            "+refs/pull/{}/head:{}".format(pr_number, pr_head_ref),
        ],
        check=True,
        capture_output=True,
        cwd=cwd,
    )
    return pr_head_ref


def _worktree_for_branch(branch: str) -> Optional[str]:
    completed = _run_command(
        ["git", "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
    )
    current_path = None
    expected_branch_ref = "refs/heads/{}".format(branch)
    for line in (completed.stdout or "").splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
            continue
        if line == "branch {}".format(expected_branch_ref) and current_path:
            return current_path
    return None


def _ensure_pr_worktree(
    *,
    worktree_root: str,
    pr_number: int,
    branch: str,
) -> str:
    os.makedirs(worktree_root, exist_ok=True)
    path = _worktree_path(
        worktree_root=worktree_root,
        pr_number=pr_number,
        branch=branch,
    )
    start_ref = _fetch_pr_branch_or_head(pr_number=pr_number, branch=branch)
    existing_branch_worktree = _worktree_for_branch(branch)
    if existing_branch_worktree and os.path.abspath(
        existing_branch_worktree
    ) != os.path.abspath(path):
        _print_step(
            "PR branch '{}' is already checked out at {}; reusing it".format(
                branch, existing_branch_worktree
            )
        )
        _fetch_pr_branch_or_head(
            pr_number=pr_number,
            branch=branch,
            cwd=existing_branch_worktree,
        )
        return existing_branch_worktree
    if os.path.isdir(path):
        _print_step("Using existing PR worktree {}".format(path))
        worktree_branch = _run_command(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
        )
        if (worktree_branch.stdout or "").strip() != branch:
            raise CommandError(
                "Existing worktree {} is on branch '{}' instead of '{}'.".format(
                    path,
                    (worktree_branch.stdout or "").strip() or "<unknown>",
                    branch,
                )
            )
        _fetch_pr_branch_or_head(pr_number=pr_number, branch=branch, cwd=path)
        return path
    _print_step("Creating PR worktree {}".format(path))
    result = _run_command(
        ["git", "worktree", "add", path, "-B", branch, start_ref],
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return path
    stderr = "{}\n{}".format(result.stdout or "", result.stderr or "").lower()
    if "already used by worktree" in stderr:
        sys.stdout.write("{}\n".format(LOOP_ALREADY_RUNNING_MESSAGE))
        sys.stdout.flush()
        raise SystemExit(0)
    raise CommandError(
        "Unable to create worktree for branch '{}': {}".format(
            branch,
            (result.stderr or result.stdout or "").strip(),
        )
    )
