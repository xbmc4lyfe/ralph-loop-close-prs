import argparse
import json
import os
import runpy
import signal
import subprocess
import sys

import pytest

from ralph_loop import cli, runtime
from ralph_loop.errors import (
    CODEX_ENV_FAILURE_EXIT_CODE,
    CodexEnvironmentError,
    CommandError,
)


def test_parse_args_defaults_and_explicit_options(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ralph"])

    args = cli._parse_args()

    assert args.pr is None
    assert args.base == "main"
    assert args.max_review_rounds == 0
    assert args.skip_merge is False
    assert args.dry_run is False

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ralph",
            "--pr",
            "12",
            "--base",
            "release",
            "--max-review-rounds",
            "2",
            "--max-ci-rounds",
            "3",
            "--max-local-quality-rounds",
            "4",
            "--poll-seconds",
            "5",
            "--checks-timeout-seconds",
            "6",
            "--model",
            "gpt-test",
            "--skip-rebase",
            "--skip-merge",
            "--dry-run",
            "--worktree-root",
            "/tmp/root",
            "--max-wall-clock-seconds",
            "7",
            "--json-log",
            "/tmp/ralph.jsonl",
        ],
    )

    args = cli._parse_args()

    assert args.pr == 12
    assert args.base == "release"
    assert args.max_review_rounds == 2
    assert args.max_ci_rounds == 3
    assert args.max_local_quality_rounds == 4
    assert args.poll_seconds == 5
    assert args.checks_timeout_seconds == 6
    assert args.model == "gpt-test"
    assert args.skip_rebase is True
    assert args.skip_merge is True
    assert args.dry_run is True
    assert args.worktree_root == "/tmp/root"
    assert args.max_wall_clock_seconds == 7
    assert args.json_log == "/tmp/ralph.jsonl"


@pytest.mark.parametrize(
    "argv",
    [
        ["ralph", "--poll-seconds", "0"],
        ["ralph", "--max-review-rounds", "-1"],
        ["ralph", "--pr", "0"],
    ],
)
def test_parse_args_rejects_invalid_integer_options(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit):
        cli._parse_args()


def test_integer_parsers_reject_wrong_signs():
    assert cli._nonneg_int("0") == 0
    assert cli._pos_int("1") == 1

    with pytest.raises(argparse.ArgumentTypeError):
        cli._nonneg_int("-1")
    with pytest.raises(argparse.ArgumentTypeError):
        cli._pos_int("0")


def test_main_happy_path_skip_rebase_and_merge_restores_cwd_and_releases_lock(
    cli_harness,
):
    original_cwd = os.getcwd()
    harness = cli_harness()

    assert cli.main() == 0

    assert os.getcwd() == original_cwd
    harness.lock.release.assert_called_once_with()
    harness.rebase.assert_not_called()
    harness.prepare_merge.assert_not_called()
    harness.merge_pr.assert_not_called()
    harness.mark_review.assert_called_once_with("7")
    harness.wait_checks.assert_called_once()


def test_main_installs_and_restores_global_command_deadline(
    monkeypatch, spy, cli_harness, cli_args
):
    args = cli_args(max_wall_clock_seconds=30)
    set_deadline = spy(side_effect=["previous", None])
    monkeypatch.setattr(cli.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(cli, "_set_command_deadline", set_deadline)
    harness = cli_harness(args=args)

    assert cli.main() == 0

    assert [call.args[0] for call in set_deadline.call_args_list] == [130.0, "previous"]
    assert harness.commit_push.call_args.kwargs["deadline"] == 130.0


def test_main_passes_pr_number_to_check_waits(cli_harness):
    harness = cli_harness()

    assert cli.main() == 0

    harness.wait_checks.assert_called_once()
    assert harness.wait_checks.call_args.kwargs["branch"] == "feature"
    assert harness.wait_checks.call_args.kwargs["pr_number"] == 7


def test_main_emits_phase_and_round_telemetry(monkeypatch, cli_harness, capsys):
    harness = cli_harness()
    harness.commit_push.return_value = "committed"
    harness.wait_checks.return_value = (True, [{"name": "unit", "bucket": "pass"}])
    tick_values = iter(
        [
            10.0,  # main start
            11.0,  # review phase start
            12.5,  # review phase finish
            13.0,  # ci phase start
            15.25,  # ci phase finish
            16.0,  # total finish
        ]
    )
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(tick_values))

    assert cli.main() == 0

    stderr = capsys.readouterr().err
    assert "Telemetry review_rounds=1" in stderr
    assert "ci_waits=1" in stderr
    assert "ci_repair_rounds=0" in stderr
    assert "review_seconds=1.50" in stderr
    assert "ci_seconds=2.25" in stderr
    assert "total_seconds=6.00" in stderr


def test_main_does_not_use_round_numbers_helper(monkeypatch, cli_harness):
    cli_harness()

    def fail_round_numbers(_max_rounds):
        raise AssertionError("_round_numbers should not be used by main")

    monkeypatch.setattr(cli, "_round_numbers", fail_round_numbers, raising=False)
    monkeypatch.setattr(runtime, "_round_numbers", fail_round_numbers, raising=False)

    assert cli.main() == 0


def test_main_happy_path_with_rebase_and_merge(cli_harness, cli_args):
    harness = cli_harness(args=cli_args(skip_rebase=False, skip_merge=False))

    assert cli.main() == 0

    assert harness.rebase.call_count == 2
    assert harness.pr_view.call_count == 2
    harness.prepare_merge.assert_called_once_with("7")
    harness.merge_pr.assert_called_once_with("7")
    assert harness.wait_checks.call_count == 2


def test_main_uses_current_branch_when_pr_arg_is_missing(cli_harness, cli_args):
    harness = cli_harness(args=cli_args(pr=None))

    cli.main()

    harness.pr_view.assert_called_once_with("current")


def test_main_explicit_pr_does_not_require_current_branch(cli_harness, cli_args):
    harness = cli_harness(args=cli_args(pr=12))
    harness.git_branch.side_effect = CommandError("detached")

    cli.main()

    harness.git_branch.assert_not_called()
    harness.pr_view.assert_called_once_with("12")


def test_main_rejects_numeric_current_branch_without_explicit_pr(
    cli_harness, cli_args
):
    harness = cli_harness(args=cli_args(pr=None))
    harness.git_branch.return_value = "123"

    with pytest.raises(CommandError, match="numeric branch name"):
        cli.main()

    harness.pr_view.assert_not_called()


def test_main_does_not_mutate_identity_when_pr_lookup_fails(cli_harness, cli_args):
    harness = cli_harness(args=cli_args(pr=12))
    harness.pr_view.side_effect = CommandError("permission denied")

    with pytest.raises(CommandError, match="permission denied"):
        cli.main()

    harness.ensure_identity.assert_not_called()
    harness.acquire_lock.assert_not_called()
    harness.ensure_worktree.assert_not_called()


