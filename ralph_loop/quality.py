"""Local quality gates and commit/push flow."""
from __future__ import annotations

from typing import Optional, Tuple

from .codex_agent import _run_local_quality_fix_round, _run_pre_push_review_gate
from .config import COAUTHOR_LINE, QUALITY_GATE_OUTPUT_LIMIT
from .errors import CommandError
from .git_ops import _git_head_sha, _reset_generated_changes, _working_tree_dirty
from .process import (
    _completed_process_output,
    _print_step,
    _run_command,
    _truncate_for_log,
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
