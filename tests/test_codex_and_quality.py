import pytest

from ralph_loop import codex_agent, quality
from ralph_loop.errors import CommandError


def test_extract_marker_requires_one_exact_marker_line_and_handles_missing_values():
    assert (
        codex_agent._extract_yes_no_marker(
            marker_regex=r"REVIEW_PASS=(yes|no)",
            text="REVIEW_PASS=yes",
        )
        is True
    )
    assert (
        codex_agent._extract_yes_no_marker(
            marker_regex=r"REVIEW_PASS=(yes|no)",
            text="REVIEW_PASS=no\nlater\nREVIEW_PASS=yes",
        )
        is None
    )
    assert (
        codex_agent._extract_yes_no_marker(
            marker_regex=r"REVIEW_PASS=(yes|no)",
            text="I cannot return REVIEW_PASS=yes because issues remain",
        )
        is None
    )
    assert (
        codex_agent._extract_yes_no_marker(
            marker_regex=r"REVIEW_PASS=(yes|no)",
            text="OLD_REVIEW_PASS=yes",
        )
        is None
    )
    assert (
        codex_agent._extract_yes_no_marker(
            marker_regex=r"REVIEW_PASS=(yes|no)", text="nothing"
        )
        is None
    )
    assert (
        codex_agent._extract_yes_no_marker(
            marker_regex=r"(REVIEW_PASS)=(yes|no)", text="review_pass=NO"
        )
        is False
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("No findings.", True),
        ("No issues remain.", True),
        ("Actionable issues remain.", False),
        ("No findings were produced earlier, but actionable issues remain.", False),
        ("Needs more context.", None),
    ],
)
def test_review_pass_inference_handles_pass_fail_and_ambiguous_language(
    message, expected
):
    assert codex_agent._infer_review_pass_without_marker(message) is expected