def test_main_rejects_fork_pr_before_mutating_state(cli_harness, cli_args):
    harness = cli_harness(
        args=cli_args(pr=94),
        pr_data={
            "number": 94,
            "url": "https://example.test/pr/94",
            "state": "OPEN",
            "isDraft": False,
            "isCrossRepository": True,
            "baseRefName": "main",
            "headRefName": "feat/full-test-stack",
        },
    )

    with pytest.raises(CommandError, match="fork"):
        cli.main()

    harness.ensure_identity.assert_not_called()
    harness.acquire_lock.assert_not_called()
    harness.ensure_worktree.assert_not_called()
    harness.mark_review.assert_not_called()
    harness.rebase.assert_not_called()


def test_main_dry_run_validates_pr_without_mutating_local_or_remote_state(
    cli_harness, cli_args, capsys
):
    args = cli_args(dry_run=True, skip_rebase=False, skip_merge=False)
    harness = cli_harness(args=args)

    assert cli.main() == 0

    stderr = capsys.readouterr().err
    assert "Dry run: validated PR #7" in stderr
    assert "Dry run simulation plan:" in stderr
    assert "would acquire per-PR lock for PR #7" in stderr
    assert "would approve and merge PR #7" in stderr
    assert "stopped before identity changes" in stderr
    harness.git_branch.assert_not_called()
    harness.pr_view.assert_called_once_with("7")
    harness.ensure_identity.assert_not_called()
    harness.acquire_lock.assert_not_called()
    harness.ensure_worktree.assert_not_called()
    harness.validate_identity.assert_not_called()
    harness.working_dirty.assert_not_called()
    harness.mark_review.assert_not_called()
    harness.rebase.assert_not_called()
    harness.review_round.assert_not_called()
    harness.commit_push.assert_not_called()
    harness.wait_checks.assert_not_called()
    harness.ci_fix.assert_not_called()
    harness.reset_changes.assert_not_called()
    harness.prepare_merge.assert_not_called()
    harness.merge_pr.assert_not_called()
    harness.lock.release.assert_not_called()


def test_main_writes_json_log_for_dry_run(cli_harness, cli_args, tmp_path):
    log_path = tmp_path / "ralph.jsonl"
    args = cli_args(
        dry_run=True,
        skip_rebase=False,
        skip_merge=False,
        json_log=str(log_path),
    )
    cli_harness(args=args)

    assert cli.main() == 0

    events = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == [
        "dry_run.validated_pr",
        "dry_run.simulation_start",
        "dry_run.simulation_step",
        "dry_run.simulation_step",
        "dry_run.simulation_step",
        "dry_run.simulation_step",
        "dry_run.simulation_step",
        "dry_run.simulation_step",
        "dry_run.simulation_step",
        "dry_run.simulation_step",
        "dry_run.simulation_step",
        "dry_run.stopped_before_mutation",
    ]
    assert events[0]["pr"] == 7
    assert events[0]["branch"] == "feature"
    assert events[-1]["mutates"] is False


def test_main_merge_path_emits_json_telemetry(cli_harness, cli_args, tmp_path):
    log_path = tmp_path / "ralph.jsonl"
    harness = cli_harness(
        args=cli_args(skip_rebase=True, skip_merge=False, json_log=str(log_path))
    )

    assert cli.main() == 0

    harness.prepare_merge.assert_called_once_with("7")
    harness.merge_pr.assert_called_once_with("7")
    events = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event"] == "run.telemetry" for event in events)
    assert events[-2]["event"] == "run.telemetry"
    assert events[-1]["event"] == "run.done"


def test_main_signal_handler_exits_with_shell_status(
    monkeypatch, cli_args, capfd
):
    captured = {}

    def capture_signal(signum, handler):
        captured[signum] = handler

    def interrupt():
        captured[signal.SIGINT](signal.SIGINT, None)

    monkeypatch.setattr(cli, "_parse_args", lambda: cli_args())
    monkeypatch.setattr(cli.signal, "signal", capture_signal)
    monkeypatch.setattr(cli, "_ensure_runtime_identity", interrupt)

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 130
    assert "Received SIGINT" in capfd.readouterr().err


def test_main_signal_handler_does_not_use_buffered_stderr(
    monkeypatch, cli_args, capfd
):
    captured = {}

    class ReentrantStderr:
        def write(self, _text):
            raise RuntimeError("reentrant call inside <_io.BufferedWriter name='<stderr>'>")

        def flush(self):
            raise RuntimeError("reentrant flush")

    def capture_signal(signum, handler):
        captured[signum] = handler

    def interrupt():
        original_stderr = cli.sys.stderr
        cli.sys.stderr = ReentrantStderr()
        try:
            captured[signal.SIGINT](signal.SIGINT, None)
        finally:
            cli.sys.stderr = original_stderr

    monkeypatch.setattr(cli, "_parse_args", lambda: cli_args())
    monkeypatch.setattr(cli.signal, "signal", capture_signal)
    monkeypatch.setattr(cli, "_ensure_runtime_identity", interrupt)

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 130
    assert "Received SIGINT" in capfd.readouterr().err


@pytest.mark.parametrize(
    ("pr_record", "message"),
    [
        ({"number": "7"}, "Could not resolve PR number"),
        ({"state": "CLOSED"}, "is not open"),
        ({"isDraft": True}, "draft state"),
        ({"baseRefName": "release"}, "targets base"),
        ({"headRefName": ""}, "Could not resolve PR head"),
        ({"headRefName": "main"}, "matches base"),
    ],
)
def test_main_rejects_invalid_pr_metadata_before_worktree_setup(
    pr_record, message, cli_harness, pr_data
):
    harness = cli_harness(pr_data=pr_data(**pr_record))

    with pytest.raises(CommandError, match=message):
        cli.main()

    harness.ensure_identity.assert_not_called()
    harness.ensure_worktree.assert_not_called()


def test_main_rejects_dirty_pr_worktree(cli_harness):
    harness = cli_harness()
    harness.working_dirty.return_value = True

    with pytest.raises(CommandError, match="Worktree is dirty"):
        cli.main()

    harness.lock.release.assert_called_once_with()


@pytest.mark.parametrize("commit_state", ["discarded", "no_changes"])
def test_main_review_loop_handles_discarded_and_no_changes_failures(
    commit_state, cli_harness
):
    harness = cli_harness()
    harness.review_round.return_value = (False, [])
    harness.commit_push.return_value = commit_state

    with pytest.raises(CommandError, match="Review loop exhausted"):
        cli.main()

    assert harness.reset_changes.called is (commit_state == "no_changes")


