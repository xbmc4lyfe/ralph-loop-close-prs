import os

import pytest

from ralph_loop import checks, runtime, worktrees
from ralph_loop.errors import LOOP_ALREADY_RUNNING_EXIT_CODE, CommandError


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

    assert raised.value.code == LOOP_ALREADY_RUNNING_EXIT_CODE
    assert "found another ralph loop" in capsys.readouterr().err


def test_loop_lock_rejects_preexisting_symlink(monkeypatch, tmp_path):
    monkeypatch.setattr(worktrees.tempfile, "gettempdir", lambda: str(tmp_path))
    target = tmp_path / "target"
    target.write_text("do not clobber\n", encoding="utf-8")
    (tmp_path / "codex-ralph-loop-pr-123.lock").symlink_to(target)

    with pytest.raises(CommandError, match="Refusing to use symlink"):
        worktrees._acquire_loop_lock(pr_number=123)

    assert target.read_text(encoding="utf-8") == "do not clobber\n"


def test_loop_lock_uses_no_follow_when_opening_lock_file(monkeypatch, tmp_path, spy):
    monkeypatch.setattr(worktrees.tempfile, "gettempdir", lambda: str(tmp_path))
    real_open = worktrees.os.open
    open_spy = spy(side_effect=real_open)
    monkeypatch.setattr(worktrees.os, "open", open_spy)

    lock = worktrees._acquire_loop_lock(pr_number=123)
    lock.release()

    _path, flags, _mode = open_spy.call_args.args
    assert flags & getattr(os, "O_NOFOLLOW", 0)


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

    assert raised.value.code == LOOP_ALREADY_RUNNING_EXIT_CODE
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

    seen_cmds = []

    def fake_run_recoverable(cmd, *_args, **_kwargs):
        seen_cmds.append(cmd)
        if cmd[:5] == ["git", "-C", str(path), "rev-parse", "--abbrev-ref"]:
            return completed_process(stdout="other\n")
        if cmd[:4] == ["git", "-C", str(path), "checkout"]:
            return completed_process(returncode=0)
        return completed_process(stdout="")

    monkeypatch.setattr(worktrees, "_run_command", fake_run_recoverable)
    assert (
        worktrees._ensure_pr_worktree(
            worktree_root=str(tmp_path),
            pr_number=9,
            branch="feature",
        )
        == path
    )
    assert any(
        cmd[:4] == ["git", "-C", str(path), "checkout"]
        and "feature" in cmd
        for cmd in seen_cmds
    )

    def fake_run_checkout_fails(cmd, *_args, **_kwargs):
        if cmd[:5] == ["git", "-C", str(path), "rev-parse", "--abbrev-ref"]:
            return completed_process(stdout="other\n")
        if cmd[:4] == ["git", "-C", str(path), "checkout"]:
            return completed_process(returncode=1, stderr="checkout failed")
        return completed_process(stdout="")

    monkeypatch.setattr(worktrees, "_run_command", fake_run_checkout_fails)
    with pytest.raises(CommandError, match="Could not restore branch"):
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
            completed_process(returncode=1, stderr="No rebase in progress?"),
            completed_process(stdout=""),
            completed_process(stdout="old\n"),
            completed_process(returncode=0),
            completed_process(),
        ]
    )
    monkeypatch.setattr(worktrees, "_run_command", run)

    worktrees._sync_existing_worktree(path="/wt", start_ref="origin/feature")

    assert [call.args[0] for call in run.call_args_list] == [
        ["git", "-C", "/wt", "rebase", "--abort"],
        ["git", "-C", "/wt", "status", "--porcelain"],
        ["git", "-C", "/wt", "rev-parse", "HEAD"],
        ["git", "-C", "/wt", "merge-base", "--is-ancestor", "old", "origin/feature"],
        ["git", "-C", "/wt", "reset", "--hard", "origin/feature"],
    ]


