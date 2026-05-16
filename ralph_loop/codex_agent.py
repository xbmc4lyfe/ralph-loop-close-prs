"""Codex execution and prompt-round helpers."""
from __future__ import annotations

import os
import re
import tempfile
import textwrap
from typing import List, Optional, Tuple

from .checks import _failing_check_records, _format_failing_checks
from .errors import CodexEnvironmentError, CommandError
from .process import _print_step, _run_command, _truncate_for_log


CODEX_LAST_MESSAGE_LIMIT = 4000

# Patterns that indicate codex itself (the upstream CLI / transport) is in an
# unrecoverable environmental state rather than reporting a reviewable result.
# Matching any of these means a respawn-immediately retry will just burn tokens
# hitting the same error, so the supervisor should back off significantly.
_CODEX_ENV_FAILURE_PATTERNS = (
    re.compile(r"\b401\s+Unauthorized\b", re.IGNORECASE),
    re.compile(r"Missing bearer or basic authentication", re.IGNORECASE),
    re.compile(r"\busage limit\b|\bpurchase more credits\b", re.IGNORECASE),
    re.compile(r"\binvalid api key\b", re.IGNORECASE),
    re.compile(r"exceeded retry limit.*\b429\b", re.IGNORECASE),
    re.compile(r"websocket.*HTTP error:\s*5\d\d", re.IGNORECASE),
    re.compile(r"Reconnecting\.\.\.\s*5\s*/\s*5", re.IGNORECASE),
    re.compile(
        r"codex exec failed.*no partial last-message", re.IGNORECASE | re.DOTALL
    ),
    # /review is a Codex slash-command, not a shell path. When the slash
    # command isn't installed for the worktree, Codex shells it out via
    # /bin/zsh -lc /review which exits 127. Treat that as an env failure
    # (long backoff, no review counted) so we don't loop on it.
    re.compile(
        r"no such file or directory:\s*/review", re.IGNORECASE
    ),
    re.compile(
        r"/bin/(?:ba|z)sh\s+-lc\s+/review.*exited\s+127", re.IGNORECASE | re.DOTALL
    ),
)


def _detect_codex_env_failure(*texts: str) -> Optional[str]:
    """Return a short reason if any of the codex env-failure patterns match.

    Each argument may be ``None`` or a string; they are inspected together so
    callers can pass stdout, stderr, and any captured last-message at once.
    """
    for text in texts:
        if not text:
            continue
        for pattern in _CODEX_ENV_FAILURE_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(0)
    return None


def _extract_yes_no_marker(*, marker_regex: str, text: str) -> Optional[bool]:
    """Return the yes/no marker value, scanning ``text`` bottom-up.

    Codex frequently emits its chain-of-thought before the final answer, and
    that narrative may contain intermediate ``MARKER=yes`` / ``MARKER=no``
    lines. We treat the LAST stripped line that fullmatches ``marker_regex``
    as the authoritative answer. Returns ``None`` if no line matches.
    """
    for raw_line in reversed(text.splitlines()):
        stripped = raw_line.strip()
        if not stripped:
            continue
        match = re.fullmatch(marker_regex, stripped, flags=re.IGNORECASE)
        if not match:
            continue
        values = match.groups() or (match.group(0),)
        for value in reversed(values):
            if isinstance(value, str) and value.lower() in ("yes", "no"):
                return value.lower() == "yes"
        return None
    return None


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
        cmd.append("-")
        log_cmd = cmd[:-1] + ["<codex prompt on stdin>"]
        completed = _run_command(
            cmd,
            check=False,
            capture_output=True,
            input_text=prompt,
            log_cmd=log_cmd,
        )
        try:
            with open(temp_path, "r", encoding="utf-8") as handle:
                last_message = _truncate_for_log(
                    handle.read().strip(),
                    CODEX_LAST_MESSAGE_LIMIT,
                )
        except FileNotFoundError:
            last_message = ""
    if completed.returncode != 0:
        env_reason = _detect_codex_env_failure(
            getattr(completed, "stdout", "") or "",
            getattr(completed, "stderr", "") or "",
            last_message,
        )
        if last_message == "":
            base_message = (
                "codex exec failed (exit={}) with no partial last-message captured.".format(
                    completed.returncode
                )
            )
        else:
            base_message = (
                "codex exec failed (exit={}); marker inference skipped. "
                "partial last-message: {}".format(
                    completed.returncode, _truncate_for_log(last_message)
                )
            )
        if env_reason is not None or last_message == "":
            # An empty last-message on a failed run is itself one of the
            # documented environmental failure modes, so escalate.
            detail = env_reason or "no partial last-message captured"
            raise CodexEnvironmentError(
                "{} [env failure: {}]".format(base_message, detail)
            )
        raise CommandError(base_message)
    marker_value = _extract_yes_no_marker(
        marker_regex=marker_regex, text=last_message
    )
    return marker_value, last_message


