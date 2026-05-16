#!/usr/bin/env python3
"""Codex-driven review/fix loop with CI monitoring and merge automation."""
from __future__ import annotations

import sys

if sys.version_info < (3, 8):
    sys.stderr.write(
        "ERROR: Python 3.8+ is required (uses shlex.join); got {}.{}.\n".format(
            sys.version_info.major, sys.version_info.minor
        )
    )
    raise SystemExit(2)

import argparse
import datetime
import fcntl
import json
import os
import re
import shlex
import subprocess
import tempfile
import textwrap
import time
from typing import List, Optional, Sequence, Tuple

GH_USER = os.environ.get("RALPH_GH_USER", "xbmc4lyfe")
GIT_NAME = os.environ.get("RALPH_GIT_NAME", GH_USER)
GIT_EMAIL = os.environ.get(
    "RALPH_GIT_EMAIL", "{}@users.noreply.github.com".format(GH_USER)
)
SSH_AUTH_KEY = os.environ.get(
    "RALPH_SSH_AUTH_KEY",
    os.path.expanduser("~/.ssh/id_ed25519_{}".format(GH_USER)),
)
SSH_SIGNING_KEY = os.environ.get(
    "RALPH_SSH_SIGNING_KEY",
    os.path.expanduser("~/.ssh/id_ed25519_signing.pub"),
)
SSH_COMMAND = os.environ.get(
    "RALPH_SSH_COMMAND",
    "ssh -i {} -o IdentitiesOnly=yes -o IdentityAgent=none".format(SSH_AUTH_KEY),
)
COAUTHOR_LINE = os.environ.get(
    "RALPH_COAUTHOR_LINE", "Co-Authored-By: Oz <oz-agent@warp.dev>"
)
NEEDS_REVIEW_LABEL = "needs review"
LOOP_ALREADY_RUNNING_MESSAGE = "found another ralph loop already for this PR"
DEFAULT_WORKTREE_ROOT = os.environ.get(
    "RALPH_WORKTREE_ROOT",
    os.path.join(tempfile.gettempdir(), "codex-ralph-worktrees"),
)
QUALITY_GATE_OUTPUT_LIMIT = 12000


class CommandError(RuntimeError):
    """Raised when a subprocess command fails."""


def _print_step(message: str):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    sys.stderr.write("\n[{}] ==> {}\n".format(timestamp, message))
    sys.stderr.flush()


def _printable_cmd(cmd: Sequence[str], *, max_arg_len: int = 4000) -> str:
    parts = []
    for arg in cmd:
        if len(arg) > max_arg_len:
            parts.append(
                "{}...<+{} chars>".format(arg[:max_arg_len], len(arg) - max_arg_len)
            )
        else:
            parts.append(arg)
    return shlex.join(parts)


def _run_command(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture_output: bool = True,
    cwd: Optional[str] = None,
) -> subprocess.CompletedProcess:
    printable = _printable_cmd(cmd)
    sys.stderr.write("$ {}\n".format(printable))
    sys.stderr.flush()
    completed = subprocess.run(  # nosec B603
        list(cmd),
        cwd=cwd,
        text=True,
        capture_output=capture_output,
        check=False,
    )
    if capture_output:
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
    if check and completed.returncode != 0:
        raise CommandError(
            "Command failed (exit={}): {}".format(completed.returncode, printable)
        )
    return completed


