import json
import subprocess
import sys

import pytest

from ralph_loop import git_ops, process
from ralph_loop.errors import CommandError, RebaseConflictError


@pytest.fixture(autouse=True)
def clear_command_deadline():
    yield
    process._set_command_deadline(None)


def test_printable_cmd_quotes_and_truncates_long_arguments():
    assert process._printable_cmd(["git", "commit", "-m", "hello world"]) == (
        "git commit -m 'hello world'"
    )
    assert process._printable_cmd(["cmd", "abcdef"], max_arg_len=3) == (
        "cmd 'abc...<+3 chars>'"
    )


def test_truncate_for_log_leaves_short_text_and_marks_truncation():
    assert process._truncate_for_log("abcdef", limit=10) == "abcdef"
    assert "truncated" in process._truncate_for_log("abcdefghij", limit=6)


def test_print_step_writes_timestamped_stderr_line(monkeypatch, capsys):
    class FakeDateTime:
        @classmethod
        def now(cls):
            return cls()

        def strftime(self, _fmt):
            return "12:34:56"

    monkeypatch.setattr(process.datetime, "datetime", FakeDateTime)

    process._print_step("hello")

    assert capsys.readouterr().err == "\n[12:34:56] ==> hello\n"


def test_print_step_appends_structured_json_event(monkeypatch, tmp_path, capsys):
    class FakeDateTime:
        @classmethod
        def now(cls):
            return cls()

        def strftime(self, _fmt):
            return "12:34:56"

        def isoformat(self, timespec="seconds"):
            assert timespec == "seconds"
            return "2026-05-17T12:34:56"

    log_path = tmp_path / "ralph.jsonl"
    monkeypatch.setattr(process.datetime, "datetime", FakeDateTime)
    process._configure_json_log(str(log_path))
    try:
        process._print_step("hello", event="unit.test", answer=42)
    finally:
        process._configure_json_log(None)

    assert capsys.readouterr().err == "\n[12:34:56] ==> hello\n"
    assert json.loads(log_path.read_text(encoding="utf-8")) == {
        "answer": 42,
        "event": "unit.test",
        "message": "hello",
        "timestamp": "2026-05-17T12:34:56",
    }


def test_run_command_captures_and_replays_output(capsys):
    result = process._run_command(
        [sys.executable, "-c", "print('out')"],
        capture_output=True,
    )

    assert result.returncode == 0
    assert result.stdout == "out\n"
    assert capsys.readouterr().out == "out\n"


def test_run_command_can_suppress_replayed_captured_output(capsys):
    result = process._run_command(
        [sys.executable, "-c", "print('secret output')"],
        capture_output=True,
        replay_output=False,
    )

    assert result.returncode == 0
    assert result.stdout == "secret output\n"
    assert capsys.readouterr().out == ""


def test_run_command_raises_command_error_when_checked_command_fails():
    with pytest.raises(CommandError, match="Command failed"):
        process._run_command(
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            capture_output=True,
        )


