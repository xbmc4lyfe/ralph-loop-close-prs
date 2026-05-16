import os

import pytest

from ralph_loop import checks, runtime, worktrees
from ralph_loop.errors import CommandError


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("feature/foo bar@123", "feature-foo-bar-123"),
        ("!!!", "unknown"),
    ],
)
def test_slug_keeps_safe_chars_and_collapses_unsafe_chars(value, expected):
    assert worktrees._slug(value) == expected


def test_loop_lock_returns_none_without_key_and_keeps_lock_file_after_release(
    monkeypatch, tmp_path
):
    assert worktrees._acquire_loop_lock(pr_number=None) is None
    monkeypatch.setattr(worktrees.tempfile, "gettempdir", lambda: str(tmp_path))

    lock = worktrees._acquire_loop_lock(pr_number=123)
    path = lock.path

    assert os.path.exists(path)
    with open(path, "r", encoding="utf-8") as handle:
        assert handle.read() == "{}\n".format(os.getpid())

    lock.release()
    assert os.path.exists(path)

    next_lock = worktrees._acquire_loop_lock(pr_number=123)
    assert next_lock.path == path
    next_lock.release()


def test_loop_lock_exits_cleanly_when_flock_is_blocked(monkeypatch, tmp_path, capsys):
    def blocked(*_args, **_kwargs):
        raise BlockingIOError()

    monkeypatch.setattr(worktrees.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(worktrees.fcntl, "flock", blocked)

    with pytest.raises(SystemExit) as raised:
        worktrees._acquire_loop_lock(pr_number=123)

    assert raised.value.code == 0
    assert "found another ralph loop" in capsys.readouterr().err


def test_loop_lock_rejects_preexisting_symlink(monkeypatch, tmp_path):
    monkeypatch.setattr(worktrees.tempfile, "gettempdir", lambda: str(tmp_path))
    target = tmp_path / "target"
    target.write_text("do not clobber\n", encoding="utf-8")
    (tmp_path / "codex-ralph-loop-pr-123.lock").symlink_to(target)

    with pytest.raises(CommandError, match="Refusing to use symlink"):
        worktrees._acquire_loop_lock(pr_number=123)

    assert target.read_text(encoding="utf-8") == "do not clobber\n"


def test_worktree_path_slugifies_branch_name(tmp_path):
    assert worktrees._worktree_path(
        worktree_root=str(tmp_path),
        pr_number=5,
        branch="feature/foo bar",
    ) == os.path.join(str(tmp_path), "pr-5-feature-foo-bar")


def test_pr_head_fetch_ref_is_stable():
    assert worktrees._pr_head_fetch_ref(42) == "refs/remotes/origin/pr-42-head"


def test_fetch_pr_branch_uses_origin_branch_when_fetch_succeeds(
    monkeypatch, spy, completed_process
):
    run = spy(return_value=completed_process())
    monkeypatch.setattr(worktrees, "_run_command", run)

    assert (
        worktrees._fetch_pr_branch_or_head(
            pr_number=8,
            branch="feature",
            cwd="/repo",
        )
        == "origin/feature"
    )
    run.assert_called_once_with(
        ["git", "fetch", "origin", "feature"],
        check=False,
        capture_output=True,
        cwd="/repo",
    )


def test_fetch_pr_branch_falls_back_to_pull_head_ref(
    monkeypatch, spy, completed_process
):
    run = spy(
        side_effect=[completed_process(returncode=1, stderr="nope"), completed_process()]
    )
    monkeypatch.setattr(worktrees, "_run_command", run)

    assert (
        worktrees._fetch_pr_branch_or_head(pr_number=8, branch="missing")
        == "refs/remotes/origin/pr-8-head"
    )
    assert run.call_args_list[1].args[0] == [
        "git",
        "fetch",
        "origin",
        "+refs/pull/8/head:refs/remotes/origin/pr-8-head",
    ]


def test_worktree_for_branch_parses_porcelain_output(monkeypatch, completed_process):
    porcelain = "\n".join(
        [
            "worktree /repo",
            "HEAD abc",
            "branch refs/heads/main",
            "",
            "worktree /repo-feature",
            "HEAD def",
            "branch refs/heads/feature",
        ]
    )
    monkeypatch.setattr(
        worktrees,
        "_run_command",
        lambda *_args, **_kwargs: completed_process(stdout=porcelain),
    )

    assert worktrees._worktree_for_branch("feature") == "/repo-feature"
    assert worktrees._worktree_for_branch("other") is None


def test_ensure_pr_worktree_refuses_other_branch_worktree(
    monkeypatch, tmp_path, capsys, spy
):
    fetch = spy(return_value="origin/feature")
    monkeypatch.setattr(worktrees, "_fetch_pr_branch_or_head", fetch)
    monkeypatch.setattr(worktrees, "_worktree_for_branch", lambda _branch: "/existing")

    with pytest.raises(SystemExit) as raised:
        worktrees._ensure_pr_worktree(
            worktree_root=str(tmp_path),
            pr_number=9,
            branch="feature",
        )

    assert raised.value.code == 0
    assert fetch.call_count == 1
    assert "found another ralph loop" in capsys.readouterr().out


def test_ensure_pr_worktree_operates_in_place_when_branch_is_checked_out_at_cwd(
    monkeypatch, tmp_path
):
    target = tmp_path / "primary-checkout"
    target.mkdir()
    monkeypatch.chdir(target)
    monkeypatch.setattr(
        worktrees, "_fetch_pr_branch_or_head", lambda **_kwargs: "origin/feature"
    )
    monkeypatch.setattr(
        worktrees, "_worktree_for_branch", lambda _branch: str(target)
    )
    origin_calls = []
    sync_calls = []
    monkeypatch.setattr(
        worktrees,
        "_ensure_worktree_origin_matches",
        lambda path: origin_calls.append(path),
    )
    monkeypatch.setattr(
        worktrees,
        "_sync_existing_worktree",
        lambda **kwargs: sync_calls.append(kwargs),
    )

    result = worktrees._ensure_pr_worktree(
        worktree_root=str(tmp_path / "worktrees"),
        pr_number=9,
        branch="feature",
    )

    assert result == os.path.abspath(str(target))
    assert origin_calls == [os.path.abspath(str(target))]
    assert sync_calls and sync_calls[0]["path"] == os.path.abspath(str(target))


def test_ensure_pr_worktree_reuses_matching_path_and_rejects_wrong_branch(
    monkeypatch, tmp_path, completed_process
):
    path = worktrees._worktree_path(
        worktree_root=str(tmp_path), pr_number=9, branch="feature"
    )
    os.makedirs(path)
    monkeypatch.setattr(
        worktrees, "_fetch_pr_branch_or_head", lambda **_kwargs: "origin/feature"
    )
    monkeypatch.setattr(worktrees, "_worktree_for_branch", lambda _branch: None)
    monkeypatch.setattr(worktrees, "_worktree_path_is_registered", lambda _path: True)
    monkeypatch.setattr(worktrees, "_ensure_worktree_origin_matches", lambda _path: None)
    monkeypatch.setattr(worktrees, "_sync_existing_worktree", lambda **_kwargs: None)

    monkeypatch.setattr(
        worktrees,
        "_run_command",
        lambda *_args, **_kwargs: completed_process(stdout="feature\n"),
    )
    assert (
        worktrees._ensure_pr_worktree(
            worktree_root=str(tmp_path),
            pr_number=9,
            branch="feature",
        )
        == path
    )

    monkeypatch.setattr(
        worktrees,
        "_run_command",
        lambda *_args, **_kwargs: completed_process(stdout="other\n"),
    )
    with pytest.raises(CommandError, match="instead of 'feature'"):
        worktrees._ensure_pr_worktree(
            worktree_root=str(tmp_path),
            pr_number=9,
            branch="feature",
        )


def test_ensure_pr_worktree_rejects_unregistered_existing_path(monkeypatch, tmp_path):
    path = worktrees._worktree_path(
        worktree_root=str(tmp_path), pr_number=9, branch="feature"
    )
    os.makedirs(path)
    monkeypatch.setattr(
        worktrees, "_fetch_pr_branch_or_head", lambda **_kwargs: "origin/feature"
    )
    monkeypatch.setattr(worktrees, "_worktree_for_branch", lambda _branch: None)
    monkeypatch.setattr(worktrees, "_worktree_path_is_registered", lambda _path: False)

    with pytest.raises(CommandError, match="not registered"):
        worktrees._ensure_pr_worktree(
            worktree_root=str(tmp_path),
            pr_number=9,
            branch="feature",
        )


def test_sync_existing_worktree_fast_forwards_clean_stale_branch(
    monkeypatch, spy, completed_process
):
    run = spy(
        side_effect=[
            completed_process(stdout=""),
            completed_process(stdout="old\n"),
            completed_process(returncode=0),
            completed_process(),
        ]
    )
    monkeypatch.setattr(worktrees, "_run_command", run)

    worktrees._sync_existing_worktree(path="/wt", start_ref="origin/feature")

    assert [call.args[0] for call in run.call_args_list] == [
        ["git", "-C", "/wt", "status", "--porcelain"],
        ["git", "-C", "/wt", "rev-parse", "HEAD"],
        ["git", "-C", "/wt", "merge-base", "--is-ancestor", "old", "origin/feature"],
        ["git", "-C", "/wt", "reset", "--hard", "origin/feature"],
    ]


def test_ensure_pr_worktree_creates_new_worktree(
    monkeypatch, tmp_path, spy, completed_process
):
    run = spy(return_value=completed_process())
    monkeypatch.setattr(
        worktrees, "_fetch_pr_branch_or_head", lambda **_kwargs: "origin/feature"
    )
    monkeypatch.setattr(worktrees, "_worktree_for_branch", lambda _branch: None)
    monkeypatch.setattr(worktrees, "_local_branch_exists", lambda _branch: False)
    monkeypatch.setattr(worktrees, "_run_command", run)

    result = worktrees._ensure_pr_worktree(
        worktree_root=str(tmp_path),
        pr_number=9,
        branch="feature",
    )

    assert result.endswith("pr-9-feature")
    assert run.call_args.args[0] == [
        "git",
        "worktree",
        "add",
        result,
        "-b",
        "feature",
        "origin/feature",
    ]


def test_ensure_pr_worktree_does_not_reset_existing_unoccupied_local_branch(
    monkeypatch, tmp_path, spy, completed_process
):
    run = spy(return_value=completed_process())
    monkeypatch.setattr(
        worktrees, "_fetch_pr_branch_or_head", lambda **_kwargs: "origin/feature"
    )
    monkeypatch.setattr(worktrees, "_worktree_for_branch", lambda _branch: None)
    monkeypatch.setattr(worktrees, "_local_branch_exists", lambda _branch: True)
    monkeypatch.setattr(worktrees, "_run_command", run)

    result = worktrees._ensure_pr_worktree(
        worktree_root=str(tmp_path),
        pr_number=9,
        branch="feature",
    )

    assert run.call_args.args[0] == ["git", "worktree", "add", result, "feature"]


@pytest.mark.parametrize(
    ("stderr", "expected_exception", "expected_message"),
    [
        ("already used by worktree", SystemExit, "found another ralph loop"),
        ("fatal boom", CommandError, "Unable to create worktree"),
    ],
)
def test_ensure_pr_worktree_handles_create_failures(
    monkeypatch, tmp_path, capsys, completed_process, stderr, expected_exception, expected_message
):
    monkeypatch.setattr(
        worktrees, "_fetch_pr_branch_or_head", lambda **_kwargs: "origin/feature"
    )
    monkeypatch.setattr(worktrees, "_worktree_for_branch", lambda _branch: None)
    monkeypatch.setattr(worktrees, "_local_branch_exists", lambda _branch: False)
    monkeypatch.setattr(
        worktrees,
        "_run_command",
        lambda *_args, **_kwargs: completed_process(returncode=1, stderr=stderr),
    )

    with pytest.raises(expected_exception):
        worktrees._ensure_pr_worktree(
            worktree_root=str(tmp_path),
            pr_number=9,
            branch="feature",
        )

    captured = capsys.readouterr()
    assert expected_message in captured.out or expected_exception is CommandError


def test_check_wall_clock_raises_after_deadline(monkeypatch):
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 11)

    with pytest.raises(CommandError, match="Wall-clock timeout"):
        runtime._check_wall_clock(10)
    runtime._check_wall_clock(None)


