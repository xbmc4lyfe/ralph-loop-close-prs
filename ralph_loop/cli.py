"""Command-line interface and top-level orchestration."""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

from .checks import _wait_for_required_checks_green
from .codex_agent import _run_ci_fix_round, _run_review_fix_round
from .config import DEFAULT_WORKTREE_ROOT
from .errors import (
    CODEX_ENV_FAILURE_EXIT_CODE,
    LOOP_ALREADY_RUNNING_EXIT_CODE,
    REBASE_CONFLICT_EXIT_CODE,
    CodexEnvironmentError,
    CommandError,
    RebaseConflictError,
)
from .gh_ops import (
    _list_open_prs,
    _mark_pr_needs_review,
    _merge_pr,
    _pr_is_still_open,
    _pr_review_comments,
    _pr_view,
    _prepare_pr_for_merge,
    _reply_to_pr_review_comment,
)
from .git_ops import (
    _git_branch,
    _git_head_sha,
    _rebase_onto_base,
    _reset_generated_changes,
    _working_tree_dirty,
)
from .identity import _ensure_runtime_identity, _validate_identity_and_signing
from .process import (
    _configure_json_log,
    _print_step,
    _run_command,
    _set_command_deadline,
)
from .quality import LocalQualityTelemetry, _commit_and_push
from .runtime import _check_wall_clock
from .worktrees import (
    _acquire_loop_lock,
    _cleanup_stale_loop_state,
    _ensure_pr_worktree,
)


def _write_stderr_fd(message: str):
    try:
        os.write(2, message.encode("utf-8", errors="replace"))
    except OSError:
        pass


@dataclass
class _RunTelemetry:
    started_at: float
    review_rounds: int = 0
    ci_waits: int = 0
    ci_repair_rounds: int = 0
    local_quality_repair_rounds: int = 0
    phase_seconds: Dict[str, float] = field(default_factory=dict)

    @contextmanager
    def phase(self, name: str):
        phase_started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - phase_started
            self.phase_seconds[name] = self.phase_seconds.get(name, 0.0) + elapsed

    def record_local_quality(self, local: LocalQualityTelemetry) -> None:
        self.local_quality_repair_rounds += local.repair_rounds

    def emit(self) -> None:
        total_seconds = time.monotonic() - self.started_at
        review_seconds = self.phase_seconds.get("review", 0.0)
        ci_seconds = self.phase_seconds.get("ci", 0.0)
        _print_step(
            "Telemetry review_rounds={review_rounds} ci_waits={ci_waits} "
            "ci_repair_rounds={ci_repair_rounds} "
            "local_quality_repair_rounds={local_quality_repair_rounds} "
            "review_seconds={review_seconds:.2f} ci_seconds={ci_seconds:.2f} "
            "total_seconds={total_seconds:.2f}".format(
                review_rounds=self.review_rounds,
                ci_waits=self.ci_waits,
                ci_repair_rounds=self.ci_repair_rounds,
                local_quality_repair_rounds=self.local_quality_repair_rounds,
                review_seconds=review_seconds,
                ci_seconds=ci_seconds,
                total_seconds=total_seconds,
            ),
            event="run.telemetry",
            review_rounds=self.review_rounds,
            ci_waits=self.ci_waits,
            ci_repair_rounds=self.ci_repair_rounds,
            local_quality_repair_rounds=self.local_quality_repair_rounds,
            review_seconds=round(review_seconds, 2),
            ci_seconds=round(ci_seconds, 2),
            total_seconds=round(total_seconds, 2),
        )


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
        "--json-log",
        default=None,
        help="Append structured JSON-lines events to this path.",
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
        "--fan-out-env-failure-backoff-seconds",
        type=_pos_int,
        default=300,
        help=(
            "Seconds to wait before respawning a fan-out child that exited "
            "with the codex environmental-failure exit code ({}). Defaults to "
            "5 minutes so transient auth/transport issues don't burn tokens "
            "in a tight respawn loop.".format(CODEX_ENV_FAILURE_EXIT_CODE)
        ),
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help=(
            "Treat each positional directory as a parent and discover git "
            "repositories in its immediate subdirectories. Each discovered "
            "repo gets its own supervisor in parallel."
        ),
    )
    parser.add_argument(
        "directory",
        nargs="*",
        default=None,
        help=(
            "One or more target repository directories. With multiple, each "
            "gets its own supervisor in parallel. With --recursive, each "
            "directory is scanned for git repos in its immediate subdirs. "
            "Defaults to the current working directory."
        ),
    )
    args = parser.parse_args()
    if args.all_prs and args.pr is not None:
        parser.error("--all-prs cannot be combined with --pr")
    if args.pr is not None and args.directory and len(args.directory) > 1:
        parser.error("--pr cannot be combined with multiple directories")
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
            "PR #{} is from a fork / cross-repository branch; Ralph requires a "
            "same-repository PR because it rebases and pushes fixes to the head "
            "branch.".format(pr_number)
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
        ),
        event="dry_run.validated_pr",
        pr=pr_number,
        url=pr_data.get("url", ""),
        branch=branch,
        base=args.base,
    )
    _print_dry_run_simulation(args, pr_number, branch)
    _print_step(
        "Dry run: stopped before identity changes, worktree setup, Codex, "
        "quality gates, rebase, push, reset, approval, merge, or branch deletion.",
        event="dry_run.stopped_before_mutation",
        pr=pr_number,
        mutates=False,
    )
    return 0


