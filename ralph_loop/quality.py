"""Local quality gates and commit/push flow."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

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
from .runtime import _check_wall_clock

_GENERATED_ARTIFACT_DIRS = frozenset(
    (
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".nox",
        "htmlcov",
        ".ralph-logs",
    )
)
_GENERATED_ARTIFACT_FILES = frozenset((".coverage", "coverage.xml", ".DS_Store"))
_GENERATED_ARTIFACT_SUFFIXES = (".pyc", ".pyo")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)(token=)[^\s&]+"),
    re.compile(r"(?i)(password=)[^\s&]+"),
)


@dataclass
class LocalQualityTelemetry:
    repair_rounds: int = 0


def _redact_for_prompt(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(r"\1<redacted>", redacted)
    return redacted


def _is_generated_artifact_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    if not normalized:
        return False
    parts = [part for part in normalized.split("/") if part]
    # isdisjoint in C is faster than any() with a python generator expression
    if not _GENERATED_ARTIFACT_DIRS.isdisjoint(parts):
        return True
    filename = parts[-1]
    if filename in _GENERATED_ARTIFACT_FILES:
        return True
    if filename.startswith(".coverage."):
        return True
    return filename.endswith(_GENERATED_ARTIFACT_SUFFIXES)


def _untracked_files_for_commit() -> List[str]:
    result = _run_command(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
        max_output_bytes=None,
    )
    return [path for path in (result.stdout or "").split("\0") if path]


def _stage_commit_changes():
    _run_command(["git", "add", "-u"], check=True, capture_output=True)
    untracked_paths = [
        path
        for path in _untracked_files_for_commit()
        if not _is_generated_artifact_path(path)
    ]
    if untracked_paths:
        _run_command(
            ["git", "add", "--"] + untracked_paths,
            check=True,
            capture_output=True,
        )


def _staged_changes_exist() -> bool:
    result = _run_command(
        ["git", "diff", "--cached", "--quiet"],
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise CommandError(
        "Unable to inspect staged changes with `git diff --cached --quiet`."
    )


def _run_local_quality_gates() -> Tuple[bool, str]:
    _print_step("Running local quality gates before commit/push (just ci + just test)")
    for recipe in ("ci", "test"):
        result = _run_command(
            ["just", recipe],
            check=False,
            capture_output=True,
            replay_output=False,
        )
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
                _redact_for_prompt(failure_summary),
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
    deadline: Optional[float] = None,
    telemetry: Optional[LocalQualityTelemetry] = None,
) -> str:
    local_quality_round = 0
    review_gate_needed = require_review_gate
    while True:
        _check_wall_clock(deadline)
        head_sha = _git_head_sha()
        has_new_commits = bool(pre_round_sha) and head_sha != pre_round_sha
        dirty = _working_tree_dirty()
        if not dirty and not has_new_commits:
            _print_step("No changes to commit.")
            return "no_changes"
        if not dirty and has_new_commits:
            _print_step(
                "Codex created commits directly; discarding them instead of pushing unreviewed commits."
            )
            _reset_generated_changes(pre_round_sha)
            return "discarded"
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
        if telemetry is not None:
            telemetry.repair_rounds += 1
        _check_wall_clock(deadline)
        ready = _run_local_quality_fix_round(
            round_number=local_quality_round,
            failure_summary=failure_summary,
            model=model,
        )
        _check_wall_clock(deadline)
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
    _check_wall_clock(deadline)
    if _working_tree_dirty():
        _print_step("Committing Codex-generated changes")
        _stage_commit_changes()
        if _staged_changes_exist():
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
        elif has_new_commits:
            _print_step(
                "No committable working-tree changes after filtering generated artifacts; pushing existing Codex commits."
            )
        else:
            _print_step(
                "No committable changes after filtering generated artifacts."
            )
            _reset_generated_changes(pre_round_sha)
            return "no_changes"
    else:
        _print_step(
            "No working-tree changes to commit; pushing existing Codex commits."
        )
    _print_step("Pushing branch {}".format(branch))
    _run_command(["git", "push", "origin", branch], check=True, capture_output=True)
    return "committed"