def test_main_review_pass_discarded_keeps_loop_failed(cli_harness):
    harness = cli_harness()
    harness.review_round.return_value = (True, [])
    harness.commit_push.return_value = "discarded"

    with pytest.raises(CommandError, match="Review loop exhausted"):
        cli.main()


@pytest.mark.parametrize(
    ("ready", "commit_state", "reset_expected"),
    [
        (False, "committed", True),
        (True, "discarded", False),
        (True, "no_changes", False),
    ],
)
def test_main_ci_loop_handles_not_ready_discarded_no_changes_and_exhaustion(
    ready, commit_state, reset_expected, cli_harness
):
    harness = cli_harness()
    harness.wait_checks.return_value = (False, [{"name": "unit", "bucket": "fail"}])
    harness.ci_fix.return_value = ready
    harness.commit_push.side_effect = ["no_changes", commit_state]

    with pytest.raises(CommandError, match="CI loop exhausted"):
        cli.main()

    assert harness.reset_changes.called is reset_expected


def test_main_ci_loop_can_commit_fix_and_wait_again(cli_harness, cli_args):
    harness = cli_harness(args=cli_args(max_ci_rounds=1))
    harness.wait_checks.side_effect = [
        (False, [{"name": "unit", "bucket": "fail"}]),
        (True, [{"name": "unit", "bucket": "pass"}]),
    ]
    harness.commit_push.side_effect = ["no_changes", "committed"]

    assert cli.main() == 0

    assert harness.ci_fix.call_count == 1


def test_main_revalidates_pr_metadata_before_merge(cli_harness, cli_args, pr_data):
    harness = cli_harness(args=cli_args(skip_rebase=True, skip_merge=False))
    harness.pr_view.side_effect = [
        pr_data(),
        pr_data(isDraft=True),
    ]

    with pytest.raises(CommandError, match="draft state"):
        cli.main()

    harness.prepare_merge.assert_not_called()
    harness.merge_pr.assert_not_called()


def test_main_merge_path_orders_revalidation_prepare_and_merge(
    cli_harness, cli_args, pr_data
):
    harness = cli_harness(args=cli_args(skip_rebase=True, skip_merge=False))
    call_order: List[str] = []
    harness.pr_view.side_effect = lambda _ref: call_order.append("pr_view") or pr_data()
    harness.prepare_merge.side_effect = lambda _ref: call_order.append("prepare")
    harness.merge_pr.side_effect = lambda _ref: call_order.append("merge")

    assert cli.main() == 0

    assert call_order == ["pr_view", "pr_view", "prepare", "merge"]


def test_main_final_rebase_rechecks_pr_checks_before_merge(
    cli_harness, cli_args
):
    harness = cli_harness(args=cli_args(skip_rebase=False, skip_merge=False))

    assert cli.main() == 0

    assert harness.rebase.call_count == 2
    assert harness.wait_checks.call_count == 2
    assert harness.wait_checks.call_args_list[-1].kwargs["pr_number"] == 7
    harness.prepare_merge.assert_called_once_with("7")
    harness.merge_pr.assert_called_once_with("7")


def test_main_final_rebase_requires_green_checks(cli_harness, cli_args):
    harness = cli_harness(args=cli_args(skip_rebase=False, skip_merge=True))
    harness.wait_checks.side_effect = [
        (True, [{"name": "unit", "bucket": "pass"}]),
        (False, [{"name": "unit", "bucket": "fail"}]),
    ]

    with pytest.raises(CommandError, match="failed after rebase"):
        cli.main()


def test_compatibility_script_exits_with_main_status(monkeypatch):
    path = os.path.join(os.getcwd(), "codex_ralph_wiggum_loop.py")
    monkeypatch.setattr(cli, "main", lambda: 0)

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(path, run_name="__main__")

    assert raised.value.code == 0


def test_parse_args_accepts_directory_positional_and_all_prs_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ralph", "--all-prs", "/tmp/some-dir"])
    args = cli._parse_args()
    assert args.all_prs is True
    assert args.directory == ["/tmp/some-dir"]
    assert args.recursive is False
    assert args.pr is None