def test_round_numbers_supports_bounded_and_unbounded_modes():
    assert list(runtime._round_numbers(3)) == [1, 2, 3]
    generator = runtime._round_numbers(0)
    assert [next(generator), next(generator), next(generator)] == [1, 2, 3]


def test_check_formatting_summarizes_and_lists_failures():
    check_records = [
        {"name": "unit", "bucket": "pass", "state": "SUCCESS"},
        {
            "name": "lint",
            "bucket": "fail",
            "state": "FAILURE",
            "link": "https://x",
            "workflow": "ci.yml",
        },
        {"name": "build", "bucket": "cancel", "state": "CANCELLED"},
    ]

    assert checks._bucket_summary(check_records) == "cancel=1, fail=1, pass=1"
    failing = checks._failing_check_records(check_records)
    assert [record["name"] for record in failing] == ["lint", "build"]
    assert "- lint [FAILURE] workflow=ci.yml https://x" in checks._format_failing_checks(
        failing
    )


def test_wait_for_checks_green_polls_until_checks_appear(monkeypatch, spy):
    monkeypatch.setattr(
        checks,
        "_required_checks",
        spy(side_effect=[([], True), ([{"name": "unit", "bucket": "pass"}], True)]),
    )
    sleep = spy()
    monkeypatch.setattr(checks.time, "sleep", sleep)

    assert checks._wait_for_required_checks_green(
        branch="feature",
        poll_seconds=1,
        timeout_seconds=10,
    ) == (True, [{"name": "unit", "bucket": "pass"}])
    sleep.assert_called_once_with(1)


