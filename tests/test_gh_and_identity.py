import importlib

import pytest

from ralph_loop import config, gh_ops, identity
from ralph_loop.errors import CommandError


def _reload_config_and_identity():
    importlib.reload(config)
    importlib.reload(identity)


def test_gh_run_with_retry_retries_transient_stderr_then_succeeds(
    monkeypatch, spy, completed_process
):
    run = spy(
        side_effect=[
            completed_process(returncode=1, stderr="i/o timeout"),
            completed_process(stdout="ok\n"),
        ]
    )
    sleep = spy()
    monkeypatch.setattr(gh_ops, "_run_command", run)
    monkeypatch.setattr(gh_ops.time, "sleep", sleep)

    result = gh_ops._gh_run_with_retry(
        ["api", "user"],
        check=True,
        capture_output=True,
        base_delay=0.1,
    )

    assert result.stdout == "ok\n"
    assert run.call_count == 2
    sleep.assert_called_once_with(0.1)


def test_gh_run_with_retry_uses_unbounded_capture_and_deadline_aware_sleep(
    monkeypatch, spy, completed_process
):
    run = spy(
        side_effect=[
            completed_process(returncode=1, stderr="i/o timeout"),
            completed_process(stdout="ok\n"),
        ]
    )
    sleep = spy()
    monkeypatch.setattr(gh_ops, "_run_command", run)
    monkeypatch.setattr(gh_ops, "_sleep_with_command_deadline", sleep)

    gh_ops._gh_run_with_retry(
        ["api", "user"],
        check=True,
        capture_output=True,
        base_delay=0.1,
    )

    assert run.call_args_list[0].kwargs["max_output_bytes"] is None
    sleep.assert_called_once_with(0.1, "gh retry backoff")


def test_gh_run_with_retry_raises_for_checked_permanent_failure(
    monkeypatch, completed_process
):
    monkeypatch.setattr(
        gh_ops,
        "_run_command",
        lambda *_args, **_kwargs: completed_process(
            returncode=4, stderr="permission denied"
        ),
    )

    with pytest.raises(CommandError, match="Command failed"):
        gh_ops._gh_run_with_retry(["pr", "view"], check=True, capture_output=True)


def test_gh_run_with_retry_returns_last_failure_when_unchecked(
    monkeypatch, completed_process
):
    monkeypatch.setattr(
        gh_ops,
        "_run_command",
        lambda *_args, **_kwargs: completed_process(
            returncode=4, stderr="permission denied"
        ),
    )

    result = gh_ops._gh_run_with_retry(
        ["pr", "view"], check=False, capture_output=True
    )

    assert result.returncode == 4


def test_gh_json_requires_non_empty_valid_json(monkeypatch, completed_process):
    monkeypatch.setattr(
        gh_ops,
        "_gh_run_with_retry",
        lambda *_args, **_kwargs: completed_process(stdout='{"number": 12}\n'),
    )
    assert gh_ops._gh_json(["pr", "view"]) == {"number": 12}

    monkeypatch.setattr(
        gh_ops,
        "_gh_run_with_retry",
        lambda *_args, **_kwargs: completed_process(stdout=""),
    )
    with pytest.raises(CommandError, match="empty JSON"):
        gh_ops._gh_json(["pr", "view"])

    monkeypatch.setattr(
        gh_ops,
        "_gh_run_with_retry",
        lambda *_args, **_kwargs: completed_process(stdout="{"),
    )
    with pytest.raises(CommandError, match="Failed to parse JSON"):
        gh_ops._gh_json(["pr", "view"])