def test_parse_args_defaults_directory_and_all_prs_off(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ralph"])
    args = cli._parse_args()
    assert args.directory == []
    assert args.recursive is False
    assert args.all_prs is False


def test_parse_args_rejects_combination_of_all_prs_and_pr(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ralph", "--all-prs", "--pr", "5"])
    with pytest.raises(SystemExit):
        cli._parse_args()
    assert "--all-prs cannot be combined with --pr" in capsys.readouterr().err


def test_parse_args_accepts_directory_after_pr_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ralph", "--pr", "12", "/tmp/some-dir"])
    args = cli._parse_args()
    assert args.pr == 12
    assert args.directory == ["/tmp/some-dir"]


def test_parse_args_accepts_multiple_directories_and_recursive(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["ralph", "--recursive", "/tmp/a", "/tmp/b", "/tmp/c"]
    )
    args = cli._parse_args()
    assert args.recursive is True
    assert args.directory == ["/tmp/a", "/tmp/b", "/tmp/c"]


def test_parse_args_rejects_pr_with_multiple_directories(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ralph", "--pr", "5", "/tmp/a", "/tmp/b"])
    with pytest.raises(SystemExit):
        cli._parse_args()
    assert "--pr cannot be combined with multiple directories" in capsys.readouterr().err


def test_main_chdirs_into_directory_argument_before_resolving_pr(
    cli_harness, cli_args, tmp_path
):
    target_dir = tmp_path / "target-repo"
    target_dir.mkdir()
    original_cwd = os.getcwd()
    harness = cli_harness(args=cli_args(directory=[str(target_dir)]))
    seen_cwds = []
    real_pr_view = harness.pr_view

    def record_cwd(*args, **kwargs):
        seen_cwds.append(os.getcwd())
        return real_pr_view.return_value

    harness.pr_view.side_effect = record_cwd

    assert cli.main() == 0
    assert seen_cwds == [os.path.realpath(str(target_dir))]
    assert os.getcwd() == original_cwd


def test_main_rejects_nonexistent_directory_argument(cli_harness, cli_args, tmp_path):
    missing = tmp_path / "does-not-exist"
    cli_harness(args=cli_args(directory=[str(missing)]))

    with pytest.raises(CommandError, match="Target directory does not exist"):
        cli.main()


def test_passthrough_args_strips_all_prs_flag():
    argv = [
        "/abs/path/script.py",
        "--base",
        "main",
        "--all-prs",
        "--max-review-rounds",
        "3",
        "/tmp/dir",
    ]
    out = cli._passthrough_args(argv)
    assert "--all-prs" not in out
    assert out == ["--base", "main", "--max-review-rounds", "3", "/tmp/dir"]


def test_passthrough_args_preserves_argv_when_no_all_prs():
    argv = ["script.py", "--pr", "7", "/tmp/dir"]
    assert cli._passthrough_args(argv) == ["--pr", "7", "/tmp/dir"]


def test_should_fan_out_implicitly_true_only_when_on_base_without_pr_or_flag(
    monkeypatch, cli_args
):
    monkeypatch.setattr(cli, "_git_branch", lambda: "main")
    assert cli._should_fan_out_implicitly(cli_args(pr=None, all_prs=False)) is True
    assert cli._should_fan_out_implicitly(cli_args(pr=5, all_prs=False)) is False
    assert cli._should_fan_out_implicitly(cli_args(pr=None, all_prs=True)) is False

    monkeypatch.setattr(cli, "_git_branch", lambda: "feature")
    assert cli._should_fan_out_implicitly(cli_args(pr=None, all_prs=False)) is False


class _FakeProc:
    """Fake subprocess.Popen-ish object for supervisor tests."""

    def __init__(self, pid=0, exit_after_polls=0, returncode=0):
        self.pid = pid
        self._exit_after = exit_after_polls
        self._polls = 0
        self.returncode = None
        self._final_code = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        self._polls += 1
        if self._polls >= self._exit_after:
            self.returncode = self._final_code
            return self._final_code
        return None

    def wait(self, timeout=None):
        self.returncode = self._final_code
        return self._final_code

    def terminate(self):
        self.terminated = True
        self.returncode = self._final_code

    def kill(self):
        self.killed = True
        self.returncode = self._final_code


def test_spawn_child_uses_devnull_stdin_and_writes_log_header(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(pid=42, exit_after_polls=1)

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    log_root = tmp_path / "logs"
    log_root.mkdir()

    proc, log_path, log_handle = cli._spawn_child(
        pr=77,
        script_path="/abs/script.py",
        base_child_args=["--base", "main"],
        log_root=str(log_root),
    )

    assert proc.pid == 42
    assert log_path == str(log_root / "pr-77.log")
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is subprocess.STDOUT
    assert captured["cmd"][-2:] == ["--pr", "77"]
    log_handle.close()
    assert "spawn" in (log_root / "pr-77.log").read_text()


def test_fan_out_all_prs_returns_zero_when_no_open_prs(
    monkeypatch, cli_args, tmp_path
):
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [])
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: pytest.fail("should not spawn")
    )

    rc = cli._fan_out_all_prs(
        cli_args(all_prs=True),
        ["script.py", "--all-prs"],
        str(tmp_path / "script.py"),
    )

    assert rc == 0


def test_fan_out_cleanup_is_scoped_to_launching_repo_origin(
    monkeypatch, cli_args, tmp_path
):
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [])
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: pytest.fail("spawn"))
    cleanup_calls = []

    def fake_cleanup(worktree_root, open_pr_numbers, **kwargs):
        cleanup_calls.append((worktree_root, open_pr_numbers, kwargs))

    def fake_run(cmd, **_kwargs):
        assert cmd == ["git", "remote", "get-url", "origin"]
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="git@github.com:owner/current.git\n",
            stderr="",
        )

    monkeypatch.setattr(cli, "_cleanup_stale_loop_state", fake_cleanup)
    monkeypatch.setattr(cli, "_run_command", fake_run)

    rc = cli._fan_out_all_prs(
        cli_args(all_prs=True, worktree_root="/tmp/shared-ralph-worktrees"),
        ["script.py", "--all-prs"],
        str(tmp_path / "script.py"),
    )

    assert rc == 0
    assert len(cleanup_calls) == 1
    worktree_root, open_pr_numbers, kwargs = cleanup_calls[0]
    assert worktree_root == "/tmp/shared-ralph-worktrees"
    assert open_pr_numbers == set()
    assert kwargs["source_origin_lookup"]() == "git@github.com:owner/current.git"


def test_fan_out_supervisor_respawns_exited_children_until_shutdown(
    monkeypatch, cli_args, tmp_path
):
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [50])
    monkeypatch.setattr(cli, "_pr_is_still_open", lambda _pr: True)
    procs_made = []

    def fake_popen(_cmd, **_kwargs):
        proc = _FakeProc(pid=100 + len(procs_made), exit_after_polls=1)
        procs_made.append(proc)
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    clock = {"t": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["t"])

    wait_calls = {"n": 0}

    def fake_wait(event, _timeout):
        clock["t"] += 60.0
        wait_calls["n"] += 1
        if wait_calls["n"] >= 3:
            event.set()
            return True
        return False

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)

    script_path = tmp_path / "script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    rc = cli._fan_out_all_prs(
        cli_args(
            all_prs=True,
            fan_out_log_dir=str(log_dir),
            fan_out_respawn_backoff_seconds=1,
            fan_out_stuck_timeout_seconds=60,
        ),
        ["script.py", "--all-prs"],
        str(script_path),
    )

    assert rc == 0
    assert len(procs_made) >= 2, "supervisor should respawn at least once"
    assert (log_dir / "pr-50.log").exists()


def test_fan_out_supervisor_escalates_backoff_across_consecutive_short_lived_failures(
    monkeypatch, cli_args, tmp_path, capfd
):
    """Repeated short-lived child failures must ramp the respawn backoff
    (30s -> 60s -> 120s -> 240s -> 300s cap) instead of plateauing at 30s.

    Regression test: previously ``pending_backoff[pr]`` was popped on respawn,
    so the next short-lived exit read ``prior = respawn_backoff`` (5s) and
    computed ``min(env_failure_backoff, max(prior*2, 30)) = 30`` every cycle.
    A PR with a permanent failure (e.g. unresolved merge conflict) cycled
    every ~40s forever instead of slowing down.
    """
    import re

    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [50])
    monkeypatch.setattr(cli, "_pr_is_still_open", lambda _pr: True)

    procs_made = []

    def fake_popen(_cmd, **_kwargs):
        # Child always exits non-zero on its first poll.
        proc = _FakeProc(
            pid=100 + len(procs_made), exit_after_polls=1, returncode=1
        )
        procs_made.append(proc)
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    clock = {"t": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["t"])

    wait_calls = {"n": 0}

    def fake_wait(event, _timeout):
        # Advance 30s per sweep: keeps child lifetime well under the 60s
        # "short-lived" threshold and lets cooldowns expire over a handful of
        # sweeps. Bail after enough iterations to observe several escalations.
        clock["t"] += 30.0
        wait_calls["n"] += 1
        if wait_calls["n"] >= 60:
            event.set()
            return True
        return False

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)

    script_path = tmp_path / "script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    rc = cli._fan_out_all_prs(
        cli_args(
            all_prs=True,
            fan_out_log_dir=str(log_dir),
            fan_out_respawn_backoff_seconds=5,
            fan_out_env_failure_backoff_seconds=300,
            fan_out_stuck_timeout_seconds=900,
        ),
        ["script.py", "--all-prs"],
        str(script_path),
    )
    assert rc == 0

    err = capfd.readouterr().err
    backoffs = [int(m) for m in re.findall(r"escalating backoff to (\d+)s", err)]
    assert backoffs, "expected escalating-backoff messages in supervisor output"
    # First short-lived failure starts at 30s; each subsequent consecutive
    # short-lived failure should roughly double up to the env-failure cap.
    assert backoffs[0] == 30
    assert backoffs[1] >= 60, (
        "second consecutive short-lived failure should escalate beyond 30s; "
        "observed sequence: {}".format(backoffs)
    )
    # Sequence must be non-decreasing up to the cap.
    for prev, cur in zip(backoffs, backoffs[1:]):
        assert cur >= prev, "backoff regressed mid-sequence: {}".format(backoffs)
    # And it should eventually hit the env-failure cap given enough cycles.
    assert max(backoffs) >= 240, (
        "expected backoff to ramp toward the env-failure cap; saw {}".format(
            backoffs
        )
    )