def test_run_command_wraps_missing_executable(monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(
        process.subprocess,
        "run",
        missing,
    )

    with pytest.raises(CommandError, match="Unable to run command"):
        process._run_command(["missing-tool"], capture_output=True)


def test_run_command_supports_uncaptured_output():
    result = process._run_command(
        [sys.executable, "-c", "pass"],
        capture_output=False,
    )

    assert result.returncode == 0
    assert result.stdout is None


def test_run_command_uses_remaining_wall_clock_deadline_as_timeout(
    monkeypatch, spy, completed_process
):
    run = spy(return_value=completed_process())
    process._set_command_deadline(110.0)
    monkeypatch.setattr(process.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(process.subprocess, "run", run)

    process._run_command(["cmd"], capture_output=False)

    assert run.call_args.kwargs["timeout"] == 10.0


def test_run_command_disconnects_inherited_stdin(monkeypatch, spy, completed_process):
    run = spy(return_value=completed_process())
    monkeypatch.setattr(process.subprocess, "run", run)

    process._run_command(["cmd"], capture_output=False)

    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


def test_run_command_can_pass_captured_stdin(monkeypatch, spy, completed_process):
    def fake_run(_cmd, **kwargs):
        assert kwargs["stdin"] is None
        assert kwargs["input"] == b"prompt body"
        return completed_process()

    run = spy(side_effect=fake_run)
    monkeypatch.setattr(process.subprocess, "run", run)

    process._run_command(["cmd"], capture_output=True, input_text="prompt body")


def test_run_command_can_pass_uncaptured_stdin(monkeypatch, spy, completed_process):
    def fake_run(_cmd, **kwargs):
        assert kwargs["stdin"] is None
        assert kwargs["input"] == "prompt body"
        assert kwargs["text"] is True
        return completed_process()

    run = spy(side_effect=fake_run)
    monkeypatch.setattr(process.subprocess, "run", run)

    process._run_command(["cmd"], capture_output=False, input_text="prompt body")


def test_run_command_logs_redacted_command_when_supplied(
    monkeypatch, capsys, spy, completed_process
):
    run = spy(return_value=completed_process())
    monkeypatch.setattr(process.subprocess, "run", run)

    process._run_command(
        ["codex", "exec", "prompt with secret-token"],
        capture_output=False,
        log_cmd=["codex", "exec", "<codex prompt>"],
    )

    stderr = capsys.readouterr().err
    assert "<codex prompt>" in stderr
    assert "secret-token" not in stderr


def test_run_command_can_suppress_command_logging(
    monkeypatch, capsys, spy, completed_process
):
    def fake_run(_cmd, **kwargs):
        kwargs["stdout"].write(b"private json\n")
        return completed_process(stdout=None)

    run = spy(side_effect=fake_run)
    monkeypatch.setattr(process.subprocess, "run", run)

    result = process._run_command(
        ["gh", "api", "secret"],
        capture_output=True,
        replay_output=False,
        log_cmd=[],
    )

    captured = capsys.readouterr()
    assert result.stdout == "private json\n"
    assert "gh api secret" not in captured.err
    assert "private json" not in captured.out


def test_run_command_interrupts_commands_when_wall_clock_deadline_expires(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["cmd"], 5.0)

    process._set_command_deadline(105.0)
    monkeypatch.setattr(process.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        process.subprocess,
        "run",
        timeout,
    )

    with pytest.raises(CommandError, match="timed out after 5.00s"):
        process._run_command(["cmd"], capture_output=False)


def test_run_command_replays_only_bounded_captured_output(monkeypatch, capsys):
    script = (
        "import sys; "
        "sys.stdout.write('A' * 80); "
        "sys.stderr.write('B' * 70)"
    )
    monkeypatch.setattr(process, "MAX_CAPTURED_STREAM_BYTES", 20)

    result = process._run_command(
        [sys.executable, "-c", script],
        capture_output=True,
    )
    captured = capsys.readouterr()

    assert result.returncode == 0
    assert "<truncated 60 bytes>" in result.stdout
    assert "<truncated 50 bytes>" in result.stderr
    assert captured.out == result.stdout
    assert captured.err.endswith(result.stderr)


def test_run_command_can_return_unbounded_machine_readable_output(monkeypatch):
    monkeypatch.setattr(process, "MAX_CAPTURED_STREAM_BYTES", 20)

    result = process._run_command(
        [sys.executable, "-c", "print('A' * 80)"],
        capture_output=True,
        replay_output=False,
        max_output_bytes=None,
    )

    assert result.stdout == "{}\n".format("A" * 80)


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("same\n", "same\n", "stdout+stderr:\nsame"),
        ("out\n", "err\n", "stdout:\nout\n\nstderr:\nerr"),
    ],
)
def test_completed_process_output_formats_and_deduplicates_streams(
    completed_process, stdout, stderr, expected
):
    result = completed_process(returncode=1, stdout=stdout, stderr=stderr)

    assert process._completed_process_output(result) == expected


def test_git_output_returns_stripped_stdout(monkeypatch, spy, completed_process):
    run = spy(return_value=completed_process(stdout="abc\n"))
    monkeypatch.setattr(git_ops, "_run_command", run)

    assert git_ops._git_output(["rev-parse", "HEAD"]) == "abc"
    run.assert_called_once_with(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
    )


def test_git_config_get_returns_empty_string_for_missing_config(
    monkeypatch, completed_process
):
    monkeypatch.setattr(
        git_ops,
        "_run_command",
        lambda *_args, **_kwargs: completed_process(returncode=1, stderr="missing"),
    )

    assert git_ops._git_config_get("user.name") == ""


def test_git_branch_rejects_detached_head(monkeypatch):
    monkeypatch.setattr(git_ops, "_git_output", lambda _args: "HEAD")

    with pytest.raises(CommandError, match="Detached HEAD"):
        git_ops._git_branch()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (" M file", True),
        ("", False),
    ],
)
def test_working_tree_dirty_uses_status_porcelain(monkeypatch, status, expected):
    monkeypatch.setattr(git_ops, "_git_output", lambda _args: status)

    assert git_ops._working_tree_dirty() is expected


