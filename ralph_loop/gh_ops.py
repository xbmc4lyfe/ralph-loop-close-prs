"""GitHub CLI helpers."""
from __future__ import annotations

import json
import shlex
import subprocess
import time
from typing import Sequence, Tuple

from .config import NEEDS_REVIEW_LABEL, SSH_SIGNING_KEY
from .errors import CommandError
from .git_ops import _git_head_sha
from .process import (
    _print_step,
    _printable_cmd,
    _remaining_command_timeout,
    _run_command,
)

_GH_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "temporary failure",
    "503",
    "502",
    "504",
    "rate limit",
    "could not resolve host",
    "network is unreachable",
    "eof",
    "i/o timeout",
)


def _sleep_with_command_deadline(seconds: float, reason: str):
    remaining = _remaining_command_timeout(reason)
    delay = seconds if remaining is None else min(seconds, remaining)
    time.sleep(delay)


def _gh_run_with_retry(
    args: Sequence[str],
    *,
    check: bool,
    capture_output: bool,
    max_attempts: int = 3,
    base_delay: float = 2.0,
) -> subprocess.CompletedProcess:
    cmd = ["gh"] + list(args)
    last_completed = None
    for attempt in range(1, max_attempts + 1):
        completed = _run_command(
            cmd,
            check=False,
            capture_output=capture_output,
            max_output_bytes=None if capture_output else None,
        )
        last_completed = completed
        if completed.returncode == 0:
            return completed
        stderr_text = (completed.stderr or "").lower()
        is_transient = any(marker in stderr_text for marker in _GH_TRANSIENT_MARKERS)
        if is_transient and attempt < max_attempts:
            delay = min(base_delay * (2 ** (attempt - 1)), 30.0)
            _print_step(
                "Transient gh failure (attempt {}/{}); retrying in {}s".format(
                    attempt, max_attempts, delay
                )
            )
            _sleep_with_command_deadline(delay, "gh retry backoff")
            continue
        break
    if check and last_completed is not None and last_completed.returncode != 0:
        raise CommandError(
            "Command failed (exit={}): {}".format(
                last_completed.returncode, _printable_cmd(cmd)
            )
        )
    return last_completed


