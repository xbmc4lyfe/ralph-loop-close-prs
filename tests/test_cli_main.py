import argparse
import os
import runpy
import signal
import sys

import pytest

from ralph_loop import cli
from ralph_loop.errors import CommandError


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


def test_main_dry_run_validates_pr_without_mutating_local_or_remote_state(
    cli_harness, cli_args, capsys
):
    args = cli_args(dry_run=True, skip_rebase=False, skip_merge=False)
    harness = cli_harness(args=args)

    assert cli.main() == 0

    stderr = capsys.readouterr().err
    assert "Dry run: validated PR #7" in stderr
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


def test_main_signal_handler_exits_with_shell_status(
    monkeypatch, cli_args, capsys
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
    assert "Received SIGINT" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("pr_record", "message"),
    [
        ({"number": "7"}, "Could not resolve PR number"),
        ({"state": "CLOSED"}, "is not open"),
        ({"isDraft": True}, "draft state"),
        ({"isCrossRepository": True}, "fork PR"),
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
    harness.review_round.return_value = False
    harness.commit_push.return_value = commit_state

    with pytest.raises(CommandError, match="Review loop exhausted"):
        cli.main()

    assert harness.reset_changes.called is (commit_state == "no_changes")


def test_main_review_pass_discarded_keeps_loop_failed(cli_harness):
    harness = cli_harness()
    harness.review_round.return_value = True
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


def test_compatibility_script_prints_command_error(monkeypatch, capsys):
    path = os.path.join(os.getcwd(), "codex_ralph_wiggum_loop.py")

    def boom():
        raise CommandError("boom")

    monkeypatch.setattr(cli, "main", boom)

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(path, run_name="__main__")

    assert raised.value.code == 1
    assert "ERROR: boom" in capsys.readouterr().err