def _truncate_for_log(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - limit
    return "{}...<truncated {} chars>...{}".format(
        text[:head], omitted, text[-tail:]
    )


def _completed_process_output(completed: subprocess.CompletedProcess) -> str:
    parts = []
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if stdout and stderr and stdout == stderr:
        return "stdout+stderr:\n{}".format(stdout)
    if stdout:
        parts.append("stdout:\n{}".format(stdout))
    if stderr:
        parts.append("stderr:\n{}".format(stderr))
    return "\n\n".join(parts).strip()


def _git_output(args: Sequence[str]) -> str:
    completed = _run_command(["git"] + list(args), check=True, capture_output=True)
    return (completed.stdout or "").strip()


def _git_config_get(key: str) -> str:
    completed = _run_command(
        ["git", "config", "--get", key], check=False, capture_output=True
    )
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def _active_gh_user() -> str:
    completed = _run_command(
        ["gh", "api", "user", "--jq", ".login"], check=True, capture_output=True
    )
    return (completed.stdout or "").strip()


def _is_truthy(value: str) -> bool:
    return value.lower() in ("1", "true", "yes", "on")


def _looks_like_ssh_public_key(text: str) -> bool:
    head = text.strip().split()
    return bool(head) and head[0].startswith(("ssh-", "ecdsa-", "sk-ssh-", "sk-ecdsa-"))


def _validate_identity_and_signing():
    _print_step("Validating GitHub/git identity and signing configuration")
    gh_user = _active_gh_user()
    if gh_user != GH_USER:
        raise CommandError(
            "Active gh user is '{}' (expected '{}').".format(
                gh_user or "<empty>", GH_USER
            )
        )
    git_user = _git_config_get("user.name")
    if git_user != GIT_NAME:
        raise CommandError(
            "git user.name is '{}' (expected '{}').".format(
                git_user or "<empty>", GIT_NAME
            )
        )
    git_email = _git_config_get("user.email")
    if git_email != GIT_EMAIL:
        raise CommandError(
            "git user.email is '{}' (expected '{}').".format(
                git_email or "<empty>", GIT_EMAIL
            )
        )
    signing_key = _git_config_get("user.signingkey")
    if not signing_key:
        raise CommandError("git user.signingkey is not set.")
    if signing_key.startswith(("/", "~")):
        signing_key_path = os.path.expanduser(signing_key)
        if not os.path.exists(signing_key_path):
            raise CommandError(
                "Configured signing key path does not exist: {}".format(
                    signing_key_path
                )
            )
    elif not _looks_like_ssh_public_key(signing_key):
        raise CommandError(
            "git user.signingkey is set but is neither a readable path nor an "
            "SSH public key body: {!r}".format(signing_key[:80])
        )
    if not _is_truthy(_git_config_get("commit.gpgsign")):
        raise CommandError(
            "git commit.gpgsign must be enabled to ensure signed commits."
        )


def _gh_json(args: Sequence[str]):
    completed = _run_command(["gh"] + list(args), check=True, capture_output=True)
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


def _gh_json_allow_empty(args: Sequence[str], *, empty_error_text: str = ""):
    completed = _run_command(["gh"] + list(args), check=False, capture_output=True)
    raw = (completed.stdout or "").strip()
    stderr_text = (completed.stderr or "").strip()
    if completed.returncode in (0, 8):
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(
                "Failed to parse JSON from gh command: {}".format(exc)
            ) from exc
    if empty_error_text and (
        empty_error_text in stderr_text or empty_error_text in raw
    ):
        return []
    raise CommandError(
        "gh command failed (exit={}): {}".format(
            completed.returncode, stderr_text or "<no stderr>"
        )
    )


def _ensure_runtime_identity():
    if not os.path.exists(SSH_AUTH_KEY):
        raise CommandError(
            "Required SSH auth key is missing: {}".format(SSH_AUTH_KEY)
        )
    if not os.path.exists(SSH_SIGNING_KEY):
        raise CommandError(
            "Required SSH signing key is missing: {}".format(SSH_SIGNING_KEY)
        )
    _print_step("Ensuring GitHub login is '{}'".format(GH_USER))
    gh_login = _active_gh_user()
    if gh_login != GH_USER:
        raise CommandError(
            "gh is authenticated as '{}' instead of '{}'.".format(gh_login, GH_USER)
        )
    _print_step(
        "Setting git identity and SSH/signing keys for '{}'".format(GH_USER)
    )
    _run_command(
        ["git", "config", "user.name", GIT_NAME],
        check=True,
        capture_output=True,
    )
    _run_command(
        ["git", "config", "user.email", GIT_EMAIL],
        check=True,
        capture_output=True,
    )
    _run_command(
        ["git", "config", "core.sshCommand", SSH_COMMAND],
        check=True,
        capture_output=True,
    )
    _run_command(
        ["git", "config", "gpg.format", "ssh"],
        check=True,
        capture_output=True,
    )
    _run_command(
        ["git", "config", "user.signingkey", SSH_SIGNING_KEY],
        check=True,
        capture_output=True,
    )
    _run_command(
        ["git", "config", "commit.gpgsign", "true"],
        check=True,
        capture_output=True,
    )


def _pr_has_user_approval(pr_ref: str, login: str) -> bool:
    pr_data = _gh_json(["pr", "view", pr_ref, "--json", "reviews"])
    for review in pr_data.get("reviews", []):
        author = (review.get("author") or {}).get("login")
        state = (review.get("state") or "").upper()
        if author == login and state == "APPROVED":
            return True
    return False


def _mark_pr_needs_review(pr_ref: str):
    _print_step("Marking PR {} as '{}'".format(pr_ref, NEEDS_REVIEW_LABEL))
    edit_result = _run_command(
        ["gh", "pr", "edit", pr_ref, "--add-label", NEEDS_REVIEW_LABEL],
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
    create_result = _run_command(
        [
            "gh",
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
    _run_command(
        ["gh", "pr", "edit", pr_ref, "--add-label", NEEDS_REVIEW_LABEL],
        check=True,
        capture_output=True,
    )


def _sign_off_pr(pr_ref: str):
    gh_user = _active_gh_user()
    if _pr_has_user_approval(pr_ref, gh_user):
        _print_step("PR {} already approved by {}".format(pr_ref, gh_user))
        return
    _print_step("Submitting PR approval as {}".format(gh_user))
    result = _run_command(
        [
            "gh",
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
    _sign_off_pr(pr_ref)
    _run_command(["git", "config", "gpg.format", "ssh"], check=True, capture_output=True)
    _run_command(
        ["git", "config", "user.signingkey", SSH_SIGNING_KEY],
        check=True,
        capture_output=True,
    )
    _run_command(
        ["git", "config", "commit.gpgsign", "true"], check=True, capture_output=True
    )


def _git_branch() -> str:
    branch = _git_output(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch == "HEAD":
        raise CommandError("Detached HEAD is not supported for this workflow.")
    return branch


def _git_head_sha() -> str:
    return _git_output(["rev-parse", "HEAD"])


def _working_tree_dirty() -> bool:
    return bool(_git_output(["status", "--porcelain"]))


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return slug or "unknown"


class _LoopLock:
    """Advisory lock keyed by PR number (or branch fallback)."""

    def __init__(self, handle, path: str):
        self.handle = handle
        self.path = path

    def release(self):
        try:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        finally:
            try:
                self.handle.close()
            except OSError:
                pass
            try:
                os.unlink(self.path)
            except OSError:
                pass


def _acquire_loop_lock(
    *, pr_number: Optional[int], branch_fallback: Optional[str] = None
) -> Optional["_LoopLock"]:
    if pr_number is not None:
        key = "pr-{}".format(pr_number)
    elif branch_fallback:
        key = "branch-{}".format(_slug(branch_fallback))
    else:
        return None
    lock_path = os.path.join(
        tempfile.gettempdir(), "codex-ralph-loop-{}.lock".format(key)
    )
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    handle = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.stderr.write("{}\n".format(LOOP_ALREADY_RUNNING_MESSAGE))
            sys.stderr.flush()
            handle.close()
            raise SystemExit(0)
        handle.seek(0)
        handle.truncate(0)
        handle.write("{}\n".format(os.getpid()))
        handle.flush()
    except SystemExit:
        raise
    except BaseException:
        try:
            handle.close()
        except OSError:
            pass
        raise
    return _LoopLock(handle, lock_path)


def _worktree_path(*, worktree_root: str, pr_number: int, branch: str) -> str:
    return os.path.join(
        worktree_root,
        "pr-{}-{}".format(pr_number, _slug(branch)),
    )


def _pr_head_fetch_ref(pr_number: int) -> str:
    return "refs/remotes/origin/pr-{}-head".format(pr_number)


def _fetch_pr_branch_or_head(
    *, pr_number: int, branch: str, cwd: Optional[str] = None
) -> str:
    fetch_branch = _run_command(
        ["git", "fetch", "origin", branch],
        check=False,
        capture_output=True,
        cwd=cwd,
    )
    if fetch_branch.returncode == 0:
        return "origin/{}".format(branch)

    _print_step(
        "Origin branch '{}' was not fetchable; fetching PR #{} head ref".format(
            branch, pr_number
        )
    )
    pr_head_ref = _pr_head_fetch_ref(pr_number)
    _run_command(
        [
            "git",
            "fetch",
            "origin",
            "+refs/pull/{}/head:{}".format(pr_number, pr_head_ref),
        ],
        check=True,
        capture_output=True,
        cwd=cwd,
    )
    return pr_head_ref


def _worktree_for_branch(branch: str) -> Optional[str]:
    completed = _run_command(
        ["git", "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
    )
    current_path = None
    expected_branch_ref = "refs/heads/{}".format(branch)
    for line in (completed.stdout or "").splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
            continue
        if line == "branch {}".format(expected_branch_ref) and current_path:
            return current_path
    return None


def _ensure_pr_worktree(
    *,
    worktree_root: str,
    pr_number: int,
    branch: str,
) -> str:
    os.makedirs(worktree_root, exist_ok=True)
    path = _worktree_path(
        worktree_root=worktree_root,
        pr_number=pr_number,
        branch=branch,
    )
    start_ref = _fetch_pr_branch_or_head(pr_number=pr_number, branch=branch)
    existing_branch_worktree = _worktree_for_branch(branch)
    if existing_branch_worktree and os.path.abspath(
        existing_branch_worktree
    ) != os.path.abspath(path):
        _print_step(
            "PR branch '{}' is already checked out at {}; reusing it".format(
                branch, existing_branch_worktree
            )
        )
        _fetch_pr_branch_or_head(
            pr_number=pr_number,
            branch=branch,
            cwd=existing_branch_worktree,
        )
        return existing_branch_worktree
    if os.path.isdir(path):
        _print_step("Using existing PR worktree {}".format(path))
        worktree_branch = _run_command(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
        )
        if (worktree_branch.stdout or "").strip() != branch:
            raise CommandError(
                "Existing worktree {} is on branch '{}' instead of '{}'.".format(
                    path,
                    (worktree_branch.stdout or "").strip() or "<unknown>",
                    branch,
                )
            )
        _fetch_pr_branch_or_head(pr_number=pr_number, branch=branch, cwd=path)
        return path
    _print_step("Creating PR worktree {}".format(path))
    result = _run_command(
        ["git", "worktree", "add", path, "-B", branch, start_ref],
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return path
    stderr = "{}\n{}".format(result.stdout or "", result.stderr or "").lower()
    if "already used by worktree" in stderr:
        sys.stdout.write("{}\n".format(LOOP_ALREADY_RUNNING_MESSAGE))
        sys.stdout.flush()
        raise SystemExit(0)
    raise CommandError(
        "Unable to create worktree for branch '{}': {}".format(
            branch,
            (result.stderr or result.stdout or "").strip(),
        )
    )


def _checkout_branch(branch: str):
    current_branch = _git_branch()
    if current_branch == branch:
        return
    _print_step("Switching to PR branch {}".format(branch))
    _run_command(["git", "fetch", "origin", branch], check=True, capture_output=True)
    has_local_branch = bool(_git_output(["branch", "--list", branch]))
    if has_local_branch:
        checkout_result = _run_command(
            ["git", "checkout", branch], check=False, capture_output=True
        )
    else:
        checkout_result = _run_command(
            ["git", "checkout", "-b", branch, "--track", "origin/{}".format(branch)],
            check=False,
            capture_output=True,
        )
    if checkout_result.returncode == 0:
        return
    stderr = "{}\n{}".format(
        checkout_result.stdout or "", checkout_result.stderr or ""
    ).lower()
    if "already used by worktree" in stderr:
        sys.stdout.write("{}\n".format(LOOP_ALREADY_RUNNING_MESSAGE))
        sys.stdout.flush()
        raise SystemExit(0)
    raise CommandError(
        "Unable to checkout branch '{}': {}".format(
            branch, (checkout_result.stderr or checkout_result.stdout or "").strip()
        )
    )


def _run_local_quality_gates() -> Tuple[bool, str]:
    _print_step("Running local quality gates before commit/push (just ci + just test)")
    for recipe in ("ci", "test"):
        result = _run_command(["just", recipe], check=False, capture_output=True)
        if result.returncode != 0:
            output = _completed_process_output(result) or "<no output>"
            failure_summary = "Command `just {}` failed with exit code {}.\n{}".format(
                recipe,
                result.returncode,
                output,
            )
            _print_step(
                "just {} failed; starting a local quality repair round.".format(
                    recipe
                )
            )
            return False, _truncate_for_log(
                failure_summary,
                QUALITY_GATE_OUTPUT_LIMIT,
            )
    return True, ""


def _reset_generated_changes(target_sha: Optional[str] = None):
    target = target_sha or "HEAD"
    head_sha = _git_head_sha()
    dirty = _working_tree_dirty()
    if not dirty and target_sha and head_sha == target_sha:
        return
    if not dirty and not target_sha:
        return
    _print_step(
        "Resetting generated changes (git reset --hard {} + git clean -fd)".format(
            target
        )
    )
    _run_command(["git", "reset", "--hard", target], check=True, capture_output=True)
    _run_command(["git", "clean", "-fd"], check=True, capture_output=True)


def _extract_yes_no_marker(*, marker_regex: str, text: str) -> Optional[bool]:
    matches = re.findall(marker_regex, text, flags=re.IGNORECASE)
    if not matches:
        return None
    last = matches[-1]
    if isinstance(last, tuple):
        last = next((g for g in last if g), "")
    return last.lower() == "yes"


def _codex_exec_with_marker(
    *,
    prompt: str,
    marker_regex: str,
    model: Optional[str],
    sandbox: str = "danger-full-access",
) -> Tuple[Optional[bool], str]:
    with tempfile.TemporaryDirectory(prefix="codex-last-msg-") as tmp_dir:
        temp_path = os.path.join(tmp_dir, "out.txt")
        cmd: List[str] = [
            "codex",
            "--ask-for-approval",
            "never",
            "--sandbox",
            sandbox,
            "exec",
            "-o",
            temp_path,
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.append(prompt)
        _run_command(cmd, check=True, capture_output=True)
        try:
            with open(temp_path, "r", encoding="utf-8") as handle:
                last_message = handle.read().strip()
        except FileNotFoundError:
            last_message = ""
    marker_value = _extract_yes_no_marker(
        marker_regex=marker_regex, text=last_message
    )
    return marker_value, last_message


def _infer_review_pass_without_marker(last_message: str) -> Optional[bool]:
    text = last_message.lower()
    if re.search(r"\b(no findings|no actionable issues remain|no issues found)\b", text):
        return True
    if re.search(r"\b(actionable issues remain|findings remain|issues remain)\b", text):
        return False
    return None


def _run_review_fix_round(round_number: int, base: str, model: Optional[str]) -> bool:
    _print_step("Codex review/fix round {}".format(round_number))
    prompt = textwrap.dedent(
        """
        Run `/review --base {base}`.
        If `/review` finds actionable issues, fix them in the current repository.
        Then run `/review --base {base}` exactly one more time.
        If no actionable issues remain after that second review, return:
        REVIEW_PASS=yes
        Otherwise return:
        REVIEW_PASS=no
        Respond with exactly one line and nothing else.
        """
    ).strip().format(base=base)
    marker_value, last_message = _codex_exec_with_marker(
        prompt=prompt,
        marker_regex=r"REVIEW_PASS=(yes|no)",
        model=model,
    )
    _print_step(
        "Codex marker output: {}".format(
            _truncate_for_log(last_message or "<empty>")
        )
    )
    if marker_value is not None:
        return marker_value
    inferred = _infer_review_pass_without_marker(last_message)
    if inferred is None:
        raise CommandError(
            "Codex did not return REVIEW_PASS marker and pass/fail could not be inferred."
        )
    _print_step(
        "REVIEW_PASS marker missing; inferred REVIEW_PASS={} from Codex text.".format(
            "yes" if inferred else "no"
        )
    )
    return inferred


def _run_pre_push_review_gate(*, base: str, model: Optional[str]) -> bool:
    _print_step("Running Codex /review gate before push")
    prompt = textwrap.dedent(
        """
        Run `/review --base {base}` exactly once and do not modify files.
        If no actionable issues remain, return:
        PRE_PUSH_REVIEW_OK=yes
        Otherwise return:
        PRE_PUSH_REVIEW_OK=no
        Respond with exactly one line and nothing else.
        """
    ).strip().format(base=base)
    marker_value, last_message = _codex_exec_with_marker(
        prompt=prompt,
        marker_regex=r"PRE_PUSH_REVIEW_OK=(yes|no)",
        model=model,
    )
    _print_step(
        "Codex marker output: {}".format(_truncate_for_log(last_message or "<empty>"))
    )
    if marker_value is not None:
        return marker_value
    inferred = _infer_review_pass_without_marker(last_message)
    if inferred is None:
        raise CommandError(
            "Codex did not return PRE_PUSH_REVIEW_OK marker and pass/fail could not be inferred."
        )
    _print_step(
        "PRE_PUSH_REVIEW_OK marker missing; inferred PRE_PUSH_REVIEW_OK={} from Codex text.".format(
            "yes" if inferred else "no"
        )
    )
    return inferred


def _run_local_quality_fix_round(
    *,
    round_number: int,
    failure_summary: str,
    model: Optional[str],
) -> bool:
    _print_step("Codex local quality repair round {}".format(round_number))
    prompt = textwrap.dedent(
        """
        Local quality gates failed before commit/push.
        Failure output:
        {failure_summary}

        Diagnose the failure, fix the underlying code or test issue in this
        repository, and run the relevant local verification. Preserve the
        intended PR changes. Do not commit or push.
        Return exactly one line:
        LOCAL_QUALITY_FIX_READY=yes
        if you made a concrete fix and are ready to retry commit/push, otherwise:
        LOCAL_QUALITY_FIX_READY=no
        """
    ).strip().format(failure_summary=failure_summary)
    marker_value, last_message = _codex_exec_with_marker(
        prompt=prompt,
        marker_regex=r"LOCAL_QUALITY_FIX_READY=(yes|no)",
        model=model,
    )
    _print_step(
        "Codex marker output: {}".format(
            _truncate_for_log(last_message or "<empty>")
        )
    )
    if marker_value is None:
        raise CommandError("Codex did not return LOCAL_QUALITY_FIX_READY marker.")
    return marker_value


def _round_numbers(max_rounds: int):
    if max_rounds <= 0:
        round_number = 1
        while True:
            yield round_number
            round_number += 1
    else:
        for round_number in range(1, max_rounds + 1):
            yield round_number


def _commit_and_push(
    iteration_label: str,
    branch: str,
    *,
    base: str,
    model: Optional[str],
    require_review_gate: bool,
    review_gate_after_quality_fix: bool,
    max_local_quality_rounds: int,
    pre_round_sha: Optional[str] = None,
) -> str:
    local_quality_round = 0
    review_gate_needed = require_review_gate
    while True:
        head_sha = _git_head_sha()
        has_new_commits = bool(pre_round_sha) and head_sha != pre_round_sha
        if not _working_tree_dirty() and not has_new_commits:
            _print_step("No changes to commit.")
            return "no_changes"
        if review_gate_needed and not _run_pre_push_review_gate(
            base=base,
            model=model,
        ):
            _print_step(
                "Pre-push review found actionable issues; discarding generated changes."
            )
            _reset_generated_changes(pre_round_sha)
            return "discarded"
        gates_ok, failure_summary = _run_local_quality_gates()
        if gates_ok:
            break
        if (
            max_local_quality_rounds > 0
            and local_quality_round >= max_local_quality_rounds
        ):
            _reset_generated_changes(pre_round_sha)
            raise CommandError(
                "Local quality loop exhausted {} repair rounds during {}.".format(
                    max_local_quality_rounds,
                    iteration_label,
                )
            )
        local_quality_round += 1
        ready = _run_local_quality_fix_round(
            round_number=local_quality_round,
            failure_summary=failure_summary,
            model=model,
        )
        if not ready:
            _print_step(
                "Local quality repair round {} did not produce a useful fix; discarding generated changes.".format(
                    local_quality_round
                )
            )
            _reset_generated_changes(pre_round_sha)
            return "discarded"
        if review_gate_after_quality_fix:
            review_gate_needed = True
        _print_step(
            "Retrying commit/push after local quality repair round {}".format(
                local_quality_round
            )
        )
    if _working_tree_dirty():
        _print_step("Committing Codex-generated changes")
        _run_command(["git", "add", "-A"], check=True, capture_output=True)
        _run_command(
            [
                "git",
                "commit",
                "--signoff",
                "-S",
                "-m",
                "fix: codex loop {}".format(iteration_label),
                "-m",
                COAUTHOR_LINE,
            ],
            check=True,
            capture_output=True,
        )
    else:
        _print_step(
            "No working-tree changes to commit; pushing existing Codex commits."
        )
    _print_step("Pushing branch {}".format(branch))
    _run_command(["git", "push", "origin", branch], check=True, capture_output=True)
    return "committed"


def _pr_view(pr_ref: str) -> dict:
    data = _gh_json(
        [
            "pr",
            "view",
            pr_ref,
            "--json",
            "number,url,state,headRefName,baseRefName,isDraft",
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
    )


def _required_checks(branch: str) -> Tuple[list, bool]:
    checks = _pr_checks(branch, required_only=True)
    if checks:
        return checks, True
    return _pr_checks(branch, required_only=False), False


def _bucket_summary(checks) -> str:
    counts = {}
    for check in checks:
        bucket = check.get("bucket", "unknown")
        counts[bucket] = counts.get(bucket, 0) + 1
    parts = []
    for key in sorted(counts):
        parts.append("{}={}".format(key, counts[key]))
    return ", ".join(parts)


def _failing_check_records(checks: Sequence[dict]) -> List[dict]:
    return [
        {
            "name": check.get("name", "<unknown>"),
            "state": check.get("state", "<unknown>"),
            "link": check.get("link", ""),
            "workflow": check.get("workflow", ""),
        }
        for check in checks
        if check.get("bucket") in ("fail", "cancel")
    ]


def _format_failing_checks(records: Sequence[dict]) -> str:
    if not records:
        return "- <none>"
    lines = []
    for rec in records:
        link = " {}".format(rec["link"]) if rec.get("link") else ""
        lines.append("- {} [{}]{}".format(rec["name"], rec["state"], link))
    return "\n".join(lines)


def _wait_for_required_checks_green(
    *,
    branch: str,
    poll_seconds: int,
    timeout_seconds: int,
    treat_optional_as_blocking: bool = True,
) -> Tuple[bool, list]:
    _print_step("Waiting for required checks on PR branch {}".format(branch))
    started = time.monotonic()
    while True:
        checks, were_required = _required_checks(branch)
        if not checks:
            _print_step("No checks reported; treating as green.")
            return True, checks
        if not were_required and not treat_optional_as_blocking:
            _print_step(
                "No required checks reported; ignoring optional check failures."
            )
            return True, checks
        summary = _bucket_summary(checks)
        scope = "Required" if were_required else "All (no required reported)"
        _print_step("{} check buckets: {}".format(scope, summary))
        buckets = {check.get("bucket") for check in checks}
        if buckets.issubset({"pass", "skipping"}):
            return True, checks
        if "pending" not in buckets:
            return False, checks
        if (time.monotonic() - started) > timeout_seconds:
            raise CommandError(
                "Timed out waiting for required checks after {}s.".format(
                    timeout_seconds
                )
            )
        time.sleep(poll_seconds)


def _run_ci_fix_round(
    *,
    round_number: int,
    checks: list,
    model: Optional[str],
) -> bool:
    _print_step("Codex CI repair round {}".format(round_number))
    failing_summary = _format_failing_checks(_failing_check_records(checks))
    prompt = textwrap.dedent(
        """
        Required GitHub checks are failing on this branch.
        Failing checks:
        {failing_summary}

        Diagnose the failures (using gh/github logs as needed), fix the underlying
        code or test issues in this repository, and run any local verification needed.
        Do not commit or push.
        Return exactly one line:
        CI_FIX_READY=yes
        if you made a concrete fix and are ready for commit/push, otherwise:
        CI_FIX_READY=no
        """
    ).strip().format(failing_summary=failing_summary)
    marker_value, last_message = _codex_exec_with_marker(
        prompt=prompt,
        marker_regex=r"CI_FIX_READY=(yes|no)",
        model=model,
    )
    _print_step(
        "Codex marker output: {}".format(
            _truncate_for_log(last_message or "<empty>")
        )
    )
    if marker_value is None:
        raise CommandError("Codex did not return CI_FIX_READY marker.")
    return marker_value


def _rebase_onto_base(branch: str, base: str):
    _print_step("Rebasing {} onto origin/{}".format(branch, base))
    _run_command(["git", "fetch", "origin", base], check=True, capture_output=True)
    _run_command(
        ["git", "rebase", "origin/{}".format(base)], check=True, capture_output=True
    )
    _run_command(
        ["git", "push", "--force-with-lease", "origin", branch],
        check=True,
        capture_output=True,
    )


def _merge_pr(pr_ref: str):
    head_sha = _git_head_sha()
    _sign_off_pr(pr_ref)
    _print_step("Merging PR with rebase strategy")
    _run_command(
        [
            "gh",
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
        type=int,
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
        "--worktree-root",
        default=DEFAULT_WORKTREE_ROOT,
        help="Directory where per-PR git worktrees are created.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    pr_loop_lock = None
    try:
        _validate_identity_and_signing()
        _ensure_runtime_identity()
        current_branch = _git_branch()
        pr_ref = str(args.pr) if args.pr is not None else current_branch
        pr_data = _pr_view(pr_ref)
        pr_number = pr_data.get("number")
        if not isinstance(pr_number, int):
            raise CommandError("Could not resolve PR number for '{}'.".format(pr_ref))
        pr_loop_lock = _acquire_loop_lock(pr_number=pr_number)
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
        pr_base = pr_data.get("baseRefName")
        if pr_base and pr_base != args.base:
            raise CommandError(
                "PR #{} targets base '{}' but --base is '{}'. Use the PR base branch.".format(
                    pr_number, pr_base, args.base
                )
            )

        branch = pr_data.get("headRefName")
        if not branch:
            raise CommandError("Could not resolve PR head branch.")
        if branch == args.base:
            raise CommandError(
                "PR head branch '{}' matches base '{}'; aborting.".format(
                    branch, args.base
                )
            )

        worktree = _ensure_pr_worktree(
            worktree_root=args.worktree_root,
            pr_number=pr_number,
            branch=branch,
        )
        os.chdir(worktree)
        _print_step("Working in PR worktree {}".format(worktree))
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
            last_ci_round = round_number
            ci_green, checks = _wait_for_required_checks_green(
                branch=branch,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.checks_timeout_seconds,
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
            )
            if not ci_green_after_rebase:
                raise CommandError("Required checks failed after rebase.")

        if not args.skip_merge:
            _prepare_pr_for_merge(str(pr_number))
            _merge_pr(pr_target)
        _print_step("Done.")
        return 0
    finally:
        if pr_loop_lock is not None:
            pr_loop_lock.release()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CommandError as exc:
        sys.stderr.write("ERROR: {}\n".format(exc))
        raise SystemExit(1)