def test_fan_out_supervisor_kills_idle_child_using_log_mtime_watchdog(
    monkeypatch, cli_args, tmp_path
):
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [33])
    monkeypatch.setattr(cli, "_pr_is_still_open", lambda _pr: True)
    procs_made = []

    def fake_popen(_cmd, **_kwargs):
        proc = _FakeProc(pid=200 + len(procs_made), exit_after_polls=99)
        procs_made.append(proc)
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli.os.path, "getmtime", lambda _path: 0.0)
    wait_calls = {"n": 0}

    def fake_wait(event, _timeout):
        wait_calls["n"] += 1
        if wait_calls["n"] >= 2:
            event.set()
            return True
        return False

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)

    script_path = tmp_path / "script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    rc = cli._fan_out_all_prs(
        cli_args(
            all_prs=True,
            fan_out_log_dir=str(log_dir),
            fan_out_stuck_timeout_seconds=60,
            fan_out_respawn_backoff_seconds=1,
        ),
        ["script.py", "--all-prs"],
        str(script_path),
    )

    assert rc == 0
    assert any(p.terminated or p.killed for p in procs_made), (
        "watchdog should have terminated the idle child"
    )


def test_main_with_all_prs_triggers_fan_out_and_skips_single_pr_path(
    cli_harness, cli_args, monkeypatch, tmp_path
):
    target = tmp_path / "repo"
    target.mkdir()
    script_path = tmp_path / "ralph_script.py"
    script_path.write_text("# stub\n")
    monkeypatch.setattr(
        sys, "argv", [str(script_path), "--all-prs", str(target)]
    )
    harness = cli_harness(args=cli_args(all_prs=True, directory=[str(target)]))
    called = {"count": 0}

    def fake_fan_out(args, argv, sp):
        called["count"] += 1
        return 0

    monkeypatch.setattr(cli, "_fan_out_all_prs", fake_fan_out)

    assert cli.main() == 0
    assert called["count"] == 1
    harness.pr_view.assert_not_called()
    harness.acquire_lock.assert_not_called()


def test_main_implicit_fan_out_when_on_base_branch_without_pr(
    cli_harness, cli_args, monkeypatch, tmp_path
):
    target = tmp_path / "repo"
    target.mkdir()
    script_path = tmp_path / "ralph_script.py"
    script_path.write_text("# stub\n")
    monkeypatch.setattr(sys, "argv", [str(script_path), str(target)])
    harness = cli_harness(
        args=cli_args(pr=None, all_prs=False, directory=[str(target)])
    )
    harness.git_branch.return_value = "main"
    called = {"count": 0}

    def fake_fan_out(args, argv, sp):
        called["count"] += 1
        return 0

    monkeypatch.setattr(cli, "_fan_out_all_prs", fake_fan_out)

    assert cli.main() == 0
    assert called["count"] == 1
    harness.pr_view.assert_not_called()


def test_main_no_implicit_fan_out_when_on_feature_branch(
    cli_harness, cli_args, monkeypatch
):
    harness = cli_harness(args=cli_args(pr=None, all_prs=False))
    harness.git_branch.return_value = "feature"
    monkeypatch.setattr(
        cli,
        "_fan_out_all_prs",
        lambda *a, **k: pytest.fail("should not fan out"),
    )

    assert cli.main() == 0
    harness.pr_view.assert_called_once_with("feature")


def test_fan_out_skips_initial_spawn_for_prs_that_are_no_longer_open(
    monkeypatch, cli_args, tmp_path
):
    """Stale PRs returned by `gh pr list` must be filtered before any spawn.

    Regression: pr-101.log was showing repeated "PR 101 is not open
    (state=MERGED)" errors because the initial list was used as-is, so the
    supervisor wasted a child spawn on each stale PR every fan-out cycle.
    """
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [50, 101, 77])

    def fake_state(pr):
        # 101 was merged between list and view.
        return pr != 101

    monkeypatch.setattr(cli, "_pr_is_still_open", fake_state)
    spawned: List[int] = []

    def fake_popen(cmd, **_kwargs):
        # The PR number is the last argument (`--pr <n>`); record it.
        spawned.append(int(cmd[-1]))
        return _FakeProc(pid=900 + len(spawned), exit_after_polls=99)

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    wait_calls = {"n": 0}

    def fake_wait(event, _timeout):
        wait_calls["n"] += 1
        if wait_calls["n"] >= 1:
            event.set()
            return True
        return False

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)
    monkeypatch.setattr(cli.os.path, "getmtime", lambda _path: 9e18)

    script_path = tmp_path / "script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    rc = cli._fan_out_all_prs(
        cli_args(
            all_prs=True,
            fan_out_log_dir=str(log_dir),
            fan_out_stuck_timeout_seconds=60,
            fan_out_respawn_backoff_seconds=1,
        ),
        ["script.py", "--all-prs"],
        str(script_path),
    )

    assert rc == 0
    # 101 was filtered out; only the two truly-open PRs got spawned.
    assert sorted(spawned) == [50, 77]