def test_git_helpers_read_real_repository_state(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(
            ["git"] + list(args),
            cwd=str(tmp_path),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    git("init")
    git("checkout", "-b", "main")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.invalid")
    git("config", "commit.gpgsign", "false")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-m", "initial")
    monkeypatch.chdir(tmp_path)

    assert git_ops._git_branch() == "main"
    assert git_ops._working_tree_dirty() is False

    tracked.write_text("v2\n", encoding="utf-8")

    assert git_ops._working_tree_dirty() is True


def test_checkout_branch_noops_when_already_on_branch(monkeypatch, spy):
    run = spy()
    monkeypatch.setattr(git_ops, "_git_branch", lambda: "feature")
    monkeypatch.setattr(git_ops, "_run_command", run)

    git_ops._checkout_branch("feature")

    run.assert_not_called()


def test_checkout_branch_fetches_and_checks_out_existing_local_branch(
    monkeypatch, spy, completed_process
):
    run = spy(side_effect=[completed_process(), completed_process()])
    monkeypatch.setattr(git_ops, "_git_branch", lambda: "main")
    monkeypatch.setattr(git_ops, "_git_output", lambda _args: "  feature\n")
    monkeypatch.setattr(git_ops, "_run_command", run)

    git_ops._checkout_branch("feature")

    assert run.call_args_list[-1].args[0] == ["git", "checkout", "feature"]


def test_checkout_branch_tracks_remote_when_no_local_branch_exists(
    monkeypatch, spy, completed_process
):
    run = spy(side_effect=[completed_process(), completed_process()])
    monkeypatch.setattr(git_ops, "_git_branch", lambda: "main")
    monkeypatch.setattr(git_ops, "_git_output", lambda _args: "")
    monkeypatch.setattr(git_ops, "_run_command", run)

    git_ops._checkout_branch("feature")

    assert run.call_args_list[-1].args[0] == [
        "git",
        "checkout",
        "-b",
        "feature",
        "--track",
        "origin/feature",
    ]


def test_checkout_branch_exits_cleanly_when_branch_used_by_worktree(
    monkeypatch, capsys, spy, completed_process
):
    monkeypatch.setattr(git_ops, "_git_branch", lambda: "main")
    monkeypatch.setattr(git_ops, "_git_output", lambda _args: "feature")
    monkeypatch.setattr(
        git_ops,
        "_run_command",
        spy(
            side_effect=[
                completed_process(),
                completed_process(returncode=1, stderr="fatal: already used by worktree"),
            ]
        ),
    )

    with pytest.raises(SystemExit) as raised:
        git_ops._checkout_branch("feature")

    assert raised.value.code == 0
    assert "found another ralph loop" in capsys.readouterr().out


def test_checkout_branch_raises_command_error_for_other_checkout_failures(
    monkeypatch, spy, completed_process
):
    monkeypatch.setattr(git_ops, "_git_branch", lambda: "main")
    monkeypatch.setattr(git_ops, "_git_output", lambda _args: "feature")
    monkeypatch.setattr(
        git_ops,
        "_run_command",
        spy(
            side_effect=[
                completed_process(),
                completed_process(returncode=1, stderr="boom"),
            ]
        ),
    )

    with pytest.raises(CommandError, match="Unable to checkout"):
        git_ops._checkout_branch("feature")


def test_reset_generated_changes_cleans_ignored_files_when_clean_and_at_target(
    monkeypatch, spy
):
    run = spy()
    monkeypatch.setattr(git_ops, "_git_head_sha", lambda: "abc")
    monkeypatch.setattr(git_ops, "_working_tree_dirty", lambda: False)
    monkeypatch.setattr(git_ops, "_run_command", run)

    git_ops._reset_generated_changes("abc")

    run.assert_called_once_with(
        ["git", "clean", "-fdx"],
        check=True,
        capture_output=True,
    )


def test_reset_generated_changes_runs_reset_and_clean_when_dirty(
    monkeypatch, spy, completed_process
):
    run = spy(return_value=completed_process())
    monkeypatch.setattr(git_ops, "_git_head_sha", lambda: "def")
    monkeypatch.setattr(git_ops, "_working_tree_dirty", lambda: True)
    monkeypatch.setattr(git_ops, "_run_command", run)

    git_ops._reset_generated_changes("abc")

    assert run.call_args_list[0].args[0] == ["git", "reset", "--hard", "abc"]
    assert run.call_args_list[1].args[0] == ["git", "clean", "-fdx"]


def test_rebase_fetches_rebases_and_force_pushes(monkeypatch, spy, completed_process):
    run = spy(return_value=completed_process())
    monkeypatch.setattr(git_ops, "_run_command", run)

    git_ops._rebase_onto_base("feature", "main")

    assert [call.args[0] for call in run.call_args_list] == [
        ["git", "fetch", "origin", "main"],
        ["git", "rebase", "origin/main"],
        ["git", "push", "--force-with-lease", "origin", "feature"],
    ]


def test_rebase_conflict_raises_dedicated_error_and_aborts(
    monkeypatch, spy, completed_process
):
    responses = [
        completed_process(),
        completed_process(
            returncode=1,
            stdout="Auto-merging README.md\nCONFLICT (content): Merge conflict in README.md\n",
            stderr="error: could not apply abc123... update docs\n",
        ),
        completed_process(),
    ]
    run = spy(side_effect=responses)
    monkeypatch.setattr(git_ops, "_run_command", run)

    with pytest.raises(RebaseConflictError, match="Rebase conflict"):
        git_ops._rebase_onto_base("feature", "main")

    assert [call.args[0] for call in run.call_args_list] == [
        ["git", "fetch", "origin", "main"],
        ["git", "rebase", "origin/main"],
        ["git", "rebase", "--abort"],
    ]
    assert run.call_args_list[1].kwargs["check"] is False
    assert run.call_args_list[2].kwargs["check"] is False
