"""PR worktree and lock management."""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import sys
import tempfile
from typing import Dict, Optional, Set

from .config import LOOP_ALREADY_RUNNING_MESSAGE
from .errors import LOOP_ALREADY_RUNNING_EXIT_CODE, CommandError
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
    if os.path.islink(lock_path):
        raise CommandError("Refusing to use symlink as Ralph lock file: {}".format(lock_path))
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    handle = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.stderr.write("{}\n".format(LOOP_ALREADY_RUNNING_MESSAGE))
            sys.stderr.flush()
            handle.close()
            raise SystemExit(LOOP_ALREADY_RUNNING_EXIT_CODE)
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


_LOCK_FILENAME_PR_RE = re.compile(r"^codex-ralph-loop-pr-(\d+)\.lock$")
_WORKTREE_DIR_PR_RE = re.compile(r"^pr-(\d+)-")


def _is_path_within(child: str, parent: str) -> bool:
    """Return True if realpath(child) is strictly inside realpath(parent)."""
    try:
        parent_real = os.path.realpath(parent)
        child_real = os.path.realpath(child)
    except OSError:
        return False
    if not parent_real or not child_real:
        return False
    parent_real = parent_real.rstrip(os.sep) + os.sep
    if child_real == parent_real.rstrip(os.sep):
        return False
    return child_real.startswith(parent_real)