def test_gh_json_allow_empty_accepts_empty_success_and_textual_empty_errors(
    monkeypatch, completed_process
):
    monkeypatch.setattr(
        gh_ops,
        "_gh_run_with_retry",
        lambda *_args, **_kwargs: completed_process(stdout=""),
    )
    assert gh_ops._gh_json_allow_empty(["pr", "checks"]) == []

    monkeypatch.setattr(
        gh_ops,
        "_gh_run_with_retry",
        lambda *_args, **_kwargs: completed_process(
            returncode=1, stderr="no required checks reported"
        ),
    )
    assert (
        gh_ops._gh_json_allow_empty(
            ["pr", "checks"], empty_error_text="no required checks reported"
        )
        == []
    )

    monkeypatch.setattr(
        gh_ops,
        "_gh_run_with_retry",
        lambda *_args, **_kwargs: completed_process(
            returncode=1, stderr="no checks reported"
        ),
    )
    assert gh_ops._gh_json_allow_empty(["pr", "checks"]) == []


def test_pr_checks_maps_gh_pending_exit_to_pending_check(monkeypatch, completed_process):
    monkeypatch.setattr(
        gh_ops,
        "_gh_run_with_retry",
        lambda *_args, **_kwargs: completed_process(
            returncode=8, stdout="", stderr="checks pending"
        ),
    )

    assert gh_ops._pr_checks("feature", required_only=True) == [
        {
            "name": "GitHub checks",
            "bucket": "pending",
            "state": "PENDING",
            "link": "",
            "workflow": "",
        }
    ]


def test_gh_json_allow_empty_rejects_bad_json_and_unexpected_failure(
    monkeypatch, completed_process
):
    monkeypatch.setattr(
        gh_ops,
        "_gh_run_with_retry",
        lambda *_args, **_kwargs: completed_process(stdout="["),
    )
    with pytest.raises(CommandError, match="Failed to parse JSON"):
        gh_ops._gh_json_allow_empty(["pr", "checks"])

    monkeypatch.setattr(
        gh_ops,
        "_gh_run_with_retry",
        lambda *_args, **_kwargs: completed_process(returncode=2, stderr="boom"),
    )
    with pytest.raises(CommandError, match="gh command failed"):
        gh_ops._gh_json_allow_empty(["pr", "checks"])


def test_active_user_and_review_approval_helpers(monkeypatch, completed_process):
    monkeypatch.setattr(
        gh_ops,
        "_gh_run_with_retry",
        lambda *_args, **_kwargs: completed_process(stdout="xbmc4lyfe\n"),
    )
    assert gh_ops._active_gh_user() == "xbmc4lyfe"

    monkeypatch.setattr(
        gh_ops,
        "_gh_json",
        lambda _args: {
            "reviews": [
                {"author": {"login": "someone"}, "state": "APPROVED"},
                {"author": {"login": "me"}, "state": "commented"},
                {"author": {"login": "me"}, "state": "APPROVED"},
            ]
        },
    )
    assert gh_ops._pr_has_user_approval("7", "me") is True


@pytest.mark.parametrize("state", ["CHANGES_REQUESTED", "DISMISSED"])
def test_pr_has_user_approval_rejects_later_negative_review(monkeypatch, state):
    monkeypatch.setattr(
        gh_ops,
        "_gh_json",
        lambda _args: {
            "headRefOid": "head-sha",
            "reviews": [
                {
                    "author": {"login": "me"},
                    "state": "APPROVED",
                    "commit": {"oid": "head-sha"},
                },
                {
                    "author": {"login": "me"},
                    "state": state,
                    "commit": {"oid": "head-sha"},
                },
            ],
        },
    )

    assert gh_ops._pr_has_user_approval("7", "me") is False


def test_pr_has_user_approval_rejects_stale_head_approval(monkeypatch):
    monkeypatch.setattr(
        gh_ops,
        "_gh_json",
        lambda _args: {
            "headRefOid": "current-sha",
            "reviews": [
                {
                    "author": {"login": "me"},
                    "state": "APPROVED",
                    "commit": {"oid": "old-sha"},
                }
            ],
        },
    )

    assert gh_ops._pr_has_user_approval("7", "me") is False