def _gh_json(args: Sequence[str]):
    completed = _gh_run_with_retry(args, check=True, capture_output=True)
    raw = (completed.stdout or "").strip()
    if not raw:
        raise CommandError(
            "gh command returned empty JSON output: gh {}".format(shlex.join(args))
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CommandError(
            "Failed to parse JSON from gh command: {}".format(exc)
        ) from exc


def _gh_json_allow_empty(
    args: Sequence[str],
    *,
    empty_error_text: str = "",
    pending_on_exit_8: bool = False,
):
    completed = _gh_run_with_retry(args, check=False, capture_output=True)
    raw = (completed.stdout or "").strip()
    stderr_text = (completed.stderr or "").strip()
    if completed.returncode == 0:
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(
                "Failed to parse JSON from gh command: {}".format(exc)
            ) from exc
    if completed.returncode == 8 and pending_on_exit_8:
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CommandError(
                    "Failed to parse JSON from gh command: {}".format(exc)
                ) from exc
        return [
            {
                "name": "GitHub checks",
                "bucket": "pending",
                "state": "PENDING",
                "link": "",
                "workflow": "",
            }
        ]
    empty_markers = ["no checks reported", "no required checks reported"]
    if empty_error_text:
        empty_markers.append(empty_error_text)
    combined_text = "{}\n{}".format(stderr_text, raw).lower()
    if any(marker and marker in combined_text for marker in empty_markers):
        return []
    raise CommandError(
        "gh command failed (exit={}): {}".format(
            completed.returncode, stderr_text or "<no stderr>"
        )
    )


def _active_gh_user() -> str:
    completed = _gh_run_with_retry(
        ["api", "user", "--jq", ".login"], check=True, capture_output=True
    )
    return (completed.stdout or "").strip()


_REVIEW_STATES_THAT_SET_APPROVAL = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")


def _review_commit_oid(review: dict) -> str:
    commit = review.get("commit")
    if isinstance(commit, dict):
        value = commit.get("oid") or commit.get("abbreviatedOid")
        if isinstance(value, str):
            return value
    if isinstance(commit, str):
        return commit
    for key in ("commitOid", "commitOID"):
        value = review.get(key)
        if isinstance(value, str):
            return value
    return ""


def _pr_has_user_approval(pr_ref: str, login: str) -> bool:
    pr_data = _gh_json(["pr", "view", pr_ref, "--json", "reviews,headRefOid"])
    head_oid = pr_data.get("headRefOid")
    if not isinstance(head_oid, str):
        head_oid = ""
    for review in reversed(pr_data.get("reviews") or []):
        author = (review.get("author") or {}).get("login")
        state = (review.get("state") or "").upper()
        if author != login or state not in _REVIEW_STATES_THAT_SET_APPROVAL:
            continue
        if state != "APPROVED":
            return False
        review_oid = _review_commit_oid(review)
        if head_oid and review_oid and review_oid != head_oid:
            return False
        return True
    return False


def _mark_pr_needs_review(pr_ref: str):
    _print_step("Marking PR {} as '{}'".format(pr_ref, NEEDS_REVIEW_LABEL))
    edit_result = _gh_run_with_retry(
        ["pr", "edit", pr_ref, "--add-label", NEEDS_REVIEW_LABEL],
        check=False,
        capture_output=True,
    )
    if edit_result.returncode == 0:
        return
    stderr = "{}\n{}".format(edit_result.stdout or "", edit_result.stderr or "").lower()
    if "not found" not in stderr:
        raise CommandError(
            "Failed to set '{}' label on PR {}.".format(NEEDS_REVIEW_LABEL, pr_ref)
        )
    _print_step("Creating missing label '{}'".format(NEEDS_REVIEW_LABEL))
    create_result = _gh_run_with_retry(
        [
            "label",
            "create",
            NEEDS_REVIEW_LABEL,
            "--color",
            "0E8A16",
            "--description",
            "Ready for maintainer review",
        ],
        check=False,
        capture_output=True,
    )
    if create_result.returncode != 0:
        create_stderr = "{}\n{}".format(
            create_result.stdout or "", create_result.stderr or ""
        ).lower()
        if "already exists" not in create_stderr:
            raise CommandError(
                "Failed to create label '{}': {}".format(
                    NEEDS_REVIEW_LABEL,
                    (create_result.stderr or create_result.stdout or "").strip(),
                )
            )
    _gh_run_with_retry(
        ["pr", "edit", pr_ref, "--add-label", NEEDS_REVIEW_LABEL],
        check=True,
        capture_output=True,
    )


def _sign_off_pr(pr_ref: str, head_sha: str = ""):
    gh_user = _active_gh_user()
    if _pr_has_user_approval(pr_ref, gh_user):
        _print_step("PR {} already approved by {}".format(pr_ref, gh_user))
        return
    _print_step("Submitting PR approval as {}".format(gh_user))
    if head_sha:
        result = _gh_run_with_retry(
                [
                    "api",
                    "repos/{{owner}}/{{repo}}/pulls/{}/reviews".format(pr_ref),
                "-f",
                "event=APPROVE",
                "-f",
                "commit_id={}".format(head_sha),
                "-f",
                "body=Automated sign-off before merge.",
            ],
            check=False,
            capture_output=True,
        )
    else:
        result = _gh_run_with_retry(
            [
                "pr",
                "review",
                pr_ref,
                "--approve",
                "--body",
                "Automated sign-off before merge.",
            ],
            check=False,
            capture_output=True,
        )
    if result.returncode == 0:
        return
    stderr = "{}\n{}".format(result.stdout or "", result.stderr or "").lower()
    if "already approved" in stderr:
        return
    raise CommandError(
        "Failed to approve PR {} as {}.".format(pr_ref, gh_user or "<unknown>")
    )


def _prepare_pr_for_merge(pr_ref: str):
    _mark_pr_needs_review(pr_ref)
    _run_command(["git", "config", "gpg.format", "ssh"], check=True, capture_output=True)
    _run_command(
        ["git", "config", "user.signingkey", SSH_SIGNING_KEY],
        check=True,
        capture_output=True,
    )
    _run_command(
        ["git", "config", "commit.gpgsign", "true"], check=True, capture_output=True
    )


def _pr_view(pr_ref: str) -> dict:
    data = _gh_json(
        [
            "pr",
            "view",
            pr_ref,
            "--json",
            "number,url,state,headRefName,baseRefName,isDraft,isCrossRepository",
        ]
    )
    if not isinstance(data, dict):
        raise CommandError(
            "Unexpected gh pr view response shape: expected object, got {}".format(
                type(data).__name__
            )
        )
    return data


def _pr_checks(branch: str, required_only: bool):
    args = ["pr", "checks"]
    if required_only:
        args.append("--required")
    args.extend([branch, "--json", "name,bucket,state,link,workflow"])
    return _gh_json_allow_empty(
        args,
        empty_error_text="no required checks reported",
        pending_on_exit_8=True,
    )


def _required_checks(branch: str) -> Tuple[list, bool]:
    checks = _pr_checks(branch, required_only=True)
    if checks:
        return checks, True
    return _pr_checks(branch, required_only=False), False


def _ensure_pr_head_matches_local(pr_ref: str, head_sha: str):
    pr_data = _gh_json(["pr", "view", pr_ref, "--json", "headRefOid"])
    remote_head = pr_data.get("headRefOid")
    if remote_head != head_sha:
        raise CommandError(
            "PR {} head is {} but local HEAD is {}; refusing to approve or merge stale code.".format(
                pr_ref, remote_head or "<unknown>", head_sha
            )
        )


def _merge_pr(pr_ref: str):
    head_sha = _git_head_sha()
    _ensure_pr_head_matches_local(pr_ref, head_sha)
    _sign_off_pr(pr_ref, head_sha=head_sha)
    _print_step("Merging PR with rebase strategy")
    _gh_run_with_retry(
        [
            "pr",
            "merge",
            pr_ref,
            "--rebase",
            "--delete-branch",
            "--match-head-commit",
            head_sha,
        ],
        check=True,
        capture_output=True,
    )