def test_sync_existing_worktree_aborts_interrupted_rebase_before_status(
    monkeypatch, spy, completed_process
):
    run = spy(
        side_effect=[
            completed_process(returncode=0),
            completed_process(stdout=""),
            completed_process(stdout="old\n"),
            completed_process(returncode=0),
            completed_process(),
        ]
    )
    monkeypatch.setattr(worktrees, "_run_command", run)

    worktrees._sync_existing_worktree(path="/wt", start_ref="origin/feature")

    assert [call.args[0] for call in run.call_args_list] == [
        ["git", "-C", "/wt", "rebase", "--abort"],
        ["git", "-C", "/wt", "status", "--porcelain"],
        ["git", "-C", "/wt", "rev-parse", "HEAD"],
        ["git", "-C", "/wt", "merge-base", "--is-ancestor", "old", "origin/feature"],
        ["git", "-C", "/wt", "reset", "--hard", "origin/feature"],
    ]


def test_sync_existing_worktree_rejects_unknown_rebase_abort_failure(
    monkeypatch, completed_process
):
    monkeypatch.setattr(
        worktrees,
        "_run_command",
        lambda *_args, **_kwargs: completed_process(
            returncode=1, stderr="could not read HEAD"
        ),
    )

    with pytest.raises(CommandError, match="Could not abort interrupted rebase"):
        worktrees._sync_existing_worktree(path="/wt", start_ref="origin/feature")


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


def test_cleanup_stale_loop_state_removes_locks_for_closed_prs(
    monkeypatch, tmp_path
):
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    worktree_root = tmp_path / "wt"
    worktree_root.mkdir()
    monkeypatch.setattr(worktrees.tempfile, "gettempdir", lambda: str(tmp_root))

    stale = tmp_root / "codex-ralph-loop-pr-101.lock"
    stale.write_text("123\n", encoding="utf-8")
    active = tmp_root / "codex-ralph-loop-pr-202.lock"
    active.write_text("456\n", encoding="utf-8")
    unrelated = tmp_root / "some-other-file.txt"
    unrelated.write_text("keep me", encoding="utf-8")

    counts = worktrees._cleanup_stale_loop_state(
        str(worktree_root), open_pr_numbers={202}
    )

    assert counts["locks_removed"] == 1
    assert not stale.exists()
    assert active.exists()
    assert unrelated.exists()


def test_cleanup_stale_loop_state_leaves_currently_held_locks_alone(
    monkeypatch, tmp_path
):
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    worktree_root = tmp_path / "wt"
    worktree_root.mkdir()
    monkeypatch.setattr(worktrees.tempfile, "gettempdir", lambda: str(tmp_root))

    held = tmp_root / "codex-ralph-loop-pr-303.lock"
    held.write_text("999\n", encoding="utf-8")
    fd = os.open(str(held), os.O_RDWR)
    try:
        import fcntl as _fcntl

        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)

        # PR 303 is *not* in open set, so normally it would be deleted, but
        # the active flock should make cleanup skip it.
        counts = worktrees._cleanup_stale_loop_state(
            str(worktree_root), open_pr_numbers=set()
        )
    finally:
        try:
            import fcntl as _fcntl

            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

    assert counts["locks_removed"] == 0
    assert held.exists()


def test_cleanup_stale_loop_state_respects_worktree_root_boundary(
    monkeypatch, tmp_path
):
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    worktree_root = tmp_path / "wt"
    worktree_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(worktrees.tempfile, "gettempdir", lambda: str(tmp_root))

    stale_dir = worktree_root / "pr-9-feature"
    stale_dir.mkdir()
    (stale_dir / "sentinel").write_text("x", encoding="utf-8")

    sneaky_target = outside / "victim"
    sneaky_target.mkdir()
    (sneaky_target / "do-not-delete").write_text("keep", encoding="utf-8")
    sneaky_link = worktree_root / "pr-99-symlink"
    sneaky_link.symlink_to(sneaky_target)

    run_calls = []

    def fake_run(cmd, *_args, **_kwargs):
        run_calls.append(cmd)
        # Simulate git refusing because it's not a registered worktree.
        import subprocess as _sp

        return _sp.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="not a worktree"
        )

    monkeypatch.setattr(worktrees, "_run_command", fake_run)

    counts = worktrees._cleanup_stale_loop_state(
        str(worktree_root), open_pr_numbers=set()
    )

    # The legit stale dir should be removed via rmtree fallback.
    assert not stale_dir.exists()
    # The symlink-out-of-root must NOT cause us to nuke the outside victim dir.
    assert sneaky_target.exists()
    assert (sneaky_target / "do-not-delete").exists()
    # Symlink entry itself either left in place or unlinked, but importantly
    # the target outside the root is untouched.
    assert counts["worktrees_removed"] >= 1


