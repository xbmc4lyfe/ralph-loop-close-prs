"""Command-line interface and top-level orchestration."""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Any, Dict, Optional, Tuple

from .checks import _wait_for_required_checks_green
from .codex_agent import _run_ci_fix_round, _run_review_fix_round
from .config import DEFAULT_WORKTREE_ROOT
from .errors import CommandError
from .gh_ops import _mark_pr_needs_review, _merge_pr, _pr_view, _prepare_pr_for_merge
from .git_ops import (
    _git_branch,
    _git_head_sha,
    _rebase_onto_base,
    _reset_generated_changes,
    _working_tree_dirty,
)
from .identity import _ensure_runtime_identity, _validate_identity_and_signing
from .process import _print_step, _set_command_deadline
from .quality import _commit_and_push
from .runtime import _check_wall_clock, _round_numbers
from .worktrees import _acquire_loop_lock, _ensure_pr_worktree

def _nonneg_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            "expected a non-negative integer, got {!r}".format(value)
        )
    if parsed < 0:
        raise argparse.ArgumentTypeError(
            "expected a non-negative integer, got {}".format(parsed)
        )
    return parsed


def _pos_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            "expected a positive integer, got {!r}".format(value)
        )
    if parsed <= 0:
        raise argparse.ArgumentTypeError(
            "expected a positive integer, got {}".format(parsed)
        )
    return parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a Codex /review repair loop, then CI repair, then rebase+merge."
    )
    parser.add_argument(
        "--pr",
        type=_pos_int,
        default=None,
        help="Target PR number. If provided, run against that PR and its head branch.",
    )
    parser.add_argument(
        "--base", default="main", help="Base branch for review and rebase."
    )
    parser.add_argument(
        "--max-review-rounds",
        type=_nonneg_int,
        default=0,
        help="Maximum Codex review/fix rounds before aborting. Use 0 for unlimited.",
    )
    parser.add_argument(
        "--max-ci-rounds",
        type=_nonneg_int,
        default=0,
        help="Maximum Codex CI fix rounds before aborting. Use 0 for unlimited.",
    )
    parser.add_argument(
        "--max-local-quality-rounds",
        type=_nonneg_int,
        default=0,
        help=(
            "Maximum Codex repair rounds for local just ci/test failures before aborting. "
            "Use 0 for unlimited."
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=_pos_int,
        default=20,
        help="Polling interval for required checks (positive integer seconds).",
    )
    parser.add_argument(
        "--checks-timeout-seconds",
        type=_pos_int,
        default=5400,
        help="Timeout for a single required-check wait cycle (positive integer seconds).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional Codex model override passed as --model.",
    )
    parser.add_argument(
        "--skip-rebase",
        action="store_true",
        help="Skip both initial and final rebase steps.",
    )
    parser.add_argument(
        "--skip-merge",
        action="store_true",
        help="Stop after CI is green (and optional rebase), without merging.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Resolve and validate the PR, then stop before identity changes, "
            "worktree setup, Codex, quality gates, rebase, push, approval, or merge."
        ),
    )
    parser.add_argument(
        "--worktree-root",
        default=DEFAULT_WORKTREE_ROOT,
        help="Directory where per-PR git worktrees are created.",
    )
    parser.add_argument(
        "--max-wall-clock-seconds",
        type=_nonneg_int,
        default=0,
        help=(
            "Global wall-clock timeout (seconds) for the entire run. Enforced "
            "at review/CI loop boundaries, local-quality repair boundaries, "
            "subprocesses, and required-check polling. Use 0 for unlimited "
            "(the default)."
        ),
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=None,
        help=(
            "Target repository directory to run against. Defaults to the "
            "current working directory."
        ),
    )
    return parser.parse_args()