def test_wait_for_checks_green_times_out_before_any_checks_appear(monkeypatch):
    monkeypatch.setattr(checks, "_required_checks", lambda _branch: ([], True))
    monkeypatch.setattr(checks.time, "monotonic", spy_time([0, 11]))

    with pytest.raises(CommandError, match="Timed out waiting for checks"):
        checks._wait_for_required_checks_green(
            branch="feature",
            poll_seconds=1,
            timeout_seconds=10,
        )


@pytest.mark.parametrize(
    ("check_records", "were_required", "treat_optional_as_blocking", "expected"),
    [
        ([{"name": "lint", "bucket": "fail"}], False, False, True),
        (
            [{"name": "unit", "bucket": "pass"}, {"name": "skip", "bucket": "skipping"}],
            True,
            True,
            True,
        ),
        ([{"name": "unit", "bucket": "fail"}], True, True, False),
    ],
)
def test_wait_for_checks_green_returns_terminal_check_states(
    monkeypatch, check_records, were_required, treat_optional_as_blocking, expected
):
    monkeypatch.setattr(
        checks, "_required_checks", lambda _branch: (check_records, were_required)
    )

    assert checks._wait_for_required_checks_green(
        branch="feature",
        poll_seconds=1,
        timeout_seconds=10,
        treat_optional_as_blocking=treat_optional_as_blocking,
    ) == (expected, check_records)