def test_cleanup_stale_loop_state_uses_git_remove_when_registered(
    monkeypatch, tmp_path
):
    import shutil as _shutil
    import subprocess as _sp

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    worktree_root = tmp_path / "wt"
    worktree_root.mkdir()
    monkeypatch.setattr(worktrees.tempfile, "gettempdir", lambda: str(tmp_root))

    stale_dir = worktree_root / "pr-77-feature"
    stale_dir.mkdir()

    captured = []

    def fake_run(cmd, *_args, **_kwargs):
        captured.append(cmd)
        # Pretend git succeeded and removed the directory.
        try:
            _shutil.rmtree(stale_dir)
        except FileNotFoundError:
            pass
        return _sp.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worktrees, "_run_command", fake_run)

    counts = worktrees._cleanup_stale_loop_state(
        str(worktree_root), open_pr_numbers=set()
    )

    assert counts["worktrees_removed"] == 1
    assert captured and captured[0][:3] == ["git", "worktree", "remove"]
    assert str(stale_dir) in captured[0]
    assert not stale_dir.exists()


def test_cleanup_stale_loop_state_keeps_open_pr_worktrees(monkeypatch, tmp_path):
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    worktree_root = tmp_path / "wt"
    worktree_root.mkdir()
    monkeypatch.setattr(worktrees.tempfile, "gettempdir", lambda: str(tmp_root))

    keep = worktree_root / "pr-50-still-open"
    keep.mkdir()
    drop = worktree_root / "pr-51-merged"
    drop.mkdir()

    def fake_run(cmd, *_args, **_kwargs):
        import subprocess as _sp

        return _sp.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="not registered"
        )

    monkeypatch.setattr(worktrees, "_run_command", fake_run)

    counts = worktrees._cleanup_stale_loop_state(
        str(worktree_root), open_pr_numbers={50}
    )

    assert counts["worktrees_removed"] == 1
    assert keep.exists()
    assert not drop.exists()


def test_check_wall_clock_raises_after_deadline(monkeypatch):
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 11)

    with pytest.raises(CommandError, match="Wall-clock timeout"):
        runtime._check_wall_clock(10)
    runtime._check_wall_clock(None)


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
        "_required_checks_for_ref",
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


def test_wait_for_checks_green_uses_pr_number_when_provided(monkeypatch, spy):
    required_checks = spy(return_value=([{"name": "unit", "bucket": "pass"}], True))
    monkeypatch.setattr(checks, "_required_checks_for_ref", required_checks)

    assert checks._wait_for_required_checks_green(
        branch="123",
        pr_number=77,
        poll_seconds=1,
        timeout_seconds=10,
    ) == (True, [{"name": "unit", "bucket": "pass"}])

    required_checks.assert_called_once_with("77")


def test_wait_for_checks_green_returns_success_after_no_checks_grace(monkeypatch, spy):
    monkeypatch.setattr(checks, "_required_checks_for_ref", lambda _branch: ([], True))
    monkeypatch.setattr(checks.time, "monotonic", spy_time([0, 1, 2, 11]))
    sleep = spy()
    monkeypatch.setattr(checks.time, "sleep", sleep)

    assert checks._wait_for_required_checks_green(
        branch="feature",
        poll_seconds=1,
        timeout_seconds=60,
        no_checks_grace_seconds=5,
    ) == (True, [])