def test_fan_out_keeps_pr_in_initial_set_when_open_check_raises(
    monkeypatch, cli_args, tmp_path, capsys
):
    """Transient gh failures must not silently drop a PR from the set."""
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [50])

    def boom(_pr):
        raise CommandError("i/o timeout")

    monkeypatch.setattr(cli, "_pr_is_still_open", boom)
    spawned: List[int] = []

    def fake_popen(cmd, **_kwargs):
        spawned.append(int(cmd[-1]))
        return _FakeProc(pid=800 + len(spawned), exit_after_polls=99)

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli.os.path, "getmtime", lambda _path: 9e18)

    wait_calls = {"n": 0}

    def fake_wait(event, _timeout):
        wait_calls["n"] += 1
        if wait_calls["n"] >= 1:
            event.set()
            return True
        return False

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)

    script_path = tmp_path / "script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    rc = cli._fan_out_all_prs(
        cli_args(
            all_prs=True,
            fan_out_log_dir=str(log_dir),
            fan_out_stuck_timeout_seconds=60,
            fan_out_respawn_backoff_seconds=1,
        ),
        ["script.py", "--all-prs"],
        str(script_path),
    )

    assert rc == 0
    assert spawned == [50]
    stderr = capsys.readouterr().err
    assert "Could not confirm PR #50 open state" in stderr


def test_fan_out_returns_zero_when_all_initial_prs_are_filtered_out(
    monkeypatch, cli_args, tmp_path
):
    """If every initial-list PR turns out to be merged, exit cleanly."""
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [101, 102])
    monkeypatch.setattr(cli, "_pr_is_still_open", lambda _pr: False)
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("should not spawn after filter"),
    )

    rc = cli._fan_out_all_prs(
        cli_args(all_prs=True),
        ["script.py", "--all-prs"],
        str(tmp_path / "script.py"),
    )

    assert rc == 0


def test_fan_out_respawn_block_uses_pr_is_still_open_to_drop_merged_prs(
    monkeypatch, cli_args, tmp_path, capsys
):
    """Respawn path must consult `_pr_is_still_open`, not `_list_open_prs`.

    Regression: when a child exits because its PR was merged, the supervisor
    previously used `_list_open_prs` to refresh the respawn set, which can
    lag GitHub's authoritative state by tens of seconds and cause a
    just-merged PR to be respawned. A targeted view is authoritative.
    """
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [50])
    # Initial filter says PR is open; respawn-time check reports it merged.
    open_state_calls: List[int] = []

    def fake_state(pr):
        open_state_calls.append(pr)
        # First call (initial filter) -> True; subsequent (respawn check) -> False.
        return len(open_state_calls) == 1

    monkeypatch.setattr(cli, "_pr_is_still_open", fake_state)

    list_open_calls = {"n": 0}

    def fake_list(_base):
        list_open_calls["n"] += 1
        return [50]

    monkeypatch.setattr(cli, "_list_open_prs", fake_list)

    procs_made = []

    def fake_popen(_cmd, **_kwargs):
        proc = _FakeProc(pid=300 + len(procs_made), exit_after_polls=1)
        procs_made.append(proc)
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    clock = {"t": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["t"])

    wait_calls = {"n": 0}

    def fake_wait(event, _timeout):
        clock["t"] += 60.0
        wait_calls["n"] += 1
        if wait_calls["n"] >= 3:
            event.set()
            return True
        return False

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)

    script_path = tmp_path / "script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    rc = cli._fan_out_all_prs(
        cli_args(
            all_prs=True,
            fan_out_log_dir=str(log_dir),
            fan_out_respawn_backoff_seconds=1,
            fan_out_stuck_timeout_seconds=60,
        ),
        ["script.py", "--all-prs"],
        str(script_path),
    )

    assert rc == 0
    # The respawn block must not call `_list_open_prs`; it must rely on the
    # targeted view instead. Only the initial spawn discovery call counts.
    assert list_open_calls["n"] == 1
    # Exactly one spawn (the initial); the respawn was suppressed by the
    # targeted not-open check.
    assert len(procs_made) == 1
    stderr = capsys.readouterr().err
    assert "PR #50 is no longer open" in stderr


def test_fan_out_respawn_block_keeps_pr_when_state_check_raises(
    monkeypatch, cli_args, tmp_path, capsys
):
    """Transient failures in the respawn-time check must not drop the PR."""
    open_state_calls: List[int] = []

    def fake_state(pr):
        open_state_calls.append(pr)
        # Initial filter -> True; respawn check -> raise transient error.
        if len(open_state_calls) == 1:
            return True
        raise CommandError("i/o timeout")

    monkeypatch.setattr(cli, "_pr_is_still_open", fake_state)
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [50])

    procs_made = []

    def fake_popen(_cmd, **_kwargs):
        # First proc exits, second one runs forever (so we get exactly one
        # respawn attempt before shutdown).
        if not procs_made:
            proc = _FakeProc(pid=400, exit_after_polls=1)
        else:
            proc = _FakeProc(pid=401, exit_after_polls=99)
        procs_made.append(proc)
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli.os.path, "getmtime", lambda _path: 9e18)

    clock = {"t": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["t"])

    wait_calls = {"n": 0}

    def fake_wait(event, _timeout):
        clock["t"] += 60.0
        wait_calls["n"] += 1
        if wait_calls["n"] >= 3:
            event.set()
            return True
        return False

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)

    script_path = tmp_path / "script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    rc = cli._fan_out_all_prs(
        cli_args(
            all_prs=True,
            fan_out_log_dir=str(log_dir),
            fan_out_respawn_backoff_seconds=1,
            fan_out_stuck_timeout_seconds=60,
        ),
        ["script.py", "--all-prs"],
        str(script_path),
    )

    assert rc == 0
    # Initial spawn + at least one respawn (since the transient error kept
    # the PR in the respawn set).
    assert len(procs_made) >= 2
    stderr = capsys.readouterr().err
    assert "Could not confirm PR #50 open state for respawn" in stderr


def test_fan_out_respawn_checks_open_state_only_after_backoff_expires(
    monkeypatch, cli_args, tmp_path
):
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [50])
    procs_made = []
    clock = {"t": 0.0}
    open_state_times = []

    def fake_state(_pr):
        open_state_times.append(clock["t"])
        return True

    def fake_popen(_cmd, **_kwargs):
        if not procs_made:
            proc = _FakeProc(pid=450, exit_after_polls=1, returncode=0)
        else:
            proc = _FakeProc(pid=451, exit_after_polls=99, returncode=0)
        procs_made.append(proc)
        return proc

    monkeypatch.setattr(cli, "_pr_is_still_open", fake_state)
    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli.os.path, "getmtime", lambda _path: 9e18)
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["t"])

    wait_calls = {"n": 0}

    def fake_wait(event, _timeout):
        clock["t"] += 10.0
        wait_calls["n"] += 1
        if wait_calls["n"] >= 8:
            event.set()
            return True
        return False

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)

    script_path = tmp_path / "script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    rc = cli._fan_out_all_prs(
        cli_args(
            all_prs=True,
            fan_out_log_dir=str(log_dir),
            fan_out_respawn_backoff_seconds=60,
            fan_out_stuck_timeout_seconds=60,
        ),
        ["script.py", "--all-prs"],
        str(script_path),
    )

    assert rc == 0
    assert len(procs_made) == 2
    assert open_state_times == [0.0, 70.0]