def test_mark_pr_needs_review_uses_retry_wrapper_for_label_mutations(
    monkeypatch, spy, completed_process
):
    gh_run = spy(
        side_effect=[
            completed_process(returncode=1, stderr="label not found"),
            completed_process(),
            completed_process(),
        ]
    )
    direct_run = spy(side_effect=AssertionError("direct gh command was used"))
    monkeypatch.setattr(gh_ops, "_gh_run_with_retry", gh_run)
    monkeypatch.setattr(gh_ops, "_run_command", direct_run)

    gh_ops._mark_pr_needs_review("12")

    assert gh_run.call_count == 3
    assert gh_run.call_args_list[0].args[0] == [
        "pr",
        "edit",
        "12",
        "--add-label",
        gh_ops.NEEDS_REVIEW_LABEL,
    ]
    assert gh_run.call_args_list[1].args[0][:3] == [
        "label",
        "create",
        gh_ops.NEEDS_REVIEW_LABEL,
    ]
    assert gh_run.call_args_list[2].args[0] == [
        "pr",
        "edit",
        "12",
        "--add-label",
        gh_ops.NEEDS_REVIEW_LABEL,
    ]


@pytest.mark.parametrize(
    ("results", "expected_match"),
    [
        ([{"returncode": 1, "stderr": "permission denied"}], "Failed to set"),
        (
            [
                {"returncode": 1, "stderr": "label not found"},
                {"returncode": 1, "stderr": "cannot create"},
            ],
            "Failed to create label",
        ),
    ],
)
def test_mark_pr_needs_review_reports_label_failures(
    results, expected_match, monkeypatch, spy, completed_process
):
    gh_run = spy(
        side_effect=[
            completed_process(**result)
            for result in results
        ]
    )
    monkeypatch.setattr(gh_ops, "_gh_run_with_retry", gh_run)

    with pytest.raises(CommandError, match=expected_match):
        gh_ops._mark_pr_needs_review("12")


def test_sign_off_pr_uses_retry_wrapper_for_review_mutation(
    monkeypatch, spy, completed_process
):
    gh_run = spy(return_value=completed_process())
    direct_run = spy(side_effect=AssertionError("direct gh command was used"))
    monkeypatch.setattr(gh_ops, "_active_gh_user", lambda: "me")
    monkeypatch.setattr(gh_ops, "_pr_has_user_approval", lambda _pr, _user: False)
    monkeypatch.setattr(gh_ops, "_gh_run_with_retry", gh_run)
    monkeypatch.setattr(gh_ops, "_run_command", direct_run)

    gh_ops._sign_off_pr("9")

    gh_run.assert_called_once()
    assert gh_run.call_args.args[0][:4] == ["pr", "review", "9", "--approve"]


def test_sign_off_pr_can_approve_an_explicit_head_commit(
    monkeypatch, spy, completed_process
):
    gh_run = spy(return_value=completed_process())
    monkeypatch.setattr(gh_ops, "_active_gh_user", lambda: "me")
    monkeypatch.setattr(gh_ops, "_pr_has_user_approval", lambda _pr, _user: False)
    monkeypatch.setattr(gh_ops, "_gh_run_with_retry", gh_run)

    gh_ops._sign_off_pr("9", head_sha="abc123")

    gh_run.assert_called_once_with(
        [
            "api",
            "repos/{owner}/{repo}/pulls/9/reviews",
            "-f",
            "event=APPROVE",
            "-f",
            "commit_id=abc123",
            "-f",
            "body=Automated sign-off before merge.",
        ],
        check=False,
        capture_output=True,
    )


def test_sign_off_skips_existing_approval_and_accepts_already_approved_error(
    monkeypatch, spy, completed_process
):
    gh_run = spy()
    monkeypatch.setattr(gh_ops, "_active_gh_user", lambda: "me")
    monkeypatch.setattr(gh_ops, "_pr_has_user_approval", lambda _pr, _user: True)
    monkeypatch.setattr(gh_ops, "_gh_run_with_retry", gh_run)

    gh_ops._sign_off_pr("9")

    gh_run.assert_not_called()

    monkeypatch.setattr(gh_ops, "_pr_has_user_approval", lambda _pr, _user: False)
    monkeypatch.setattr(
        gh_ops,
        "_gh_run_with_retry",
        lambda *_args, **_kwargs: completed_process(
            returncode=1, stderr="Already approved"
        ),
    )
    gh_ops._sign_off_pr("9")