@pytest.mark.parametrize(
    ("check_records", "were_required", "expected"),
    [
        (
            [{"name": "unit", "bucket": "pass"}, {"name": "skip", "bucket": "skipping"}],
            True,
            True,
        ),
        ([{"name": "unit", "bucket": "fail"}], True, False),
        ([{"name": "lint", "bucket": "fail"}], False, False),
    ],
)
def test_wait_for_checks_green_returns_terminal_check_states(
    monkeypatch, check_records, were_required, expected
):
    monkeypatch.setattr(
        checks,
        "_required_checks_for_ref",
        lambda _branch: (check_records, were_required),
    )

    assert checks._wait_for_required_checks_green(
        branch="feature",
        poll_seconds=1,
        timeout_seconds=10,
    ) == (expected, check_records)


def test_wait_for_checks_green_waits_for_pending_checks(monkeypatch, spy):
    pending = [{"name": "unit", "bucket": "pending"}]
    passing = [{"name": "unit", "bucket": "pass"}]
    monkeypatch.setattr(
        checks,
        "_required_checks_for_ref",
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


def test_wait_for_checks_green_accepts_passing_optional_checks_after_grace(
    monkeypatch, spy
):
    optional = [{"name": "lint", "bucket": "pass"}]
    monkeypatch.setattr(
        checks,
        "_required_checks_for_ref",
        spy(return_value=(optional, False)),
    )
    monkeypatch.setattr(checks.time, "monotonic", spy_time([0, 6]))
    sleep = spy()
    monkeypatch.setattr(checks.time, "sleep", sleep)

    assert checks._wait_for_required_checks_green(
        branch="feature",
        poll_seconds=3,
        timeout_seconds=10,
        no_checks_grace_seconds=5,
    ) == (True, optional)
    sleep.assert_not_called()


def test_wait_for_checks_green_waits_for_required_after_optional_checks_pass(
    monkeypatch, spy
):
    optional = [{"name": "lint", "bucket": "pass"}]
    required = [{"name": "unit", "bucket": "pass"}]
    required_checks = spy(side_effect=[(optional, False), (required, True)])
    monkeypatch.setattr(checks, "_required_checks_for_ref", required_checks)
    monkeypatch.setattr(checks.time, "monotonic", spy_time([0, 1, 1, 2]))
    sleep = spy()
    monkeypatch.setattr(checks.time, "sleep", sleep)

    assert checks._wait_for_required_checks_green(
        branch="feature",
        poll_seconds=3,
        timeout_seconds=10,
        no_checks_grace_seconds=5,
    ) == (True, required)
    sleep.assert_called_once_with(3)


def test_wait_for_checks_green_succeeds_on_optional_checks_after_waiting_grace(
    monkeypatch, spy
):
    optional = [{"name": "lint", "bucket": "pass"}]
    required_checks = spy(return_value=(optional, False))
    monkeypatch.setattr(
        checks,
        "_required_checks_for_ref",
        required_checks,
    )
    monkeypatch.setattr(checks.time, "monotonic", spy_time([0, 1, 1, 6]))
    sleep = spy()
    monkeypatch.setattr(checks.time, "sleep", sleep)

    assert checks._wait_for_required_checks_green(
        branch="feature",
        poll_seconds=2,
        timeout_seconds=10,
        no_checks_grace_seconds=5,
    ) == (True, optional)

    assert required_checks.call_count == 2
    sleep.assert_called_once_with(2)


def test_wait_for_checks_green_caps_sleep_to_remaining_timeout(monkeypatch, spy):
    pending = [{"name": "unit", "bucket": "pending"}]
    passing = [{"name": "unit", "bucket": "pass"}]
    monkeypatch.setattr(
        checks,
        "_required_checks_for_ref",
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
        "_required_checks_for_ref",
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
