"""Git command helpers."""
from __future__ import annotations

import sys
from typing import Optional, Sequence

from .config import LOOP_ALREADY_RUNNING_MESSAGE
from .errors import CommandError, RebaseConflictError
from .process import _print_step, _run_command

def _git_output(args: Sequence[str]) -> str:
    completed = _run_command(["git"] + list(args), check=True, capture_output=True)
    return (completed.stdout or "").strip()


def _git_config_get(key: str) -> str:
    completed = _run_command(
        ["git", "config", "--get", key], check=False, capture_output=True
    )
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def _git_branch() -> str:
    branch = _git_output(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch == "HEAD":
        raise CommandError("Detached HEAD is not supported for this workflow.")
    return branch


def _git_head_sha() -> str:
    return _git_output(["rev-parse", "HEAD"])


def _working_tree_dirty() -> bool:
    return bool(_git_output(["status", "--porcelain"]))


def _checkout_branch(branch: str):
    current_branch = _git_branch()
    if current_branch == branch:
        return
    _print_step("Switching to PR branch {}".format(branch))
    _run_command(["git", "fetch", "origin", branch], check=True, capture_output=True)
    has_local_branch = bool(_git_output(["branch", "--list", branch]))
    if has_local_branch:
        checkout_result = _run_command(
            ["git", "checkout", branch], check=False, capture_output=True
        )
    else:
        checkout_result = _run_command(
            ["git", "checkout", "-b", branch, "--track", "origin/{}".format(branch)],
            check=False,
            capture_output=True,
        )
    if checkout_result.returncode == 0:
        return
    stderr = "{}\n{}".format(
        checkout_result.stdout or "", checkout_result.stderr or ""
    ).lower()
    if "already used by worktree" in stderr:
        sys.stdout.write("{}\n".format(LOOP_ALREADY_RUNNING_MESSAGE))
        sys.stdout.flush()
        raise SystemExit(0)
    raise CommandError(
        "Unable to checkout branch '{}': {}".format(
            branch, (checkout_result.stderr or checkout_result.stdout or "").strip()
        )
    )


def _reset_generated_changes(target_sha: Optional[str] = None):
    target = target_sha or "HEAD"
    head_sha = _git_head_sha()
    dirty = _working_tree_dirty()
    if not dirty and target_sha and head_sha == target_sha:
        _print_step("Cleaning generated files (git clean -fdx)")
        _run_command(["git", "clean", "-fdx"], check=True, capture_output=True)
        return
    if not dirty and not target_sha:
        _print_step("Cleaning generated files (git clean -fdx)")
        _run_command(["git", "clean", "-fdx"], check=True, capture_output=True)
        return
    _print_step(
        "Resetting generated changes (git reset --hard {} + git clean -fdx)".format(
            target
        )
    )
    _run_command(["git", "reset", "--hard", target], check=True, capture_output=True)
    _run_command(["git", "clean", "-fdx"], check=True, capture_output=True)


_FETCH_TRANSIENT_PATTERNS = (
    "unable to update local ref",
    "cannot lock ref",
    "could not lock config file",
    "ref-pack",
    "fatal: unable to access",
    "fatal: early eof",
)

_FETCH_MAX_ATTEMPTS = 6

_REBASE_CONFLICT_PATTERNS = (
    "conflict (",
    "could not apply",
    "resolve all conflicts",
    "after resolving the conflicts",
)


def _fetch_with_retry(remote: str, ref: str):
    import random as _random
    import time as _time

    last_exc: Optional[CommandError] = None
    for attempt in range(_FETCH_MAX_ATTEMPTS):
        try:
            _run_command(
                ["git", "fetch", remote, ref], check=True, capture_output=True
            )
            return
        except CommandError as exc:
            text = str(exc).lower()
            if not any(p in text for p in _FETCH_TRANSIENT_PATTERNS):
                raise
            last_exc = exc
            delay = 0.5 * (2 ** attempt)
            _time.sleep(delay + _random.uniform(0, delay))
    assert last_exc is not None
    raise last_exc


def _rebase_onto_base(branch: str, base: str):
    _print_step("Rebasing {} onto origin/{}".format(branch, base))
    _fetch_with_retry("origin", base)
    rebase = _run_command(
        ["git", "rebase", "origin/{}".format(base)], check=False, capture_output=True
    )
    if rebase.returncode != 0:
        output = "{}\n{}".format(rebase.stdout or "", rebase.stderr or "").strip()
        if any(pattern in output.lower() for pattern in _REBASE_CONFLICT_PATTERNS):
            _run_command(
                ["git", "rebase", "--abort"], check=False, capture_output=True
            )
            raise RebaseConflictError(
                "Rebase conflict while rebasing '{}' onto origin/{}:\n{}".format(
                    branch, base, output
                )
            )
        raise CommandError(
            "Command failed (exit={}): git rebase origin/{}\n{}".format(
                rebase.returncode, base, output
            ).strip()
        )
    _run_command(
        ["git", "push", "--force-with-lease", "origin", branch],
        check=True,
        capture_output=True,
    )