def _dry_run_simulation_steps(
    args: argparse.Namespace, pr_number: int, branch: str
) -> List[str]:
    steps = [
        "would validate runtime identity and signing configuration",
        "would acquire per-PR lock for PR #{}".format(pr_number),
        "would create or reuse isolated worktree for branch '{}'".format(branch),
        "would mark PR #{} as needing review and run Codex review rounds".format(
            pr_number
        ),
        "would run local quality gates and push generated commits to '{}'".format(
            branch
        ),
        "would wait for required checks on PR #{} and run CI repair rounds".format(
            pr_number
        ),
    ]
    if not args.skip_rebase:
        steps.insert(
            3,
            "would rebase '{}' onto origin/{} before and after repairs".format(
                branch, args.base
            ),
        )
    if args.skip_merge:
        steps.append("would stop before approval, merge, or branch deletion")
    else:
        steps.append("would approve and merge PR #{}".format(pr_number))
        steps.append("would delete the same-repository remote branch if merge succeeds")
    return steps


def _print_dry_run_simulation(
    args: argparse.Namespace, pr_number: int, branch: str
) -> None:
    _print_step(
        "Dry run simulation plan:",
        event="dry_run.simulation_start",
        pr=pr_number,
    )
    for index, step in enumerate(
        _dry_run_simulation_steps(args, pr_number, branch), start=1
    ):
        _print_step(
            "Dry run: {}".format(step),
            event="dry_run.simulation_step",
            pr=pr_number,
            step=index,
            action=step,
            mutates=False,
        )


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


def _supervisor_wait(event: "threading.Event", timeout: float) -> bool:
    return event.wait(timeout=timeout)


def _post_addressed_comment_replies(
    pr_ref: str, addressed: List[Tuple[int, str]], round_number: int
) -> None:
    for comment_id, summary in addressed:
        summary_clean = (summary or "").strip()
        if not summary_clean:
            summary_clean = "(Codex did not provide a fix summary for this comment.)"
        body = (
            "**Ralph automated review/fix round {round_n}** — pushed a fix "
            "addressing this comment.\n\n"
            "**Summary of the change:**\n\n{summary}\n\n"
            "Please re-review the latest commit."
        ).format(round_n=round_number, summary=summary_clean)
        ok = _reply_to_pr_review_comment(pr_ref, comment_id, body)
        if ok:
            _print_step(
                "Replied to PR review comment #{} with fix summary "
                "({} chars).".format(comment_id, len(body))
            )
        else:
            _print_step(
                "Failed to reply to PR review comment #{} (continuing).".format(
                    comment_id
                )
            )