def _resolve_pr_data(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    if args.pr is not None:
        pr_ref = str(args.pr)
    else:
        current_branch = _git_branch()
        if current_branch.isdigit():
            raise CommandError(
                "Current branch has numeric branch name '{}'; pass --pr explicitly "
                "to avoid ambiguity with GitHub PR numbers.".format(current_branch)
            )
        pr_ref = current_branch
    pr_data = _pr_view(pr_ref)
    pr_number = pr_data.get("number")
    if not isinstance(pr_number, int):
        raise CommandError("Could not resolve PR number for '{}'.".format(pr_ref))
    return pr_data, pr_number


def _validate_pr_metadata(
    pr_data: Dict[str, Any], pr_number: int, expected_base: str
) -> str:
    if pr_data.get("state") != "OPEN":
        raise CommandError(
            "PR {} is not open (state={}).".format(
                pr_number, pr_data.get("state")
            )
        )
    if pr_data.get("isDraft"):
        raise CommandError(
            "PR is in draft state; mark it ready before merge automation."
        )
    if pr_data.get("isCrossRepository"):
        raise CommandError(
            "fork PRs are not supported because Ralph pushes fixes to origin/<branch>."
        )
    pr_base = pr_data.get("baseRefName")
    if pr_base and pr_base != expected_base:
        raise CommandError(
            "PR #{} targets base '{}' but --base is '{}'. Use the PR base branch.".format(
                pr_number, pr_base, expected_base
            )
        )

    branch = pr_data.get("headRefName")
    if not branch:
        raise CommandError("Could not resolve PR head branch.")
    if branch == expected_base:
        raise CommandError(
            "PR head branch '{}' matches base '{}'; aborting.".format(
                branch, expected_base
            )
        )
    return branch


def _run_dry_run(args: argparse.Namespace) -> int:
    pr_data, pr_number = _resolve_pr_data(args)
    branch = _validate_pr_metadata(pr_data, pr_number, args.base)
    _print_step(
        "Dry run: validated PR #{} {} on branch '{}' targeting base '{}'.".format(
            pr_number, pr_data.get("url", ""), branch, args.base
        )
    )
    _print_step(
        "Dry run: stopped before identity changes, worktree setup, Codex, "
        "quality gates, rebase, push, reset, approval, merge, or branch deletion."
    )
    return 0


def main() -> int:
    original_cwd = os.getcwd()
    args = _parse_args()
    if args.directory is not None:
        target_dir = os.path.abspath(os.path.expanduser(args.directory))
        if not os.path.isdir(target_dir):
            raise CommandError(
                "Target directory does not exist or is not a directory: {}".format(
                    args.directory
                )
            )
        os.chdir(target_dir)
    start_time = time.monotonic()
    deadline: Optional[float] = (
        start_time + args.max_wall_clock_seconds
        if args.max_wall_clock_seconds > 0
        else None
    )
    previous_command_deadline = _set_command_deadline(deadline)

    _shutdown_signal = {"name": None}

    def _handle_shutdown(signum, _frame):
        _shutdown_signal["name"] = signal.Signals(signum).name
        sys.stderr.write(
            "\nReceived {}; cleaning up...\n".format(_shutdown_signal["name"])
        )
        sys.stderr.flush()
        raise SystemExit(130 if signum == signal.SIGINT else 143)

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    pr_loop_lock = None
    try:
        try:
            pr_data, pr_number = _resolve_pr_data(args)
        except CommandError as exc:
            if not args.dry_run and "Could not resolve PR number" not in str(exc):
                _ensure_runtime_identity()
            raise
        branch = _validate_pr_metadata(pr_data, pr_number, args.base)
        if args.dry_run:
            _print_step(
                "Dry run: validated PR #{} {} on branch '{}' targeting base '{}'.".format(
                    pr_number, pr_data.get("url", ""), branch, args.base
                )
            )
            _print_step(
                "Dry run: stopped before identity changes, worktree setup, Codex, "
                "quality gates, rebase, push, reset, approval, merge, or branch deletion."
            )
            return 0
        _ensure_runtime_identity()
        pr_loop_lock = _acquire_loop_lock(pr_number=pr_number)

        worktree = _ensure_pr_worktree(
            worktree_root=args.worktree_root,
            pr_number=pr_number,
            branch=branch,
        )
        os.chdir(worktree)
        _print_step("Working in PR worktree {}".format(worktree))
        _validate_identity_and_signing()
        _ensure_runtime_identity()
        if _working_tree_dirty():
            raise CommandError(
                "Worktree is dirty. Commit/stash changes before running this loop: {}".format(
                    worktree
                )
            )

        pr_target = str(pr_number)
        _print_step(
            "Using PR #{} {}".format(pr_number, pr_data.get("url", ""))
        )
        _mark_pr_needs_review(pr_target)
        if not args.skip_rebase:
            _print_step("Initial rebase before review/fix loop")
            _rebase_onto_base(branch, args.base)

        review_passed = False
        last_review_round = 0
        for round_number in _round_numbers(args.max_review_rounds):
            _check_wall_clock(deadline)
            last_review_round = round_number
            pre_round_sha = _git_head_sha()
            review_passed = _run_review_fix_round(round_number, args.base, args.model)
            if not review_passed:
                commit_state = _commit_and_push(
                    "review round {}".format(round_number),
                    branch,
                    base=args.base,
                    model=args.model,
                    require_review_gate=False,
                    review_gate_after_quality_fix=False,
                    max_local_quality_rounds=args.max_local_quality_rounds,
                    pre_round_sha=pre_round_sha,
                    deadline=deadline,
                )
                if commit_state == "discarded":
                    _print_step(
                        "Review round {} changes were not useful; retrying with a fresh context window and Codex session.".format(
                            round_number
                        )
                    )
                    continue
                if commit_state == "no_changes":
                    _reset_generated_changes(pre_round_sha)
                _print_step(
                    "Review round {} still has actionable findings; retrying with a fresh context window and Codex session.".format(
                        round_number
                    )
                )
                continue
            commit_state = _commit_and_push(
                "review round {}".format(round_number),
                branch,
                base=args.base,
                model=args.model,
                require_review_gate=False,
                review_gate_after_quality_fix=True,
                max_local_quality_rounds=args.max_local_quality_rounds,
                pre_round_sha=pre_round_sha,
                deadline=deadline,
            )
            if commit_state == "discarded":
                review_passed = False
                _print_step(
                    "Review round {} changes were not useful; retrying with a fresh context window and Codex session.".format(
                        round_number
                    )
                )
                continue
            if commit_state == "no_changes":
                _print_step(
                    "Review round {} passed with no file changes to commit.".format(
                        round_number
                    )
                )
            break
        if not review_passed:
            if args.max_review_rounds > 0:
                raise CommandError(
                    "Review loop exhausted {} rounds without pass.".format(
                        args.max_review_rounds
                    )
                )
            raise CommandError(
                "Review loop stopped unexpectedly after {} rounds without pass.".format(
                    last_review_round
                )
            )

        ci_green = False
        last_ci_round = 0
        for round_number in _round_numbers(args.max_ci_rounds):
            _check_wall_clock(deadline)
            last_ci_round = round_number
            ci_green, checks = _wait_for_required_checks_green(
                branch=branch,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.checks_timeout_seconds,
                deadline=deadline,
            )
            if ci_green:
                break
            pre_round_sha = _git_head_sha()
            ready = _run_ci_fix_round(
                round_number=round_number,
                checks=checks,
                model=args.model,
            )
            if not ready:
                _reset_generated_changes(pre_round_sha)
                _print_step(
                    "CI round {} did not produce a useful fix; retrying with a fresh context window and Codex session.".format(
                        round_number
                    )
                )
                continue
            commit_state = _commit_and_push(
                "ci round {}".format(round_number),
                branch,
                base=args.base,
                model=args.model,
                require_review_gate=True,
                review_gate_after_quality_fix=True,
                max_local_quality_rounds=args.max_local_quality_rounds,
                pre_round_sha=pre_round_sha,
                deadline=deadline,
            )
            if commit_state == "discarded":
                _print_step(
                    "CI round {} changes were not useful; retrying with a fresh context window and Codex session.".format(
                        round_number
                    )
                )
                continue
            if commit_state == "no_changes":
                _print_step(
                    "CI round {} produced no file changes; retrying with a fresh context window and Codex session.".format(
                        round_number
                    )
                )
                continue
            ci_green, checks = _wait_for_required_checks_green(
                branch=branch,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.checks_timeout_seconds,
                deadline=deadline,
            )
            if ci_green:
                break
        if not ci_green:
            if args.max_ci_rounds > 0:
                raise CommandError(
                    "CI loop exhausted {} rounds without green checks.".format(
                        args.max_ci_rounds
                    )
                )
            raise CommandError(
                "CI loop stopped unexpectedly after {} rounds without green checks.".format(
                    last_ci_round
                )
            )

        if not args.skip_rebase:
            _rebase_onto_base(branch, args.base)
            ci_green_after_rebase, _ = _wait_for_required_checks_green(
                branch=branch,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.checks_timeout_seconds,
                deadline=deadline,
            )
            if not ci_green_after_rebase:
                raise CommandError("Required checks failed after rebase.")

        if not args.skip_merge:
            fresh_pr_data = _pr_view(str(pr_number))
            fresh_branch = _validate_pr_metadata(
                fresh_pr_data, pr_number, args.base
            )
            if fresh_branch != branch:
                raise CommandError(
                    "PR #{} head branch changed from '{}' to '{}' during the run.".format(
                        pr_number, branch, fresh_branch
                    )
                )
            _prepare_pr_for_merge(str(pr_number))
            _merge_pr(pr_target)
        _print_step("Done.")
        return 0
    finally:
        _set_command_deadline(previous_command_deadline)
        if pr_loop_lock is not None:
            pr_loop_lock.release()
        try:
            os.chdir(original_cwd)
        except OSError:
            pass