def _lock_is_held_by_other_process(path: str) -> bool:
    """Return True if another process currently holds an exclusive flock on path.

    Returns False if the file does not exist, cannot be opened, or if we can
    successfully acquire (and immediately release) an exclusive lock — the
    latter implies no other process is currently holding it.
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _cleanup_stale_loop_state(
    worktree_root: str, open_pr_numbers: Set[int]
) -> Dict[str, int]:
    """Remove lock files and worktree directories for PRs no longer open.

    - Scans ``tempfile.gettempdir()`` for ``codex-ralph-loop-pr-<N>.lock``
      files. Any whose PR number is not in ``open_pr_numbers`` is deleted,
      unless another process currently holds an exclusive flock on it (that
      indicates a running supervisor or loop and must not be touched).
    - Scans ``worktree_root`` for ``pr-<N>-*`` directories. For any PR not in
      ``open_pr_numbers``, attempts ``git worktree remove --force <path>``.
      If git refuses or the path is not a registered worktree, falls back to
      ``shutil.rmtree`` after verifying the path resolves under the
      worktree_root.
    - Refuses to touch any path outside ``tempfile.gettempdir()`` (locks) or
      outside ``worktree_root`` (directories).

    Returns a dict with keys ``locks_removed`` and ``worktrees_removed``.
    """
    counts = {"locks_removed": 0, "worktrees_removed": 0}
    tmp_root = tempfile.gettempdir()
    try:
        tmp_entries = os.listdir(tmp_root)
    except OSError as exc:
        _print_step(
            "Stale-state cleanup: could not list tempdir {}: {}".format(tmp_root, exc)
        )
        tmp_entries = []
    for name in tmp_entries:
        match = _LOCK_FILENAME_PR_RE.match(name)
        if not match:
            continue
        try:
            pr_number = int(match.group(1))
        except ValueError:
            continue
        if pr_number in open_pr_numbers:
            continue
        lock_path = os.path.join(tmp_root, name)
        if os.path.islink(lock_path):
            _print_step(
                "Stale-state cleanup: refusing to delete symlink lock {}".format(
                    lock_path
                )
            )
            continue
        if not _is_path_within(lock_path, tmp_root):
            _print_step(
                "Stale-state cleanup: refusing to delete lock outside tempdir: {}".format(
                    lock_path
                )
            )
            continue
        if _lock_is_held_by_other_process(lock_path):
            _print_step(
                "Stale-state cleanup: lock {} is held by another process; "
                "leaving it in place.".format(lock_path)
            )
            continue
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _print_step(
                "Stale-state cleanup: failed to remove lock {}: {}".format(
                    lock_path, exc
                )
            )
            continue
        counts["locks_removed"] += 1
        _print_step(
            "Stale-state cleanup: removed lock {} (PR #{} not open)".format(
                lock_path, pr_number
            )
        )

    if worktree_root and os.path.isdir(worktree_root):
        worktree_root_real = os.path.realpath(worktree_root)
        try:
            wt_entries = os.listdir(worktree_root)
        except OSError as exc:
            _print_step(
                "Stale-state cleanup: could not list worktree root {}: {}".format(
                    worktree_root, exc
                )
            )
            wt_entries = []
        for name in wt_entries:
            match = _WORKTREE_DIR_PR_RE.match(name)
            if not match:
                continue
            try:
                pr_number = int(match.group(1))
            except ValueError:
                continue
            if pr_number in open_pr_numbers:
                continue
            entry_path = os.path.join(worktree_root, name)
            if not _is_path_within(entry_path, worktree_root_real):
                _print_step(
                    "Stale-state cleanup: refusing to remove path outside "
                    "worktree root: {}".format(entry_path)
                )
                continue
            removed = False
            try:
                result = _run_command(
                    ["git", "worktree", "remove", "--force", entry_path],
                    check=False,
                    capture_output=True,
                )
                if result.returncode == 0:
                    removed = True
                    _print_step(
                        "Stale-state cleanup: git worktree removed {} (PR #{} "
                        "not open)".format(entry_path, pr_number)
                    )
            except CommandError as exc:
                _print_step(
                    "Stale-state cleanup: git worktree remove failed for "
                    "{}: {}".format(entry_path, exc)
                )
            if not removed and os.path.exists(entry_path):
                # Re-verify the path is still safely inside worktree_root
                # before fall-back rmtree (paranoia against TOCTOU).
                if not _is_path_within(entry_path, worktree_root_real):
                    _print_step(
                        "Stale-state cleanup: refusing rmtree of path that no "
                        "longer resolves under worktree root: {}".format(entry_path)
                    )
                    continue
                if os.path.islink(entry_path):
                    _print_step(
                        "Stale-state cleanup: refusing to rmtree symlink "
                        "{}".format(entry_path)
                    )
                    continue
                try:
                    shutil.rmtree(entry_path)
                    removed = True
                    _print_step(
                        "Stale-state cleanup: rmtree fallback removed {} (PR #{} "
                        "not open)".format(entry_path, pr_number)
                    )
                except OSError as exc:
                    _print_step(
                        "Stale-state cleanup: rmtree failed for {}: {}".format(
                            entry_path, exc
                        )
                    )
            if removed:
                counts["worktrees_removed"] += 1

    _print_step(
        "Stale-state cleanup: removed {} lock file(s) and {} worktree "
        "directory(ies).".format(counts["locks_removed"], counts["worktrees_removed"])
    )
    return counts


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


def _worktree_path_is_registered(path: str) -> bool:
    completed = _run_command(
        ["git", "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
    )
    expected = os.path.realpath(path)
    for line in (completed.stdout or "").splitlines():
        if line.startswith("worktree "):
            if os.path.realpath(line[len("worktree ") :]) == expected:
                return True
    return False


def _ensure_worktree_origin_matches(path: str):
    source_origin = _run_command(
        ["git", "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
    )
    worktree_origin = _run_command(
        ["git", "-C", path, "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
    )
    if (source_origin.stdout or "").strip() != (worktree_origin.stdout or "").strip():
        raise CommandError(
            "Existing worktree {} origin remote does not match the launching repo.".format(
                path
            )
        )


def _sync_existing_worktree(*, path: str, start_ref: str):
    rebase_abort = _run_command(
        ["git", "-C", path, "rebase", "--abort"],
        check=False,
        capture_output=True,
        replay_output=False,
    )
    rebase_abort_text = "{}\n{}".format(
        rebase_abort.stdout or "", rebase_abort.stderr or ""
    ).lower()
    if rebase_abort.returncode == 0:
        _print_step("Aborted interrupted rebase in worktree {}".format(path))
    elif (
        "no rebase" not in rebase_abort_text
        and "no rebase in progress" not in rebase_abort_text
    ):
        raise CommandError(
            "Could not abort interrupted rebase in worktree {}: {}".format(
                path,
                (rebase_abort.stderr or rebase_abort.stdout or "").strip(),
            )
        )
    status = _run_command(
        ["git", "-C", path, "status", "--porcelain"],
        check=True,
        capture_output=True,
    )
    if (status.stdout or "").strip():
        _print_step(
            "Discarding uncommitted mid-flight changes in worktree {}".format(path)
        )
        _run_command(
            ["git", "-C", path, "reset", "--hard", "HEAD"],
            check=True,
            capture_output=True,
        )
        _run_command(
            ["git", "-C", path, "clean", "-fdx"],
            check=True,
            capture_output=True,
        )
    head = _run_command(
        ["git", "-C", path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
    )
    head_sha = (head.stdout or "").strip()
    ancestor = _run_command(
        ["git", "-C", path, "merge-base", "--is-ancestor", head_sha, start_ref],
        check=False,
        capture_output=True,
    )
    if ancestor.returncode != 0:
        raise CommandError(
            "Existing PR worktree {} is not an ancestor of {}; refusing to drop local commits.".format(
                path, start_ref
            )
        )
    _run_command(
        ["git", "-C", path, "reset", "--hard", start_ref],
        check=True,
        capture_output=True,
    )


def _local_branch_exists(branch: str) -> bool:
    result = _run_command(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/{}".format(branch)],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


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
    if existing_branch_worktree:
        existing_abs = os.path.realpath(existing_branch_worktree)
        cwd_abs = os.path.realpath(os.getcwd())
        desired_abs = os.path.realpath(path)
        if existing_abs == cwd_abs and existing_abs != desired_abs:
            _print_step(
                "PR branch '{}' is already checked out at the target directory {}; "
                "operating in place rather than creating a separate worktree.".format(
                    branch, existing_abs
                )
            )
            _ensure_worktree_origin_matches(existing_abs)
            _sync_existing_worktree(path=existing_abs, start_ref=start_ref)
            return existing_abs
        if existing_abs != desired_abs:
            sys.stdout.write("{}\n".format(LOOP_ALREADY_RUNNING_MESSAGE))
            sys.stdout.flush()
            _print_step(
                "PR branch '{}' is already checked out at {}; refusing to run outside {}".format(
                    branch,
                    existing_branch_worktree,
                    path,
                )
            )
            raise SystemExit(LOOP_ALREADY_RUNNING_EXIT_CODE)
    if os.path.isdir(path):
        _print_step("Using existing PR worktree {}".format(path))
        if not _worktree_path_is_registered(path):
            raise CommandError(
                "Existing worktree path {} is not registered for the launching repo.".format(
                    path
                )
            )
        _ensure_worktree_origin_matches(path)
        worktree_branch = _run_command(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
        )
        current = (worktree_branch.stdout or "").strip()
        if current != branch:
            _print_step(
                "Worktree {} is on '{}' instead of '{}'; restoring expected "
                "branch.".format(path, current or "<unknown>", branch)
            )
            _run_command(
                ["git", "-C", path, "reset", "--hard", "HEAD"],
                check=False,
                capture_output=True,
            )
            _run_command(
                ["git", "-C", path, "clean", "-fdx"],
                check=False,
                capture_output=True,
            )
            switch = _run_command(
                ["git", "-C", path, "checkout", "-f", branch],
                check=False,
                capture_output=True,
            )
            if switch.returncode != 0:
                raise CommandError(
                    "Could not restore branch '{}' in worktree {}: {}".format(
                        branch,
                        path,
                        (switch.stderr or switch.stdout or "").strip(),
                    )
                )
        _sync_existing_worktree(path=path, start_ref=start_ref)
        return path
    _print_step("Creating PR worktree {}".format(path))
    if _local_branch_exists(branch):
        add_cmd = ["git", "worktree", "add", path, branch]
    else:
        add_cmd = ["git", "worktree", "add", path, "-b", branch, start_ref]
    result = _run_command(
        add_cmd,
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return path
    stderr = "{}\n{}".format(result.stdout or "", result.stderr or "").lower()
    if "already used by worktree" in stderr:
        sys.stdout.write("{}\n".format(LOOP_ALREADY_RUNNING_MESSAGE))
        sys.stdout.flush()
        raise SystemExit(LOOP_ALREADY_RUNNING_EXIT_CODE)
    raise CommandError(
        "Unable to create worktree for branch '{}': {}".format(
            branch,
            (result.stderr or result.stdout or "").strip(),
        )
    )