def test_wait_for_checks_green_waits_for_pending_checks(monkeypatch, spy):
    pending = [{"name": "unit", "bucket": "pending"}]
    passing = [{"name": "unit", "bucket": "pass"}]
    monkeypatch.setattr(
        checks,
        "_required_checks",
        spy(side_effect=[(pending, True), (passing, True)]),
    )
    sleep = spy()
    monkeypatch.setattr(checks.time, "sleep", sleep)

    assert checks._wait_for_required_checks_green(
        branch="feature",
        poll_seconds=3,
        timeout_seconds=10,
    ) == (True, passing)
    sleep.assert_called_once_with(3)


def test_wait_for_checks_green_does_not_succeed_on_optional_checks_before_required(
    monkeypatch, spy
):
    optional = [{"name": "lint", "bucket": "pass"}]
    required = [{"name": "unit", "bucket": "pass"}]
    monkeypatch.setattr(
        checks,
        "_required_checks",
        spy(side_effect=[(optional, False), (required, True)]),
    )
    sleep = spy()
    monkeypatch.setattr(checks.time, "sleep", sleep)

    assert checks._wait_for_required_checks_green(
        branch="feature",
        poll_seconds=3,
        timeout_seconds=10,
    ) == (True, required)
    sleep.assert_called_once_with(3)


def test_wait_for_checks_green_caps_sleep_to_remaining_timeout(monkeypatch, spy):
    pending = [{"name": "unit", "bucket": "pending"}]
    passing = [{"name": "unit", "bucket": "pass"}]
    monkeypatch.setattr(
        checks,
        "_required_checks",
        spy(side_effect=[(pending, True), (passing, True)]),
    )
    monkeypatch.setattr(checks.time, "monotonic", spy_time([0, 8, 8, 9]))
    sleep = spy()
    monkeypatch.setattr(checks.time, "sleep", sleep)

    assert checks._wait_for_required_checks_green(
        branch="feature",
        poll_seconds=10,
        timeout_seconds=9,
    ) == (True, passing)
    sleep.assert_called_once_with(1)


def test_wait_for_checks_green_times_out_while_checks_are_pending(monkeypatch):
    monkeypatch.setattr(
        checks,
        "_required_checks",
        lambda _branch: ([{"name": "unit", "bucket": "pending"}], True),
    )
    monkeypatch.setattr(checks.time, "monotonic", spy_time([0, 11]))

    with pytest.raises(CommandError, match="Timed out"):
        checks._wait_for_required_checks_green(
            branch="feature",
            poll_seconds=1,
            timeout_seconds=10,
        )


def spy_time(values):
    values = list(values)

    def monotonic():
        return values.pop(0)

    return monotonic