def _open_log_handle_cloexec(log_path: str) -> Any:
    """Open ``log_path`` for append in binary mode with FD_CLOEXEC set.

    Using ``O_CLOEXEC`` ensures the parent-side log file descriptor is closed
    automatically if the supervisor re-execs (e.g. SIGHUP reload) so we do not
    leak fds across reload cycles. ``subprocess.Popen`` still dup2's the fd
    into the child's stdout, and dup2 clears the close-on-exec flag for the
    duplicated descriptor, so child log redirection continues to work.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC
    fd = os.open(log_path, flags, 0o644)
    return os.fdopen(fd, "ab", buffering=0)


def _spawn_child(
    *,
    pr: int,
    script_path: str,
    base_child_args: List[str],
    log_root: str,
) -> Tuple[subprocess.Popen, str, Any]:
    cmd = [sys.executable, script_path] + base_child_args + ["--pr", str(pr)]
    log_path = os.path.join(log_root, "pr-{}.log".format(pr))
    log_handle = _open_log_handle_cloexec(log_path)
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


def _filter_to_still_open_prs(pr_numbers: List[int]) -> List[int]:
    """Drop PRs that are no longer OPEN/non-draft via a targeted gh pr view.

    `gh pr list` results can lag GitHub's authoritative state by tens of
    seconds, so a PR that was merged moments before the supervisor started
    can still appear in the initial list. Spawning a child for it just
    wastes a process slot (the child errors out with "PR <N> is not open")
    and produces noisy logs. A per-PR `gh pr view` is more authoritative.

    Network/transient failures from `_pr_is_still_open` are surfaced as
    "keep this PR" — we only drop PRs that GitHub definitively reports as
    not-OPEN. This matches the behaviour callers expect: do not silently
    swallow stale PRs because of a flaky network.
    """
    # ⚡ Bolt Optimization:
    # What: Concurrent execution of `_pr_is_still_open` using ThreadPoolExecutor.
    # Why: Avoid N+1 sequential subprocess execution overhead of `gh pr view`.
    # Impact: Significantly reduces latency when checking states for multiple PRs.
    kept: List[int] = []

    def _check_pr(pr: int) -> Tuple[int, bool, Optional[CommandError]]:
        try:
            return (pr, _pr_is_still_open(pr), None)
        except CommandError as exc:
            return (pr, True, exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = executor.map(_check_pr, pr_numbers)

    for pr, still_open, exc in results:
        if exc is not None:
            _print_step(
                "Could not confirm PR #{} open state ({}); keeping it in the "
                "fan-out set.".format(pr, exc)
            )
            kept.append(pr)
        elif still_open:
            kept.append(pr)
        else:
            _print_step(
                "PR #{} is no longer open (per gh pr view); skipping "
                "fan-out spawn.".format(pr)
            )
    return kept


def _cleanup_source_origin() -> str:
    result = _run_command(
        ["git", "remote", "get-url", "origin"],
        check=False,
        capture_output=True,
        replay_output=False,
    )
    if result.returncode != 0:
        _print_step(
            "Stale-state cleanup: could not determine launching repo origin; "
            "worktree directory cleanup will skip entries whose origin cannot "
            "be confirmed."
        )
        return ""
    return (result.stdout or "").strip()


def _fan_out_all_prs(
    args: argparse.Namespace, argv: List[str], script_path: str
) -> int:
    pr_numbers = _list_open_prs(args.base)
    if pr_numbers:
        pr_numbers = _filter_to_still_open_prs(pr_numbers)
    try:
        _cleanup_stale_loop_state(
            args.worktree_root,
            set(pr_numbers),
            source_origin_lookup=_cleanup_source_origin,
        )
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        _print_step(
            "Stale-state cleanup failed before fan-out (continuing): {}".format(exc)
        )
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
    env_failure_backoff = max(
        respawn_backoff,
        getattr(args, "fan_out_env_failure_backoff_seconds", 300),
    )
    _print_step(
        "Fan-out supervisor: {} open PR(s): {} (logs in {}; stuck timeout {}s; "
        "respawn backoff {}s; env-failure backoff {}s)".format(
            len(pr_numbers),
            ", ".join("#" + str(n) for n in pr_numbers),
            log_root,
            stuck_timeout,
            respawn_backoff,
            env_failure_backoff,
        )
    )
    base_child_args = _passthrough_args(argv)
    children: Dict[int, Tuple[subprocess.Popen, str, Any, float]] = {}
    last_exit_at: Dict[int, float] = {}
    pending_backoff: Dict[int, float] = {}
    shutting_down = {"flag": False}
    reload_requested = {"flag": False}
    shutdown_event = threading.Event()

    def _request_shutdown(signum, _frame):
        shutting_down["flag"] = True
        shutdown_event.set()
        sys.stderr.write(
            "\nSupervisor received {}; terminating children...\n".format(
                signal.Signals(signum).name
            )
        )
        sys.stderr.flush()

    def _request_reload(_signum, _frame):
        reload_requested["flag"] = True
        shutting_down["flag"] = True
        shutdown_event.set()

    previous_int = signal.signal(signal.SIGINT, _request_shutdown)
    previous_term = signal.signal(signal.SIGTERM, _request_shutdown)
    previous_hup = None
    if hasattr(signal, "SIGHUP"):
        previous_hup = signal.signal(signal.SIGHUP, _request_reload)
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
            if _supervisor_wait(shutdown_event, min(10.0, float(stuck_timeout))):
                break
            now = time.monotonic()
            for pr in list(children.keys()):
                proc, log_path, log_handle, spawned_at = children[pr]
                rc = proc.poll()
                if rc is not None:
                    try:
                        log_handle.flush()
                    except (OSError, ValueError):
                        pass
                    lifetime = now - spawned_at
                    if rc == CODEX_ENV_FAILURE_EXIT_CODE:
                        backoff_for_pr = env_failure_backoff
                        reason = "codex env failure"
                    elif rc == REBASE_CONFLICT_EXIT_CODE:
                        backoff_for_pr = env_failure_backoff
                        reason = "rebase conflict"
                    elif rc == LOOP_ALREADY_RUNNING_EXIT_CODE:
                        backoff_for_pr = env_failure_backoff
                        reason = "another ralph loop already owns this PR"
                    elif rc != 0 and lifetime < 60.0:
                        prior = pending_backoff.get(pr, respawn_backoff)
                        backoff_for_pr = min(env_failure_backoff, max(prior * 2, 30))
                        reason = (
                            "short-lived failure (lifetime={:.1f}s); escalating "
                            "backoff to {}s".format(lifetime, backoff_for_pr)
                        )
                    else:
                        backoff_for_pr = respawn_backoff
                        reason = "ordinary exit"
                    _print_step(
                        "PR #{} loop exited with code {} ({}); respawning after "
                        "{}s (log: {})".format(
                            pr, rc, reason, backoff_for_pr, log_path
                        )
                    )
                    try:
                        with open(log_path, "ab", buffering=0) as marker:
                            marker.write(
                                "\n=== exit rc={} reason={} backoff={}s ===\n".format(
                                    rc, reason, backoff_for_pr
                                ).encode("utf-8", errors="replace")
                            )
                    except OSError:
                        pass
                    try:
                        log_handle.close()
                    except OSError:
                        pass
                    last_exit_at[pr] = now
                    pending_backoff[pr] = backoff_for_pr
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
                    pending_backoff[pr] = respawn_backoff
                    del children[pr]
            if shutting_down["flag"]:
                break
            for pr in list(last_exit_at.keys()):
                if pr in children:
                    continue
                backoff_for_pr = pending_backoff.get(pr, respawn_backoff)
                if now - last_exit_at[pr] < backoff_for_pr:
                    continue
                # Use a per-PR `gh pr view` only once the child is actually
                # eligible for respawn. Checking on every supervisor sweep
                # produces noisy repeated `gh pr view` bursts while the PR is
                # still cooling down under backoff.
                try:
                    still_open = _pr_is_still_open(pr)
                except CommandError as exc:
                    _print_step(
                        "Could not confirm PR #{} open state for respawn "
                        "({}); keeping it in the respawn set.".format(pr, exc)
                    )
                    still_open = True
                if not still_open:
                    _print_step(
                        "PR #{} is no longer open; dropping from respawn "
                        "set.".format(pr)
                    )
                    last_exit_at.pop(pr, None)
                    pending_backoff.pop(pr, None)
                    continue
                proc, log_path, log_handle = _spawn_child(
                    pr=pr,
                    script_path=script_path,
                    base_child_args=base_child_args,
                    log_root=log_root,
                )
                children[pr] = (proc, log_path, log_handle, time.monotonic())
                last_exit_at.pop(pr, None)
                # Intentionally keep pending_backoff[pr] across the respawn:
                # the short-lived-failure ramp (30s -> 60s -> 120s -> ...) reads
                # the previous value as ``prior`` and only escalates if it
                # survives the respawn. The natural resets are the
                # ordinary-exit and stuck-timeout branches above, which both
                # rewrite pending_backoff to ``respawn_backoff``.
                _print_step(
                    "Respawned PR #{} pid={} (log: {})".format(
                        pr, proc.pid, log_path
                    )
                )
    finally:
        if reload_requested["flag"]:
            _print_step(
                "Reload requested via SIGHUP; re-exec'ing supervisor"
            )
            for pr, (proc, _log_path, log_handle, _spawned_at) in list(
                children.items()
            ):
                try:
                    proc.terminate()
                except OSError:
                    pass
                try:
                    log_handle.close()
                except OSError:
                    pass
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
            if previous_hup is not None and hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP, previous_hup)
            sys.stdout.flush()
            sys.stderr.flush()
            os.execv(
                sys.executable, [sys.executable, script_path] + sys.argv[1:]
            )
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
        if previous_hup is not None and hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, previous_hup)
    _print_step("Fan-out supervisor exited cleanly.")
    return 0


def _resolve_target_directories(
    raw_dirs: List[str], recursive: bool
) -> List[str]:
    """Expand the user-supplied directory args into concrete repo paths.

    - If ``recursive`` is set, each input path is treated as a parent and we
      scan its immediate subdirectories for git repos (presence of ``.git``).
    - Each result is realpath'd and deduplicated while preserving order.
    - Raises CommandError if any input is not a directory.
    """
    resolved: List[str] = []
    seen: set = set()

    def _push(path: str) -> None:
        real = os.path.realpath(path)
        if real in seen:
            return
        seen.add(real)
        resolved.append(real)

    for raw in raw_dirs:
        path = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(path):
            raise CommandError(
                "Target directory does not exist or is not a directory: {}".format(
                    raw
                )
            )
        if not recursive:
            _push(path)
            continue
        for name in sorted(os.listdir(path)):
            child = os.path.join(path, name)
            if not os.path.isdir(child):
                continue
            if os.path.isdir(os.path.join(child, ".git")) or os.path.isfile(
                os.path.join(child, ".git")
            ):
                _push(child)
    if recursive and not resolved:
        raise CommandError(
            "No git repositories found under any of the recursive parents."
        )
    return resolved


def _fan_out_across_directories(
    args: argparse.Namespace,
    argv: List[str],
    script_path: str,
    target_dirs: List[str],
    original_cwd: str,
) -> int:
    """Spawn one ralph supervisor per target directory in parallel."""
    if args.fan_out_log_dir:
        log_root = os.path.abspath(os.path.expanduser(args.fan_out_log_dir))
    else:
        log_root = os.path.join(
            os.path.dirname(script_path), ".ralph-logs", "multi-repo"
        )
    os.makedirs(log_root, exist_ok=True)
    _print_step(
        "Multi-directory fan-out: launching {} supervisor(s); logs in {}".format(
            len(target_dirs), log_root
        )
    )
    base_args: List[str] = []
    skip_next = False
    for token in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if token == "--recursive":
            continue
        if token in target_dirs:
            continue
        base_args.append(token)
    procs: List[Tuple[str, subprocess.Popen, str, Any]] = []
    for target_dir in target_dirs:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(target_dir.rstrip("/"))) or "repo"
        log_path = os.path.join(log_root, "{}.log".format(slug))
        log_handle = open(log_path, "ab", buffering=0)
        cmd = [sys.executable, script_path] + base_args + [target_dir]
        log_handle.write(
            "\n=== spawn {} ===\n$ {}\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S"), shlex.join(cmd)
            ).encode("utf-8", errors="replace")
        )
        _print_step(
            "Launched repo supervisor for {} (log: {})".format(target_dir, log_path)
        )
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        procs.append((target_dir, proc, log_path, log_handle))
    shutdown_event = threading.Event()

    def _request_shutdown(_signum, _frame):
        shutdown_event.set()

    prev_int = signal.signal(signal.SIGINT, _request_shutdown)
    prev_term = signal.signal(signal.SIGTERM, _request_shutdown)
    try:
        while procs:
            if shutdown_event.wait(timeout=5):
                break
            still_running = []
            for target_dir, proc, log_path, log_handle in procs:
                rc = proc.poll()
                if rc is None:
                    still_running.append((target_dir, proc, log_path, log_handle))
                    continue
                try:
                    log_handle.close()
                except OSError:
                    pass
                _print_step(
                    "Repo supervisor for {} exited with code {} (log: {})".format(
                        target_dir, rc, log_path
                    )
                )
            procs = still_running
    finally:
        for target_dir, proc, _log_path, log_handle in procs:
            try:
                proc.terminate()
            except OSError:
                pass
        for target_dir, proc, _log_path, log_handle in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                log_handle.close()
            except OSError:
                pass
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        try:
            os.chdir(original_cwd)
        except OSError:
            pass
    _print_step("Multi-directory fan-out exited.")
    return 0


def main() -> int:
    original_cwd = os.getcwd()
    script_path = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    args = _parse_args()
    previous_json_log = _configure_json_log(args.json_log)
    raw_dirs = args.directory or []
    target_dirs = (
        _resolve_target_directories(raw_dirs, args.recursive)
        if (raw_dirs or args.recursive)
        else []
    )
    if len(target_dirs) > 1:
        if not script_path or not os.path.isfile(script_path):
            raise CommandError(
                "Cannot resolve ralph script path for multi-directory fan-out: {!r}".format(
                    sys.argv[0] if sys.argv else ""
                )
            )
        try:
            return _fan_out_across_directories(
                args, sys.argv, script_path, target_dirs, original_cwd
            )
        finally:
            _configure_json_log(previous_json_log)
    if target_dirs:
        target_dir = target_dirs[0]
        if not os.path.isdir(target_dir):
            raise CommandError(
                "Target directory does not exist or is not a directory: {}".format(
                    target_dir
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
            _configure_json_log(previous_json_log)
            try:
                os.chdir(original_cwd)
            except OSError:
                pass
    start_time = time.monotonic()
    telemetry = _RunTelemetry(started_at=start_time)
    deadline: Optional[float] = (
        start_time + args.max_wall_clock_seconds
        if args.max_wall_clock_seconds > 0
        else None
    )
    previous_command_deadline = _set_command_deadline(deadline)

    _shutdown_signal = {"name": None}

    def _handle_shutdown(signum, _frame):
        _shutdown_signal["name"] = signal.Signals(signum).name
        _write_stderr_fd(
            "\nReceived {}; cleaning up...\n".format(_shutdown_signal["name"])
        )
        raise SystemExit(130 if signum == signal.SIGINT else 143)

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    pr_loop_lock = None
    try:
        pr_data, pr_number = _resolve_pr_data(args)
        branch = _validate_pr_metadata(pr_data, pr_number, args.base)
        if args.dry_run:
            _print_step(
                "Dry run: validated PR #{} {} on branch '{}' targeting base '{}'.".format(
                    pr_number, pr_data.get("url", ""), branch, args.base
                ),
                event="dry_run.validated_pr",
                pr=pr_number,
                url=pr_data.get("url", ""),
                branch=branch,
                base=args.base,
            )
            _print_dry_run_simulation(args, pr_number, branch)
            _print_step(
                "Dry run: stopped before identity changes, worktree setup, Codex, "
                "quality gates, rebase, push, reset, approval, merge, or branch deletion.",
                event="dry_run.stopped_before_mutation",
                pr=pr_number,
                mutates=False,
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

        with telemetry.phase("review"):
            review_passed = False
            last_review_round = 0
            round_number = 0
            while True:
                round_number += 1
                if args.max_review_rounds > 0 and round_number > args.max_review_rounds:
                    break
                telemetry.review_rounds += 1
                _check_wall_clock(deadline)
                last_review_round = round_number
                pre_round_sha = _git_head_sha()
                try:
                    external_comments = _pr_review_comments(pr_target)
                except CommandError as exc:
                    _print_step(
                        "Could not fetch existing PR review comments ({}); "
                        "proceeding without them.".format(exc)
                    )
                    external_comments = []
                if external_comments:
                    _print_step(
                        "Surfacing {} existing PR review comment(s) to Codex.".format(
                            len(external_comments)
                        )
                    )
                review_passed, addressed = _run_review_fix_round(
                    round_number,
                    args.base,
                    args.model,
                    external_comments=external_comments,
                )
                if not review_passed:
                    local_telemetry = LocalQualityTelemetry()
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
                        telemetry=local_telemetry,
                    )
                    telemetry.record_local_quality(local_telemetry)
                    if commit_state == "discarded":
                        _print_step(
                            "Review round {} changes were not useful; retrying with a fresh context window and Codex session.".format(
                                round_number
                            )
                        )
                        continue
                    if commit_state == "no_changes":
                        _reset_generated_changes(pre_round_sha)
                    else:
                        _post_addressed_comment_replies(
                            pr_target, addressed, round_number
                        )
                    _print_step(
                        "Review round {} still has actionable findings; retrying with a fresh context window and Codex session.".format(
                            round_number
                        )
                    )
                    continue
                local_telemetry = LocalQualityTelemetry()
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
                    telemetry=local_telemetry,
                )
                telemetry.record_local_quality(local_telemetry)
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
                else:
                    _post_addressed_comment_replies(pr_target, addressed, round_number)
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

        with telemetry.phase("ci"):
            ci_green = False
            last_ci_round = 0
            round_number = 0
            while True:
                round_number += 1
                if args.max_ci_rounds > 0 and round_number > args.max_ci_rounds:
                    break
                _check_wall_clock(deadline)
                last_ci_round = round_number
                telemetry.ci_waits += 1
                ci_green, checks = _wait_for_required_checks_green(
                    branch=branch,
                    pr_number=pr_number,
                    poll_seconds=args.poll_seconds,
                    timeout_seconds=args.checks_timeout_seconds,
                    deadline=deadline,
                )
                if ci_green:
                    break
                pre_round_sha = _git_head_sha()
                telemetry.ci_repair_rounds += 1
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
                local_telemetry = LocalQualityTelemetry()
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
                    telemetry=local_telemetry,
                )
                telemetry.record_local_quality(local_telemetry)
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
                telemetry.ci_waits += 1
                ci_green, checks = _wait_for_required_checks_green(
                    branch=branch,
                    pr_number=pr_number,
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
                pr_number=pr_number,
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
        telemetry.emit()
        _print_step("Done.", event="run.done")
        return 0
    except CodexEnvironmentError as exc:
        sys.stderr.write(
            "ERROR: codex environmental failure: {}\n"
            "Exiting with code {} so the fan-out supervisor applies a long "
            "backoff before respawning this PR's loop.\n".format(
                exc, CODEX_ENV_FAILURE_EXIT_CODE
            )
        )
        sys.stderr.flush()
        return CODEX_ENV_FAILURE_EXIT_CODE
    except RebaseConflictError as exc:
        sys.stderr.write(
            "ERROR: rebase conflict: {}\n"
            "Exiting with code {} so the fan-out supervisor applies a long "
            "backoff before respawning this PR's loop.\n".format(
                exc, REBASE_CONFLICT_EXIT_CODE
            )
        )
        sys.stderr.flush()
        return REBASE_CONFLICT_EXIT_CODE
    finally:
        _set_command_deadline(previous_command_deadline)
        _configure_json_log(previous_json_log)
        if pr_loop_lock is not None:
            pr_loop_lock.release()
        try:
            os.chdir(original_cwd)
        except OSError:
            pass
