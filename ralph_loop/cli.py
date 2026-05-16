"""Command-line interface and top-level orchestration."""
from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .checks import _wait_for_required_checks_green
from .codex_agent import _run_ci_fix_round, _run_review_fix_round
from .config import DEFAULT_WORKTREE_ROOT
from .errors import CommandError
from .gh_ops import (
    _list_open_prs,
    _mark_pr_needs_review,
    _merge_pr,
    _pr_view,
    _prepare_pr_for_merge,
)
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
        "--all-prs",
        action="store_true",
        help=(
            "Fan out: discover all open non-draft PRs targeting --base in the "
            "target directory and launch one ralph loop per PR in parallel "
            "(passes through all other flags). Mutually exclusive with --pr."
        ),
    )
    parser.add_argument(
        "--fan-out-log-dir",
        default=None,
        help=(
            "Directory to write per-PR fan-out logs into. Defaults to "
            "<ralph-script-dir>/.ralph-logs/fan-out so logs stay outside "
            "the target repository."
        ),
    )
    parser.add_argument(
        "--fan-out-stuck-timeout-seconds",
        type=_pos_int,
        default=900,
        help=(
            "Kill and respawn any fan-out child whose log file has not been "
            "written to in this many seconds. Minimum 60."
        ),
    )
    parser.add_argument(
        "--fan-out-respawn-backoff-seconds",
        type=_pos_int,
        default=5,
        help="Seconds to wait before respawning an exited fan-out child.",
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
    args = parser.parse_args()
    if args.all_prs and args.pr is not None:
        parser.error("--all-prs cannot be combined with --pr")
    return args


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


def _should_fan_out_implicitly(args: argparse.Namespace) -> bool:
    if args.all_prs or args.pr is not None:
        return False
    current_branch = _git_branch()
    return current_branch == args.base


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


def _passthrough_args(argv: List[str]) -> List[str]:
    """Return argv (minus argv[0]) with --all-prs removed for child fan-out."""
    out: List[str] = []
    skip_next = False
    for token in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if token == "--all-prs":
            continue
        out.append(token)
    return out


def _spawn_child(
    *,
    pr: int,
    script_path: str,
    base_child_args: List[str],
    log_root: str,
) -> Tuple[subprocess.Popen, str, Any]:
    cmd = [sys.executable, script_path] + base_child_args + ["--pr", str(pr)]
    log_path = os.path.join(log_root, "pr-{}.log".format(pr))
    log_handle = open(log_path, "ab", buffering=0)
    log_handle.write(
        "\n=== spawn {} ===\n$ {}\n".format(
            time.strftime("%Y-%m-%d %H:%M:%S"), shlex.join(cmd)
        ).encode("utf-8", errors="replace")
    )
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    return proc, log_path, log_handle


def _fan_out_all_prs(
    args: argparse.Namespace, argv: List[str], script_path: str
) -> int:
    pr_numbers = _list_open_prs(args.base)
    if not pr_numbers:
        _print_step(
            "No open non-draft PRs found targeting base '{}'.".format(args.base)
        )
        return 0
    if args.fan_out_log_dir:
        log_root = os.path.abspath(os.path.expanduser(args.fan_out_log_dir))
    else:
        log_root = os.path.join(
            os.path.dirname(script_path), ".ralph-logs", "fan-out"
        )
    os.makedirs(log_root, exist_ok=True)
    stuck_timeout = max(60, args.fan_out_stuck_timeout_seconds)
    respawn_backoff = max(1, args.fan_out_respawn_backoff_seconds)
    _print_step(
        "Fan-out supervisor: {} open PR(s): {} (logs in {}; stuck timeout {}s; "
        "respawn backoff {}s)".format(
            len(pr_numbers),
            ", ".join("#" + str(n) for n in pr_numbers),
            log_root,
            stuck_timeout,
            respawn_backoff,
        )
    )
    base_child_args = _passthrough_args(argv)
    children: Dict[int, Tuple[subprocess.Popen, str, Any, float]] = {}
    last_exit_at: Dict[int, float] = {}
    shutting_down = {"flag": False}

    def _request_shutdown(signum, _frame):
        shutting_down["flag"] = True
        sys.stderr.write(
            "\nSupervisor received {}; terminating children...\n".format(
                signal.Signals(signum).name
            )
        )
        sys.stderr.flush()

    previous_int = signal.signal(signal.SIGINT, _request_shutdown)
    previous_term = signal.signal(signal.SIGTERM, _request_shutdown)
    try:
        for pr in pr_numbers:
            proc, log_path, log_handle = _spawn_child(
                pr=pr,
                script_path=script_path,
                base_child_args=base_child_args,
                log_root=log_root,
            )
            children[pr] = (proc, log_path, log_handle, time.monotonic())
            _print_step(
                "Launched PR #{} pid={} (log: {})".format(pr, proc.pid, log_path)
            )
        while not shutting_down["flag"] and (children or last_exit_at):
            time.sleep(min(10, stuck_timeout))
            now = time.monotonic()
            for pr in list(children.keys()):
                proc, log_path, log_handle, _spawned_at = children[pr]
                rc = proc.poll()
                if rc is not None:
                    try:
                        log_handle.close()
                    except OSError:
                        pass
                    _print_step(
                        "PR #{} loop exited with code {} (log: {}); respawning "
                        "after {}s".format(pr, rc, log_path, respawn_backoff)
                    )
                    last_exit_at[pr] = now
                    del children[pr]
                    continue
                try:
                    mtime = os.path.getmtime(log_path)
                except OSError:
                    mtime = time.time()
                if time.time() - mtime > stuck_timeout:
                    _print_step(
                        "PR #{} log idle >{}s; killing pid={} for respawn".format(
                            pr, stuck_timeout, proc.pid
                        )
                    )
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                    try:
                        log_handle.close()
                    except OSError:
                        pass
                    last_exit_at[pr] = now
                    del children[pr]
            if shutting_down["flag"]:
                break
            for pr in list(last_exit_at.keys()):
                if pr in children:
                    continue
                if time.monotonic() - last_exit_at[pr] < respawn_backoff:
                    continue
                proc, log_path, log_handle = _spawn_child(
                    pr=pr,
                    script_path=script_path,
                    base_child_args=base_child_args,
                    log_root=log_root,
                )
                children[pr] = (proc, log_path, log_handle, time.monotonic())
                last_exit_at.pop(pr, None)
                _print_step(
                    "Respawned PR #{} pid={} (log: {})".format(
                        pr, proc.pid, log_path
                    )
                )
    finally:
        for pr, (proc, _log_path, log_handle, _spawned_at) in list(children.items()):
            try:
                proc.terminate()
            except OSError:
                pass
        for pr, (proc, _log_path, log_handle, _spawned_at) in list(children.items()):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            try:
                log_handle.close()
            except OSError:
                pass
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
    _print_step("Fan-out supervisor exited cleanly.")
    return 0


def main() -> int:
    original_cwd = os.getcwd()
    script_path = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
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
    fan_out = args.all_prs or _should_fan_out_implicitly(args)
    if fan_out:
        if not args.all_prs:
            _print_step(
                "On base branch '{}' with no --pr; fanning out to all open PRs.".format(
                    args.base
                )
            )
        if not script_path or not os.path.isfile(script_path):
            raise CommandError(
                "Cannot resolve ralph script path for fan-out: {!r}".format(
                    sys.argv[0] if sys.argv else ""
                )
            )
        try:
            return _fan_out_all_prs(args, sys.argv, script_path)
        finally:
            try:
                os.chdir(original_cwd)
            except OSError:
                pass
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