def test_codex_exec_reads_last_message_and_passes_model_and_sandbox(
    monkeypatch, completed_process
):
    seen_commands = []

    def fake_run(cmd, check, capture_output):
        seen_commands.append(cmd)
        output_path = cmd[cmd.index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("REVIEW_PASS=yes\n")
        return completed_process()

    monkeypatch.setattr(codex_agent, "_run_command", fake_run)

    marker, message = codex_agent._codex_exec_with_marker(
        prompt="prompt",
        marker_regex=r"REVIEW_PASS=(yes|no)",
        model="gpt-test",
        sandbox="read-only",
    )

    assert marker is True
    assert message == "REVIEW_PASS=yes"
    assert "--model" in seen_commands[0]
    assert "gpt-test" in seen_commands[0]
    assert "read-only" in seen_commands[0]


def test_codex_exec_reads_last_message_with_size_bound(
    monkeypatch, completed_process
):
    def fake_run(cmd, check, capture_output, **_kwargs):
        output_path = cmd[cmd.index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("{}\nREVIEW_PASS=yes\n{}".format("A" * 40, "B" * 40))
        return completed_process()

    monkeypatch.setattr(codex_agent, "CODEX_LAST_MESSAGE_LIMIT", 30)
    monkeypatch.setattr(codex_agent, "_run_command", fake_run)

    marker, message = codex_agent._codex_exec_with_marker(
        prompt="prompt",
        marker_regex=r"REVIEW_PASS=(yes|no)",
        model=None,
    )

    assert marker is None
    assert "truncated" in message
    assert len(message) < 80


def test_codex_exec_raises_when_failed_run_has_no_last_message(
    monkeypatch, completed_process
):
    monkeypatch.setattr(
        codex_agent,
        "_run_command",
        lambda *_args, **_kwargs: completed_process(returncode=2, stderr="boom"),
    )

    with pytest.raises(CommandError, match="no partial last-message"):
        codex_agent._codex_exec_with_marker(
            prompt="prompt",
            marker_regex=r"REVIEW_PASS=(yes|no)",
            model=None,
        )


def test_codex_exec_raises_when_failed_run_has_partial_last_message(
    monkeypatch, completed_process
):
    def fake_run(cmd, check, capture_output):
        output_path = cmd[cmd.index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("REVIEW_PASS=no\n")
        return completed_process(returncode=2)

    monkeypatch.setattr(codex_agent, "_run_command", fake_run)

    with pytest.raises(CommandError) as raised:
        codex_agent._codex_exec_with_marker(
            prompt="prompt",
            marker_regex=r"REVIEW_PASS=(yes|no)",
            model=None,
        )

    message = str(raised.value)
    assert "codex exec failed (exit=2)" in message
    assert "partial last-message" in message
    assert "REVIEW_PASS=no" in message


def test_review_round_uses_marker_inference_and_error_paths(monkeypatch, spy):
    run = spy(return_value=(True, "REVIEW_PASS=yes"))
    monkeypatch.setattr(codex_agent, "_codex_exec_with_marker", run)

    passed, addressed = codex_agent._run_review_fix_round(2, "main", "model")
    assert passed is True
    assert addressed == []
    assert "/review" in run.call_args.kwargs["prompt"]
    assert "--base" not in run.call_args.kwargs["prompt"]
    assert "Do not commit or push." in run.call_args.kwargs["prompt"]

    monkeypatch.setattr(
        codex_agent,
        "_codex_exec_with_marker",
        spy(return_value=(None, "No findings.")),
    )
    passed, _ = codex_agent._run_review_fix_round(2, "main", None)
    assert passed is True

    monkeypatch.setattr(
        codex_agent,
        "_codex_exec_with_marker",
        spy(return_value=(None, "ambiguous")),
    )
    with pytest.raises(CommandError, match="REVIEW_PASS marker"):
        codex_agent._run_review_fix_round(2, "main", None)


def test_review_round_surfaces_external_comments_and_parses_addressed(
    monkeypatch, spy
):
    captured = {}

    def fake_exec(*, prompt, marker_regex, model):
        captured["prompt"] = prompt
        text = (
            "ADDRESSED_COMMENT_START=12345\n"
            "Wrapped the exception handler in _maybe_refresh_caps to redact\n"
            "any apikey=... query string before logging.\n"
            "Files: plugin.video.nzbdav/resources/lib/direct_indexers.py\n"
            "ADDRESSED_COMMENT_END\n"
            "ADDRESSED_COMMENT=67890: Match by preset_id when id is empty.\n"
            "REVIEW_PASS=yes\n"
        )
        return True, text

    monkeypatch.setattr(codex_agent, "_codex_exec_with_marker", fake_exec)
    external = [
        {
            "id": 12345,
            "user": "coderabbitai[bot]",
            "path": "foo.py",
            "line": 42,
            "body": "API key leaked",
        },
        {
            "id": 67890,
            "user": "codacy[bot]",
            "path": "bar.py",
            "line": 99,
            "body": "Duplicate row matcher",
        },
    ]

    passed, addressed = codex_agent._run_review_fix_round(
        1, "main", None, external_comments=external
    )

    assert passed is True
    assert len(addressed) == 2
    assert addressed[0][0] == 12345
    assert "redact" in addressed[0][1].lower()
    assert "_maybe_refresh_caps" in addressed[0][1]
    assert addressed[1] == (67890, "Match by preset_id when id is empty.")
    assert "COMMENT-12345" in captured["prompt"]
    assert "coderabbitai[bot]" in captured["prompt"]
    assert "ADDRESSED_COMMENT_START" in captured["prompt"]


def test_pre_push_review_gate_uses_marker_and_inference(monkeypatch, spy):
    run = spy(return_value=(False, "PRE_PUSH_REVIEW_OK=no"))
    monkeypatch.setattr(codex_agent, "_codex_exec_with_marker", run)

    assert codex_agent._run_pre_push_review_gate(base="main", model=None) is False
    assert run.call_args.kwargs["sandbox"] == "read-only"

    monkeypatch.setattr(
        codex_agent,
        "_codex_exec_with_marker",
        spy(return_value=(None, "No actionable issues remain.")),
    )
    assert codex_agent._run_pre_push_review_gate(base="main", model=None) is True

    monkeypatch.setattr(
        codex_agent,
        "_codex_exec_with_marker",
        spy(return_value=(None, "ambiguous")),
    )
    with pytest.raises(CommandError, match="PRE_PUSH_REVIEW_OK marker"):
        codex_agent._run_pre_push_review_gate(base="main", model=None)


def test_local_quality_and_ci_fix_rounds_require_markers(monkeypatch, spy):
    run = spy(return_value=(True, "LOCAL_QUALITY_FIX_READY=yes"))
    monkeypatch.setattr(codex_agent, "_codex_exec_with_marker", run)

    assert (
        codex_agent._run_local_quality_fix_round(
            round_number=1,
            failure_summary="just ci failed",
            model="m",
        )
        is True
    )
    assert "just ci failed" in run.call_args.kwargs["prompt"]

    monkeypatch.setattr(
        codex_agent,
        "_codex_exec_with_marker",
        spy(return_value=(None, "missing")),
    )
    with pytest.raises(CommandError, match="LOCAL_QUALITY_FIX_READY"):
        codex_agent._run_local_quality_fix_round(
            round_number=1,
            failure_summary="bad",
            model=None,
        )

    checks = [{"name": "lint", "bucket": "fail", "state": "FAILURE"}]
    run = spy(return_value=(False, "CI_FIX_READY=no"))
    monkeypatch.setattr(codex_agent, "_codex_exec_with_marker", run)
    assert (
        codex_agent._run_ci_fix_round(
            round_number=1,
            checks=checks,
            model=None,
        )
        is False
    )
    assert "lint" in run.call_args.kwargs["prompt"]

    monkeypatch.setattr(
        codex_agent,
        "_codex_exec_with_marker",
        spy(return_value=(None, "missing")),
    )
    with pytest.raises(CommandError, match="CI_FIX_READY"):
        codex_agent._run_ci_fix_round(round_number=1, checks=[], model=None)


def test_local_quality_gates_run_ci_then_test_and_report_first_failure(
    monkeypatch, spy, completed_process
):
    run = spy(side_effect=[completed_process(), completed_process()])
    monkeypatch.setattr(quality, "_run_command", run)

    assert quality._run_local_quality_gates() == (True, "")
    assert [call.args[0] for call in run.call_args_list] == [
        ["just", "ci"],
        ["just", "test"],
    ]

    monkeypatch.setattr(
        quality,
        "_run_command",
        lambda *_args, **_kwargs: completed_process(
            returncode=1,
            stdout="Authorization: Bearer super-secret-token\nbad\n",
        ),
    )
    ok, summary = quality._run_local_quality_gates()

    assert ok is False
    assert "Command `just ci` failed" in summary
    assert "bad" in summary
    assert "super-secret-token" not in summary
    assert "<redacted>" in summary


def test_commit_and_push_returns_no_changes_for_clean_tree_without_new_commits(
    monkeypatch
):
    monkeypatch.setattr(quality, "_git_head_sha", lambda: "abc")
    monkeypatch.setattr(quality, "_working_tree_dirty", lambda: False)

    assert (
        quality._commit_and_push(
            "review round 1",
            "feature",
            base="main",
            model=None,
            require_review_gate=False,
            review_gate_after_quality_fix=False,
            max_local_quality_rounds=0,
            pre_round_sha="abc",
        )
        == "no_changes"
    )


def test_commit_and_push_discards_when_review_gate_fails(monkeypatch, spy):
    reset = spy()
    monkeypatch.setattr(quality, "_git_head_sha", lambda: "def")
    monkeypatch.setattr(quality, "_working_tree_dirty", lambda: True)
    monkeypatch.setattr(quality, "_run_pre_push_review_gate", lambda **_kwargs: False)
    monkeypatch.setattr(quality, "_reset_generated_changes", reset)

    result = quality._commit_and_push(
        "ci round 1",
        "feature",
        base="main",
        model=None,
        require_review_gate=True,
        review_gate_after_quality_fix=False,
        max_local_quality_rounds=0,
        pre_round_sha="abc",
    )

    assert result == "discarded"
    reset.assert_called_once_with("abc")


def test_commit_and_push_stages_real_changes_without_generated_artifacts(
    monkeypatch, completed_process
):
    dirty_values = [True, True]
    commands = []

    def fake_run(cmd, check, capture_output):
        commands.append(cmd)
        if cmd == ["git", "diff", "--cached", "--quiet"]:
            return completed_process(returncode=1)
        if cmd[:4] == ["git", "ls-files", "--others", "--exclude-standard"]:
            return completed_process(
                stdout=(
                    "src/fix.py\0"
                    "tests/test_fix.py\0"
                    "docs/notes.md\0"
                    ".coverage\0"
                    "htmlcov/index.html\0"
                    ".ralph-logs/round.log\0"
                    "__pycache__/module.pyc\0"
                )
            )
        return completed_process()

    monkeypatch.setattr(quality, "_git_head_sha", lambda: "abc")
    monkeypatch.setattr(quality, "_working_tree_dirty", lambda: dirty_values.pop(0))
    monkeypatch.setattr(quality, "_run_local_quality_gates", lambda: (True, ""))
    monkeypatch.setattr(quality, "_run_command", fake_run)

    assert (
        quality._commit_and_push(
            "review round 1",
            "feature",
            base="main",
            model=None,
            require_review_gate=False,
            review_gate_after_quality_fix=False,
            max_local_quality_rounds=0,
            pre_round_sha=None,
        )
        == "committed"
    )
    staged_untracked = [cmd for cmd in commands if cmd[:3] == ["git", "add", "--"]][0]
    assert ["git", "add", "-A"] not in commands
    assert [
        "git",
        "add",
        "--",
        "src/fix.py",
        "tests/test_fix.py",
        "docs/notes.md",
    ] in commands
    assert ".coverage" not in staged_untracked
    assert "htmlcov/index.html" not in staged_untracked
    assert ".ralph-logs/round.log" not in staged_untracked
    assert "__pycache__/module.pyc" not in staged_untracked
    assert commands[-1] == ["git", "push", "origin", "feature"]


def test_commit_and_push_discards_existing_new_commit_without_new_worktree_changes(
    monkeypatch, spy, completed_process
):
    run = spy(return_value=completed_process())
    reset = spy()
    monkeypatch.setattr(quality, "_git_head_sha", lambda: "def")
    monkeypatch.setattr(
        quality,
        "_working_tree_dirty",
        spy(side_effect=[False, False]),
    )
    monkeypatch.setattr(quality, "_run_local_quality_gates", lambda: (True, ""))
    monkeypatch.setattr(quality, "_run_command", run)
    monkeypatch.setattr(quality, "_reset_generated_changes", reset)

    assert (
        quality._commit_and_push(
            "review round 1",
            "feature",
            base="main",
            model=None,
            require_review_gate=False,
            review_gate_after_quality_fix=False,
            max_local_quality_rounds=0,
            pre_round_sha="abc",
        )
        == "discarded"
    )
    reset.assert_called_once_with("abc")
    run.assert_not_called()


def test_commit_and_push_cleans_filtered_artifacts_before_no_changes(
    monkeypatch, spy, completed_process
):
    reset = spy()
    commands = []

    def fake_run(cmd, check, capture_output, **_kwargs):
        commands.append(cmd)
        if cmd == ["git", "diff", "--cached", "--quiet"]:
            return completed_process(returncode=0)
        if cmd[:4] == ["git", "ls-files", "--others", "--exclude-standard"]:
            return completed_process(stdout=".coverage\0htmlcov/index.html\0")
        return completed_process()

    monkeypatch.setattr(quality, "_git_head_sha", lambda: "abc")
    monkeypatch.setattr(quality, "_working_tree_dirty", lambda: True)
    monkeypatch.setattr(quality, "_run_local_quality_gates", lambda: (True, ""))
    monkeypatch.setattr(quality, "_run_command", fake_run)
    monkeypatch.setattr(quality, "_reset_generated_changes", reset)

    assert (
        quality._commit_and_push(
            "review round 1",
            "feature",
            base="main",
            model=None,
            require_review_gate=False,
            review_gate_after_quality_fix=False,
            max_local_quality_rounds=0,
            pre_round_sha="abc",
        )
        == "no_changes"
    )
    reset.assert_called_once_with("abc")
    assert ["git", "push", "origin", "feature"] not in commands


def test_commit_and_push_runs_quality_repair_and_reenables_review_gate(
    monkeypatch, spy, completed_process
):
    review = spy(return_value=True)
    repair = spy(return_value=True)
    monkeypatch.setattr(quality, "_git_head_sha", lambda: "def")
    monkeypatch.setattr(
        quality,
        "_working_tree_dirty",
        spy(side_effect=[True, True, True]),
    )
    monkeypatch.setattr(quality, "_run_pre_push_review_gate", review)
    monkeypatch.setattr(
        quality,
        "_run_local_quality_gates",
        spy(side_effect=[(False, "fail"), (True, "")]),
    )
    monkeypatch.setattr(quality, "_run_local_quality_fix_round", repair)
    monkeypatch.setattr(
        quality, "_run_command", lambda *_args, **_kwargs: completed_process()
    )

    assert (
        quality._commit_and_push(
            "ci round 1",
            "feature",
            base="main",
            model="m",
            require_review_gate=False,
            review_gate_after_quality_fix=True,
            max_local_quality_rounds=2,
            pre_round_sha="abc",
        )
        == "committed"
    )
    repair.assert_called_once_with(round_number=1, failure_summary="fail", model="m")
    review.assert_called_once_with(base="main", model="m")


def test_commit_and_push_discards_when_quality_repair_cannot_continue(
    monkeypatch, spy
):
    reset = spy()
    monkeypatch.setattr(quality, "_git_head_sha", lambda: "def")
    monkeypatch.setattr(quality, "_working_tree_dirty", lambda: True)
    monkeypatch.setattr(quality, "_run_local_quality_gates", lambda: (False, "fail"))
    monkeypatch.setattr(quality, "_run_local_quality_fix_round", lambda **_kwargs: False)
    monkeypatch.setattr(quality, "_reset_generated_changes", reset)

    assert (
        quality._commit_and_push(
            "review round 1",
            "feature",
            base="main",
            model=None,
            require_review_gate=False,
            review_gate_after_quality_fix=False,
            max_local_quality_rounds=0,
            pre_round_sha="abc",
        )
        == "discarded"
    )
    reset.assert_called_once_with("abc")


def test_commit_and_push_raises_after_quality_repair_rounds_are_exhausted(
    monkeypatch, spy
):
    monkeypatch.setattr(quality, "_git_head_sha", lambda: "def")
    monkeypatch.setattr(quality, "_working_tree_dirty", lambda: True)
    monkeypatch.setattr(
        quality,
        "_run_local_quality_gates",
        spy(side_effect=[(False, "fail"), (False, "still failing")]),
    )
    monkeypatch.setattr(quality, "_run_local_quality_fix_round", lambda **_kwargs: True)
    monkeypatch.setattr(quality, "_reset_generated_changes", spy())

    with pytest.raises(CommandError, match="exhausted 1 repair"):
        quality._commit_and_push(
            "review round 1",
            "feature",
            base="main",
            model=None,
            require_review_gate=False,
            review_gate_after_quality_fix=False,
            max_local_quality_rounds=1,
            pre_round_sha="abc",
        )