def test_sign_off_raises_for_review_failure(monkeypatch, completed_process):
    monkeypatch.setattr(gh_ops, "_active_gh_user", lambda: "me")
    monkeypatch.setattr(gh_ops, "_pr_has_user_approval", lambda _pr, _user: False)
    monkeypatch.setattr(
        gh_ops,
        "_gh_run_with_retry",
        lambda *_args, **_kwargs: completed_process(returncode=1, stderr="boom"),
    )

    with pytest.raises(CommandError, match="Failed to approve"):
        gh_ops._sign_off_pr("9")


def test_prepare_pr_for_merge_configures_signing_without_approving(
    monkeypatch, spy, completed_process
):
    mark = spy()
    sign = spy()
    run = spy(return_value=completed_process())
    monkeypatch.setattr(gh_ops, "_mark_pr_needs_review", mark)
    monkeypatch.setattr(gh_ops, "_sign_off_pr", sign)
    monkeypatch.setattr(gh_ops, "_run_command", run)

    gh_ops._prepare_pr_for_merge("8")

    mark.assert_called_once_with("8")
    sign.assert_not_called()
    assert run.call_count == 3


def test_pr_view_requests_fork_metadata_and_requires_object_shape(monkeypatch, spy):
    gh_json = spy(return_value={"number": 3})
    monkeypatch.setattr(gh_ops, "_gh_json", gh_json)

    assert gh_ops._pr_view("3") == {"number": 3}
    assert "isCrossRepository" in " ".join(gh_json.call_args.args[0])

    monkeypatch.setattr(gh_ops, "_gh_json", lambda _args: [])
    with pytest.raises(CommandError, match="expected object"):
        gh_ops._pr_view("3")


def test_list_open_prs_returns_numbers_filtering_drafts_only(monkeypatch, spy):
    gh_json = spy(
        return_value=[
            {"number": 107, "isDraft": False, "isCrossRepository": False},
            {"number": 105, "isDraft": False, "isCrossRepository": False},
            {"number": 94, "isDraft": False, "isCrossRepository": True},
            {"number": 90, "isDraft": True, "isCrossRepository": False},
        ]
    )
    monkeypatch.setattr(gh_ops, "_gh_json", gh_json)

    assert gh_ops._list_open_prs("main") == [107, 105, 94]
    call_args = gh_json.call_args.args[0]
    assert "--state" in call_args and "open" in call_args
    assert "--base" in call_args and "main" in call_args


def test_list_open_prs_returns_empty_list_when_no_prs(monkeypatch):
    monkeypatch.setattr(gh_ops, "_gh_json", lambda _args: [])
    assert gh_ops._list_open_prs("main") == []


def test_list_open_prs_skips_items_missing_or_non_int_number(monkeypatch):
    monkeypatch.setattr(
        gh_ops,
        "_gh_json",
        lambda _args: [
            {"isDraft": False, "isCrossRepository": False},
            {"number": "12", "isDraft": False, "isCrossRepository": False},
            {"number": 50, "isDraft": False, "isCrossRepository": False},
            "not a dict",
        ],
    )
    assert gh_ops._list_open_prs("main") == [50]


def test_list_open_prs_rejects_non_list_shape(monkeypatch):
    monkeypatch.setattr(gh_ops, "_gh_json", lambda _args: {"number": 12})
    with pytest.raises(CommandError, match="expected list"):
        gh_ops._list_open_prs("main")


def test_required_checks_falls_back_to_optional_checks(monkeypatch, spy):
    pr_checks = spy(side_effect=[[], [{"name": "unit", "bucket": "pass"}]])
    monkeypatch.setattr(gh_ops, "_pr_checks", pr_checks)

    assert gh_ops._required_checks("feature") == (
        [{"name": "unit", "bucket": "pass"}],
        False,
    )
    assert pr_checks.call_count == 2