def test_compatibility_script_prints_command_error(monkeypatch, capsys):
    path = os.path.join(os.getcwd(), "codex_ralph_wiggum_loop.py")

    def boom():
        raise CommandError("boom")

    monkeypatch.setattr(cli, "main", boom)

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(path, run_name="__main__")

    assert raised.value.code == 1
    assert "ERROR: boom" in capsys.readouterr().err


def test_main_returns_env_failure_exit_code_when_codex_env_error_in_review_loop(
    cli_harness, capsys
):
    harness = cli_harness()
    harness.review_round.side_effect = CodexEnvironmentError(
        "codex exec failed (exit=1) [env failure: 401 Unauthorized]"
    )

    rc = cli.main()

    assert rc == CODEX_ENV_FAILURE_EXIT_CODE
    stderr = capsys.readouterr().err
    assert "codex environmental failure" in stderr
    assert "long backoff" in stderr
    # Lock must still be released even on env-failure exit path.
    harness.lock.release.assert_called_once_with()


def test_main_returns_env_failure_exit_code_when_codex_env_error_in_ci_loop(
    cli_harness,
):
    harness = cli_harness()
    harness.wait_checks.return_value = (False, [{"name": "unit", "bucket": "fail"}])
    harness.ci_fix.side_effect = CodexEnvironmentError("transport gave up")

    rc = cli.main()

    assert rc == CODEX_ENV_FAILURE_EXIT_CODE


def test_compatibility_script_exits_with_env_failure_code(monkeypatch, capsys):
    path = os.path.join(os.getcwd(), "codex_ralph_wiggum_loop.py")

    def boom():
        raise CodexEnvironmentError("401 Unauthorized")

    monkeypatch.setattr(cli, "main", boom)

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(path, run_name="__main__")

    assert raised.value.code == CODEX_ENV_FAILURE_EXIT_CODE
    stderr = capsys.readouterr().err
    assert "codex environmental failure" in stderr
    assert "401" in stderr


def test_fan_out_supervisor_uses_long_backoff_after_env_failure_exit(
    monkeypatch, cli_args, tmp_path
):
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [99])
    monkeypatch.setattr(cli, "_pr_is_still_open", lambda _pr: True)
    procs_made = []

    def fake_popen(_cmd, **_kwargs):
        # First child exits with the env-failure code; the supervisor must NOT
        # respawn it after the short backoff.
        exit_code = CODEX_ENV_FAILURE_EXIT_CODE if not procs_made else 0
        proc = _FakeProc(
            pid=500 + len(procs_made),
            exit_after_polls=1,
            returncode=exit_code,
        )
        procs_made.append(proc)
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    clock = {"t": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["t"])

    wait_calls = {"n": 0}

    def fake_wait(event, _timeout):
        # Each poll cycle advances 30s of wall clock. With short backoff = 1s
        # and env-failure backoff = 600s, the child should NOT be respawned
        # within the first several poll cycles.
        clock["t"] += 30.0
        wait_calls["n"] += 1
        if wait_calls["n"] >= 4:
            event.set()
            return True
        return False

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)

    script_path = tmp_path / "script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    rc = cli._fan_out_all_prs(
        cli_args(
            all_prs=True,
            fan_out_log_dir=str(log_dir),
            fan_out_respawn_backoff_seconds=1,
            fan_out_stuck_timeout_seconds=60,
            fan_out_env_failure_backoff_seconds=600,
        ),
        ["script.py", "--all-prs"],
        str(script_path),
    )

    assert rc == 0
    # Initial spawn only; long backoff prevents respawn within the test window.
    assert len(procs_made) == 1, (
        "env-failure backoff should suppress respawn within 600s; got "
        "{} spawns at t={:.0f}s".format(len(procs_made), clock["t"])
    )


def test_fan_out_supervisor_respawns_normally_after_short_backoff_for_nonzero_exit(
    monkeypatch, cli_args, tmp_path
):
    # Non-env-failure exit code that ran for >60s (not short-lived) uses the
    # short respawn backoff and DOES respawn within the test window. This
    # guards against the long-backoff change accidentally applying to ordinary
    # failures.
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [88])
    monkeypatch.setattr(cli, "_pr_is_still_open", lambda _pr: True)
    procs_made = []

    def fake_popen(_cmd, **_kwargs):
        # First child exits with code 1 (ordinary failure); subsequent children
        # stay alive for the rest of the test.
        if not procs_made:
            proc = _FakeProc(pid=600, exit_after_polls=20, returncode=1)
        else:
            proc = _FakeProc(pid=601, exit_after_polls=99, returncode=0)
        procs_made.append(proc)
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    clock = {"t": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["t"])

    wait_calls = {"n": 0}

    def fake_wait(event, _timeout):
        clock["t"] += 15.0
        wait_calls["n"] += 1
        if wait_calls["n"] >= 30:
            event.set()
            return True
        return False

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)

    script_path = tmp_path / "script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    rc = cli._fan_out_all_prs(
        cli_args(
            all_prs=True,
            fan_out_log_dir=str(log_dir),
            fan_out_respawn_backoff_seconds=1,
            fan_out_stuck_timeout_seconds=60,
            fan_out_env_failure_backoff_seconds=600,
        ),
        ["script.py", "--all-prs"],
        str(script_path),
    )

    assert rc == 0
    assert len(procs_made) >= 2, "ordinary exit should respawn within short backoff"


def test_spawn_child_opens_log_handle_with_o_cloexec(monkeypatch, tmp_path):
    """Parent-side log fds must have FD_CLOEXEC so they don't leak across exec."""
    captured = {}

    real_os_open = os.open

    def recording_open(path, flags, mode=0o644):
        captured["path"] = path
        captured["flags"] = flags
        captured["mode"] = mode
        return real_os_open(path, flags, mode)

    monkeypatch.setattr(cli.os, "open", recording_open)
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: _FakeProc(pid=1, exit_after_polls=1)
    )

    log_root = tmp_path / "logs"
    log_root.mkdir()

    _, _, log_handle = cli._spawn_child(
        pr=99,
        script_path="/abs/script.py",
        base_child_args=[],
        log_root=str(log_root),
    )

    try:
        assert captured["flags"] & os.O_CLOEXEC, (
            "log handle must be opened with O_CLOEXEC to avoid leaking fds "
            "on supervisor re-exec; got flags={:#x}".format(captured["flags"])
        )
        assert captured["flags"] & os.O_APPEND
        assert captured["path"] == str(log_root / "pr-99.log")
    finally:
        log_handle.close()


