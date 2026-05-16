"""Codex execution and prompt-round helpers."""
from __future__ import annotations

import os
import re
import tempfile
import textwrap
from typing import List, Optional, Tuple

from .checks import _failing_check_records, _format_failing_checks
from .errors import CommandError
from .process import _print_step, _run_command, _truncate_for_log

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
        completed = _run_command(cmd, check=False, capture_output=True)
        try:
            with open(temp_path, "r", encoding="utf-8") as handle:
                last_message = handle.read().strip()
        except FileNotFoundError:
            last_message = ""
    if completed.returncode != 0:
        if last_message == "":
            raise CommandError(
                "codex exec failed (exit={}) with no partial last-message captured.".format(
                    completed.returncode
                )
            )
        _print_step(
            "codex exec exited {} but produced a last-message; continuing with marker inference.".format(
                completed.returncode
            )
        )
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