def test_merge_pr_revalidates_remote_head_approves_explicit_commit_and_merges(
    monkeypatch, spy, completed_process
):
    sign = spy()
    ensure_head = spy()
    gh_run = spy(return_value=completed_process())
    direct_run = spy(side_effect=AssertionError("direct gh command was used"))
    monkeypatch.setattr(gh_ops, "_git_head_sha", lambda: "abc")
    monkeypatch.setattr(gh_ops, "_ensure_pr_head_matches_local", ensure_head)
    monkeypatch.setattr(gh_ops, "_sign_off_pr", sign)
    monkeypatch.setattr(gh_ops, "_gh_run_with_retry", gh_run)
    monkeypatch.setattr(gh_ops, "_run_command", direct_run)
    monkeypatch.setattr(
        gh_ops,
        "_pr_view",
        lambda _ref: {"headRefName": "feature", "isCrossRepository": False},
    )

    gh_ops._merge_pr("12")

    ensure_head.assert_called_once_with("12", "abc")
    sign.assert_called_once_with("12", head_sha="abc")
    merge_call, delete_call = gh_run.call_args_list[0], gh_run.call_args_list[1]
    assert merge_call.args[0] == [
        "pr",
        "merge",
        "12",
        "--rebase",
        "--match-head-commit",
        "abc",
    ]
    assert merge_call.kwargs["check"] is False
    assert delete_call.args[0] == [
        "api",
        "-X",
        "DELETE",
        "repos/{owner}/{repo}/git/refs/heads/feature",
    ]


def test_merge_pr_treats_already_merged_state_as_success(
    monkeypatch, spy, completed_process
):
    monkeypatch.setattr(gh_ops, "_git_head_sha", lambda: "abc")
    monkeypatch.setattr(gh_ops, "_ensure_pr_head_matches_local", lambda *a: None)
    monkeypatch.setattr(gh_ops, "_sign_off_pr", lambda *a, **k: None)
    pr_view_returns = iter(
        [
            {"state": "MERGED", "headRefName": "feature", "isCrossRepository": False},
            {"headRefName": "feature", "isCrossRepository": False},
        ]
    )
    monkeypatch.setattr(gh_ops, "_pr_view", lambda _ref: next(pr_view_returns))
    gh_run = spy(
        side_effect=[
            completed_process(returncode=1, stderr="local cleanup failed"),
            completed_process(returncode=0),
        ]
    )
    monkeypatch.setattr(gh_ops, "_gh_run_with_retry", gh_run)

    gh_ops._merge_pr("12")

    assert gh_run.call_count == 2


def test_merge_pr_skips_branch_delete_for_fork_prs(
    monkeypatch, spy, completed_process
):
    monkeypatch.setattr(gh_ops, "_git_head_sha", lambda: "abc")
    monkeypatch.setattr(gh_ops, "_ensure_pr_head_matches_local", lambda *a: None)
    monkeypatch.setattr(gh_ops, "_sign_off_pr", lambda *a, **k: None)
    monkeypatch.setattr(
        gh_ops,
        "_pr_view",
        lambda _ref: {"headRefName": "feature", "isCrossRepository": True},
    )
    gh_run = spy(return_value=completed_process(returncode=0))
    monkeypatch.setattr(gh_ops, "_gh_run_with_retry", gh_run)

    gh_ops._merge_pr("12")

    assert gh_run.call_count == 1


def test_truthy_and_ssh_public_key_detection():
    assert identity._is_truthy("YES") is True
    assert identity._is_truthy("no") is False
    assert identity._looks_like_ssh_public_key("ssh-ed25519 AAA") is True
    assert (
        identity._looks_like_ssh_public_key("sk-ssh-ed25519@openssh.com AAA") is True
    )
    assert identity._looks_like_ssh_public_key("not-a-key") is False