def test_open_log_handle_cloexec_sets_close_on_exec_on_real_fd(tmp_path):
    """The returned handle's underlying fd must actually have FD_CLOEXEC set."""
    import fcntl

    log_path = tmp_path / "pr-1.log"
    handle = cli._open_log_handle_cloexec(str(log_path))
    try:
        flags = fcntl.fcntl(handle.fileno(), fcntl.F_GETFD)
        assert flags & fcntl.FD_CLOEXEC, (
            "expected FD_CLOEXEC bit on real log handle fd, got {:#x}".format(flags)
        )
    finally:
        handle.close()


def test_fan_out_installs_sighup_reload_handler(monkeypatch, cli_args, tmp_path):
    """SIGHUP handler must be installed alongside SIGINT/SIGTERM."""
    if not hasattr(signal, "SIGHUP"):
        pytest.skip("SIGHUP not available on this platform")

    install_log = []

    def capture_signal(signum, handler):
        install_log.append((signum, handler))
        return signal.SIG_DFL

    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [42])
    monkeypatch.setattr(cli, "_pr_is_still_open", lambda _pr: True)
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: _FakeProc(pid=1, exit_after_polls=1)
    )
    monkeypatch.setattr(cli.signal, "signal", capture_signal)

    def fake_wait(event, _timeout):
        event.set()
        return True

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)

    script_path = tmp_path / "script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    cli._fan_out_all_prs(
        cli_args(
            all_prs=True,
            fan_out_log_dir=str(log_dir),
            fan_out_respawn_backoff_seconds=1,
            fan_out_stuck_timeout_seconds=60,
        ),
        ["script.py", "--all-prs"],
        str(script_path),
    )

    callable_installs = {
        signum for signum, handler in install_log if callable(handler)
    }
    assert signal.SIGINT in callable_installs
    assert signal.SIGTERM in callable_installs
    assert signal.SIGHUP in callable_installs


def test_fan_out_sighup_triggers_execv_with_supervisor_argv(
    monkeypatch, cli_args, tmp_path
):
    """SIGHUP should re-exec the supervisor via os.execv with current sys.argv."""
    if not hasattr(signal, "SIGHUP"):
        pytest.skip("SIGHUP not available on this platform")

    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [55])
    monkeypatch.setattr(cli, "_pr_is_still_open", lambda _pr: True)

    procs_made = []

    def fake_popen(_cmd, **_kwargs):
        proc = _FakeProc(pid=500 + len(procs_made), exit_after_polls=99)
        procs_made.append(proc)
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    # Capture installed signal handlers so we can fire SIGHUP synthetically.
    handlers = {}
    real_signal_signal = signal.signal

    def capture_signal(signum, handler):
        handlers[signum] = handler
        return real_signal_signal(signum, signal.SIG_IGN)

    monkeypatch.setattr(cli.signal, "signal", capture_signal)

    # Fire SIGHUP on the first supervisor_wait call.
    wait_calls = {"n": 0}

    def fake_wait(event, _timeout):
        wait_calls["n"] += 1
        if wait_calls["n"] == 1:
            # Invoke the SIGHUP handler synchronously.
            assert signal.SIGHUP in handlers, "SIGHUP handler not installed"
            handlers[signal.SIGHUP](signal.SIGHUP, None)
        return event.wait(0.0) or wait_calls["n"] >= 5

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)

    exec_calls = []

    def fake_execv(path, argv):
        exec_calls.append((path, list(argv)))
        # os.execv normally never returns; raise to short-circuit.
        raise SystemExit("execv-called")

    monkeypatch.setattr(cli.os, "execv", fake_execv)

    script_path = tmp_path / "ralph_script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    monkeypatch.setattr(
        sys, "argv", [str(script_path), "--all-prs", "--base", "main"]
    )

    with pytest.raises(SystemExit, match="execv-called"):
        cli._fan_out_all_prs(
            cli_args(
                all_prs=True,
                fan_out_log_dir=str(log_dir),
                fan_out_respawn_backoff_seconds=1,
                fan_out_stuck_timeout_seconds=60,
            ),
            ["script.py", "--all-prs"],
            str(script_path),
        )

    assert len(exec_calls) == 1
    exec_path, exec_argv = exec_calls[0]
    assert exec_path == sys.executable
    assert exec_argv[0] == sys.executable
    assert exec_argv[1] == str(script_path)
    # The remaining argv should be sys.argv[1:] verbatim (the args the user
    # originally passed to the supervisor).
    assert exec_argv[2:] == ["--all-prs", "--base", "main"]
    # Children should have been terminated (but not necessarily waited for).
    assert any(p.terminated for p in procs_made)


def test_fan_out_no_execv_when_normal_shutdown(monkeypatch, cli_args, tmp_path):
    """SIGINT/SIGTERM path must not call os.execv."""
    monkeypatch.setattr(cli, "_list_open_prs", lambda _base: [77])
    monkeypatch.setattr(cli, "_pr_is_still_open", lambda _pr: True)

    def fake_popen(_cmd, **_kwargs):
        return _FakeProc(pid=600, exit_after_polls=99)

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    handlers = {}
    real_signal_signal = signal.signal

    def capture_signal(signum, handler):
        handlers[signum] = handler
        return real_signal_signal(signum, signal.SIG_IGN)

    monkeypatch.setattr(cli.signal, "signal", capture_signal)

    wait_calls = {"n": 0}

    def fake_wait(event, _timeout):
        wait_calls["n"] += 1
        if wait_calls["n"] == 1:
            handlers[signal.SIGINT](signal.SIGINT, None)
        return event.wait(0.0) or wait_calls["n"] >= 5

    monkeypatch.setattr(cli, "_supervisor_wait", fake_wait)

    exec_calls = []
    monkeypatch.setattr(
        cli.os, "execv", lambda *a, **k: exec_calls.append(a)
    )

    script_path = tmp_path / "ralph_script.py"
    script_path.write_text("# stub\n")
    log_dir = tmp_path / "logs"

    rc = cli._fan_out_all_prs(
        cli_args(
            all_prs=True,
            fan_out_log_dir=str(log_dir),
            fan_out_respawn_backoff_seconds=1,
            fan_out_stuck_timeout_seconds=60,
        ),
        ["script.py", "--all-prs"],
        str(script_path),
    )

    assert rc == 0
    assert exec_calls == [], "SIGINT must not trigger os.execv"