def _infer_review_pass_without_marker(last_message: str) -> Optional[bool]:
    text = last_message.lower()
    failure_text = re.sub(
        r"\b(no actionable issues remain|no issues remain)\b",
        "",
        text,
    )
    if re.search(
        r"\b(actionable issues remain|findings remain|issues remain)\b",
        failure_text,
    ):
        return False
    if re.search(
        r"\b(no findings|no actionable issues remain|no issues found|no issues remain)\b",
        text,
    ):
        return True
    return None


def _format_external_review_comments(comments: List[dict]) -> str:
    """Render PR review comments as a bullet list with COMMENT-<id> markers."""
    lines = []
    for comment in comments:
        body = (comment.get("body") or "").strip()
        first_lines = body.splitlines()[:6]
        snippet = "\n    ".join(first_lines)
        lines.append(
            "- COMMENT-{id} from @{user} at {path}:{line}\n    {snippet}".format(
                id=comment.get("id"),
                user=comment.get("user", "<unknown>"),
                path=comment.get("path", "<file>"),
                line=comment.get("line", 0),
                snippet=snippet,
            )
        )
    return "\n".join(lines)


def _parse_addressed_comments(text: str) -> List[Tuple[int, str]]:
    """Extract addressed-comment blocks from Codex output.

    Supports two forms in priority order:

    1. Multi-line block:
       ADDRESSED_COMMENT_START=<id>
       <summary lines>
       ADDRESSED_COMMENT_END

    2. Single-line fallback:
       ADDRESSED_COMMENT=<id>: <note>
    """
    addressed: List[Tuple[int, str]] = []
    lines = (text or "").splitlines()
    i = 0
    handled_ids: set = set()
    while i < len(lines):
        line = lines[i].strip()
        block_match = re.match(r"ADDRESSED_COMMENT_START=(\d+)\s*$", line)
        if block_match:
            comment_id = int(block_match.group(1))
            j = i + 1
            buf: List[str] = []
            while j < len(lines) and lines[j].strip() != "ADDRESSED_COMMENT_END":
                buf.append(lines[j])
                j += 1
            summary = "\n".join(buf).strip()
            if comment_id not in handled_ids:
                addressed.append((comment_id, summary))
                handled_ids.add(comment_id)
            i = j + 1
            continue
        single_match = re.match(r"ADDRESSED_COMMENT=(\d+)\s*:?\s*(.*)$", line)
        if single_match:
            comment_id = int(single_match.group(1))
            note = single_match.group(2).strip()
            if comment_id not in handled_ids:
                addressed.append((comment_id, note))
                handled_ids.add(comment_id)
        i += 1
    return addressed


def _run_review_fix_round(
    round_number: int,
    base: str,
    model: Optional[str],
    external_comments: Optional[List[dict]] = None,
) -> Tuple[bool, List[Tuple[int, str]]]:
    _print_step("Codex review/fix round {}".format(round_number))
    del base
    external_block = ""
    if external_comments:
        external_block = textwrap.dedent(
            """

            Existing reviewer comments on this PR (from bots and humans). Treat
            each as additional findings to consider alongside /review. When you
            make a code change that addresses one, emit a block in your final
            response of the form:

            ADDRESSED_COMMENT_START=<id>
            <multi-line summary explaining what you changed in the code, which
            files/functions you touched, and why it resolves the reviewer's
            concern>
            ADDRESSED_COMMENT_END

            Emit one such block per comment you addressed, BEFORE the
            REVIEW_PASS line. If a comment doesn't apply or you intentionally
            don't fix it, do not emit a block for it. The summary will be
            posted verbatim as a reply on the PR review comment, so write it
            for a human reviewer who needs to verify the fix.

            Comments:
            """
        ).rstrip() + "\n" + _format_external_review_comments(external_comments)
    prompt = (
        textwrap.dedent(
            """
            Run `/review`.
            If `/review` finds actionable issues, fix them in the current repository.
            Do not commit or push.
            Then run `/review` exactly one more time.
            If no actionable issues remain after that second review, end your
            response with the line:
            REVIEW_PASS=yes
            Otherwise end with:
            REVIEW_PASS=no
            """
        ).strip()
        + external_block
    )
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
    addressed = _parse_addressed_comments(last_message or "")
    if addressed:
        _print_step(
            "Codex reported addressing {} reviewer comment(s): {}".format(
                len(addressed),
                ", ".join("#{}".format(cid) for cid, _ in addressed),
            )
        )
    if marker_value is not None:
        return marker_value, addressed
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
    return inferred, addressed


def _run_pre_push_review_gate(*, base: str, model: Optional[str]) -> bool:
    _print_step("Running Codex /review gate before push")
    del base
    prompt = textwrap.dedent(
        """
        Run `/review` exactly once and do not modify files.
        If no actionable issues remain, return:
        PRE_PUSH_REVIEW_OK=yes
        Otherwise return:
        PRE_PUSH_REVIEW_OK=no
        Respond with exactly one line and nothing else.
        """
    ).strip()
    marker_value, last_message = _codex_exec_with_marker(
        prompt=prompt,
        marker_regex=r"PRE_PUSH_REVIEW_OK=(yes|no)",
        model=model,
        sandbox="read-only",
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