def test_validate_identity_and_signing_accepts_inline_public_key(monkeypatch):
    values = {
        "user.name": identity.GIT_NAME,
        "user.email": identity.GIT_EMAIL,
        "user.signingkey": "ssh-ed25519 AAA",
        "commit.gpgsign": "true",
    }
    monkeypatch.setattr(identity, "_active_gh_user", lambda: identity.GH_USER)
    monkeypatch.setattr(identity, "_git_config_get", lambda key: values[key])

    identity._validate_identity_and_signing()


def test_validate_identity_and_signing_reports_each_mismatch(monkeypatch):
    monkeypatch.setattr(identity, "_active_gh_user", lambda: "other")
    with pytest.raises(CommandError, match="Active gh user"):
        identity._validate_identity_and_signing()

    base_values = {
        "user.name": identity.GIT_NAME,
        "user.email": identity.GIT_EMAIL,
        "user.signingkey": "ssh-ed25519 AAA",
        "commit.gpgsign": "true",
    }
    cases = [
        ("user.name", "wrong", "git user.name"),
        ("user.email", "wrong@example.com", "git user.email"),
        ("user.signingkey", "", "signingkey is not set"),
        ("user.signingkey", "not-a-key", "neither a readable path"),
        ("commit.gpgsign", "false", "gpgsign must be enabled"),
    ]
    for key, value, message in cases:
        values = dict(base_values)
        values[key] = value
        monkeypatch.setattr(identity, "_active_gh_user", lambda: identity.GH_USER)
        monkeypatch.setattr(
            identity,
            "_git_config_get",
            lambda requested_key, values=values: values[requested_key],
        )
        with pytest.raises(CommandError, match=message):
            identity._validate_identity_and_signing()


def test_validate_identity_and_signing_checks_path_signing_key_exists(monkeypatch):
    values = {
        "user.name": identity.GIT_NAME,
        "user.email": identity.GIT_EMAIL,
        "user.signingkey": "/missing/signing.pub",
        "commit.gpgsign": "true",
    }
    monkeypatch.setattr(identity, "_active_gh_user", lambda: identity.GH_USER)
    monkeypatch.setattr(identity, "_git_config_get", lambda key: values[key])
    monkeypatch.setattr(identity.os.path, "exists", lambda _path: False)

    with pytest.raises(CommandError, match="does not exist"):
        identity._validate_identity_and_signing()


def test_ensure_runtime_identity_requires_key_files_and_expected_gh_user(monkeypatch):
    monkeypatch.setattr(identity.os.path, "exists", lambda _path: False)
    with pytest.raises(CommandError, match="auth key is missing"):
        identity._ensure_runtime_identity()

    monkeypatch.setattr(
        identity.os.path,
        "exists",
        lambda path: path == identity.SSH_AUTH_KEY,
    )
    with pytest.raises(CommandError, match="signing key is missing"):
        identity._ensure_runtime_identity()

    monkeypatch.setattr(identity.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(identity, "_active_gh_user", lambda: "other")
    with pytest.raises(CommandError, match="authenticated as"):
        identity._ensure_runtime_identity()


def test_ensure_runtime_identity_writes_all_git_config_values(
    monkeypatch, spy, completed_process
):
    def fake_run(cmd, *_args, **_kwargs):
        if cmd == ["git", "remote", "get-url", "origin"]:
            return completed_process(stdout="git@github.com:owner/repo.git\n")
        return completed_process()

    run = spy(side_effect=fake_run)
    monkeypatch.setattr(identity.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(identity, "_active_gh_user", lambda: identity.GH_USER)
    monkeypatch.setattr(identity, "_run_command", run)
    monkeypatch.setattr(identity, "_git_config_get", lambda _key: "")

    identity._ensure_runtime_identity()

    config_writes = [
        call.args[0]
        for call in run.call_args_list
        if call.args[0][:2] == ["git", "config"]
    ]
    assert len(config_writes) == 6
    assert ["git", "config", "user.name", identity.GIT_NAME] in config_writes
    assert ["git", "config", "commit.gpgsign", "true"] in config_writes


def test_set_git_config_if_changed_skips_when_value_matches(monkeypatch):
    monkeypatch.setattr(identity, "_git_config_get", lambda _key: "stable")
    monkeypatch.setattr(
        identity,
        "_run_command",
        lambda *_a, **_k: pytest.fail("should not write when value matches"),
    )
    identity._set_git_config_if_changed("user.name", "stable")


def test_set_git_config_if_changed_retries_on_lock_conflict(monkeypatch, spy):
    monkeypatch.setattr(identity, "_git_config_get", lambda _key: "old")
    monkeypatch.setattr(identity.time, "sleep", lambda _s: None)
    calls = []

    def flaky_run(cmd, *_a, **_k):
        calls.append(cmd)
        if len(calls) < 3:
            raise CommandError(
                "Command failed (exit=255): git config user.name new\n"
                "error: could not lock config file .git/config: File exists"
            )
        return None

    monkeypatch.setattr(identity, "_run_command", flaky_run)
    identity._set_git_config_if_changed("user.name", "new")
    assert len(calls) == 3


def test_set_git_config_if_changed_reraises_non_lock_errors(monkeypatch):
    monkeypatch.setattr(identity, "_git_config_get", lambda _key: "old")

    def boom(*_a, **_k):
        raise CommandError("Command failed (exit=128): permission denied")

    monkeypatch.setattr(identity, "_run_command", boom)
    with pytest.raises(CommandError, match="permission denied"):
        identity._set_git_config_if_changed("user.name", "new")


def test_ensure_runtime_identity_rejects_non_ssh_origin_remote(
    monkeypatch, completed_process
):
    def fake_run(cmd, *_args, **_kwargs):
        if cmd == ["git", "remote", "get-url", "origin"]:
            return completed_process(stdout="https://github.com/o/r.git\n")
        return completed_process()

    monkeypatch.setattr(identity.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(identity, "_active_gh_user", lambda: identity.GH_USER)
    monkeypatch.setattr(identity, "_run_command", fake_run)

    with pytest.raises(CommandError, match="origin remote must use SSH"):
        identity._ensure_runtime_identity()


def test_env_ssh_key_paths_and_command_expand_user_before_identity_setup(
    tmp_path, monkeypatch, spy, completed_process
):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    auth_key = ssh_dir / "auth-key"
    signing_key = ssh_dir / "signing-key.pub"
    auth_key.write_text("private", encoding="utf-8")
    signing_key.write_text("ssh-ed25519 AAA", encoding="utf-8")
    env = {
        "HOME": str(tmp_path),
        "RALPH_SSH_AUTH_KEY": "~/.ssh/auth-key",
        "RALPH_SSH_SIGNING_KEY": "~/.ssh/signing-key.pub",
        "RALPH_SSH_COMMAND": "ssh -i ~/.ssh/auth-key -o IdentityFile=~/.ssh/signing-key.pub",
    }

    with monkeypatch.context() as patched:
        for key, value in env.items():
            patched.setenv(key, value)
        _reload_config_and_identity()
        def fake_run(cmd, *_args, **_kwargs):
            if cmd == ["git", "remote", "get-url", "origin"]:
                return completed_process(stdout="git@github.com:owner/repo.git\n")
            return completed_process()

        run = spy(side_effect=fake_run)
        patched.setattr(identity, "_active_gh_user", lambda: identity.GH_USER)
        patched.setattr(identity, "_run_command", run)
        identity._ensure_runtime_identity()
    _reload_config_and_identity()

    commands = [call.args[0] for call in run.call_args_list]
    assert [
        "git",
        "config",
        "core.sshCommand",
        "ssh -i {} -o IdentityFile={}".format(auth_key, signing_key),
    ] in commands
    assert ["git", "config", "user.signingkey", str(signing_key)] in commands
