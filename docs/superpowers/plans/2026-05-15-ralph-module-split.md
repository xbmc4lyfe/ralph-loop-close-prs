# Ralph Module Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the current `codex_ralph_wiggum_loop.py` monolith into focused modules while keeping the same executable script and CLI behavior.

**Architecture:** Keep `codex_ralph_wiggum_loop.py` as a thin compatibility wrapper around `ralph_loop.cli.main()`. Move existing behavior into responsibility-focused modules and preserve helper names where doing so reduces behavioral risk.

**Tech Stack:** Python 3.8+ standard library, `unittest`, existing external CLIs (`git`, `gh`, `codex`, `just`).

---

## File Structure

- Create `ralph_loop/__init__.py`: package marker and short package docstring.
- Create `ralph_loop/config.py`: environment-derived constants.
- Create `ralph_loop/errors.py`: shared `CommandError`.
- Create `ralph_loop/process.py`: logging, command rendering, subprocess execution, output formatting.
- Create `ralph_loop/git_ops.py`: git branch/config/status/reset/rebase helpers.
- Create `ralph_loop/gh_ops.py`: GitHub CLI retry/json helpers, PR metadata, checks, labels, reviews, merge.
- Create `ralph_loop/identity.py`: runtime identity setup and validation.
- Create `ralph_loop/worktrees.py`: lock, slug, PR fetch, worktree selection/creation.
- Create `ralph_loop/codex_agent.py`: Codex marker extraction, final-message handling, prompt rounds.
- Create `ralph_loop/checks.py`: required-check polling and failure formatting.
- Create `ralph_loop/quality.py`: local quality gates and commit/push flow.
- Create `ralph_loop/runtime.py`: deadline and round-number helpers.
- Create `ralph_loop/cli.py`: argparse, signal handling, top-level orchestration.
- Modify `codex_ralph_wiggum_loop.py`: compatibility wrapper only.
- Create `tests/test_helpers.py`: pure helper tests that do not invoke network or external CLIs.

## Task 1: Add Helper Tests First

**Files:**
- Create: `tests/test_helpers.py`

- [ ] **Step 1: Write failing tests**

```python
import argparse
import subprocess
import unittest

from ralph_loop.checks import (
    _bucket_summary,
    _failing_check_records,
    _format_failing_checks,
)
from ralph_loop.cli import _nonneg_int, _pos_int
from ralph_loop.codex_agent import (
    _extract_yes_no_marker,
    _infer_review_pass_without_marker,
)
from ralph_loop.process import _completed_process_output, _truncate_for_log
from ralph_loop.runtime import _round_numbers
from ralph_loop.worktrees import _pr_head_fetch_ref, _slug


class HelperTests(unittest.TestCase):
    def test_slug_keeps_safe_chars_and_collapses_unsafe_chars(self):
        self.assertEqual(_slug("feature/foo bar@123"), "feature-foo-bar-123")
        self.assertEqual(_slug("!!!"), "unknown")

    def test_marker_extraction_uses_last_marker(self):
        text = "REVIEW_PASS=no\nlater\nREVIEW_PASS=yes"
        self.assertIs(
            _extract_yes_no_marker(marker_regex=r"REVIEW_PASS=(yes|no)", text=text),
            True,
        )

    def test_review_pass_inference_handles_pass_and_fail_language(self):
        self.assertIs(_infer_review_pass_without_marker("No findings."), True)
        self.assertIs(
            _infer_review_pass_without_marker("Actionable issues remain."),
            False,
        )
        self.assertIsNone(_infer_review_pass_without_marker("Needs more context."))

    def test_check_formatting_summarizes_and_lists_failures(self):
        checks = [
            {"name": "unit", "bucket": "pass", "state": "SUCCESS"},
            {"name": "lint", "bucket": "fail", "state": "FAILURE", "link": "https://x"},
            {"name": "build", "bucket": "cancel", "state": "CANCELLED"},
        ]
        self.assertEqual(_bucket_summary(checks), "cancel=1, fail=1, pass=1")
        records = _failing_check_records(checks)
        self.assertEqual([record["name"] for record in records], ["lint", "build"])
        self.assertIn("- lint [FAILURE] https://x", _format_failing_checks(records))

    def test_round_numbers_supports_bounded_and_unbounded_modes(self):
        self.assertEqual(list(_round_numbers(3)), [1, 2, 3])
        generator = _round_numbers(0)
        self.assertEqual([next(generator), next(generator), next(generator)], [1, 2, 3])

    def test_integer_parsers_reject_wrong_signs(self):
        self.assertEqual(_nonneg_int("0"), 0)
        self.assertEqual(_pos_int("1"), 1)
        with self.assertRaises(argparse.ArgumentTypeError):
            _nonneg_int("-1")
        with self.assertRaises(argparse.ArgumentTypeError):
            _pos_int("0")

    def test_process_output_helpers_are_stable(self):
        self.assertEqual(_truncate_for_log("abcdef", limit=10), "abcdef")
        self.assertIn("truncated", _truncate_for_log("abcdefghij", limit=6))
        completed = subprocess.CompletedProcess(
            args=["cmd"],
            returncode=1,
            stdout="out\n",
            stderr="err\n",
        )
        self.assertEqual(
            _completed_process_output(completed),
            "stdout:\nout\n\nstderr:\nerr",
        )

    def test_pr_head_fetch_ref_is_stable(self):
        self.assertEqual(_pr_head_fetch_ref(42), "refs/remotes/origin/pr-42-head")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail for the right reason**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: `ModuleNotFoundError: No module named 'ralph_loop'`.

## Task 2: Create Package Modules

**Files:**
- Create: `ralph_loop/*.py`
- Modify: `codex_ralph_wiggum_loop.py`

- [ ] **Step 1: Move behavior into modules**

Move existing functions into the files listed in File Structure. Keep names and
function bodies as close as possible to the current script, changing only import
statements and cross-module references.

- [ ] **Step 2: Replace the top-level script with the compatibility wrapper**

```python
#!/usr/bin/env python3
"""Compatibility entry point for the Ralph loop CLI."""
from __future__ import annotations

import sys

if sys.version_info < (3, 8):
    sys.stderr.write(
        "ERROR: Python 3.8+ is required (uses shlex.join); got {}.{}.\n".format(
            sys.version_info.major, sys.version_info.minor
        )
    )
    raise SystemExit(2)

from ralph_loop.cli import main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
```

- [ ] **Step 3: Run helper tests**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: all helper tests pass.

## Task 3: Verify CLI Compatibility

**Files:**
- Modify if needed: `ralph_loop/cli.py`
- Modify if needed: `codex_ralph_wiggum_loop.py`

- [ ] **Step 1: Compile all Python files**

Run:

```bash
python3 -m py_compile codex_ralph_wiggum_loop.py ralph_loop/*.py tests/*.py
```

Expected: exit code 0.

- [ ] **Step 2: Check help output**

Run:

```bash
python3 codex_ralph_wiggum_loop.py --help
```

Expected: help output includes all current flags:
`--pr`, `--base`, `--max-review-rounds`, `--max-ci-rounds`,
`--max-local-quality-rounds`, `--poll-seconds`, `--checks-timeout-seconds`,
`--model`, `--skip-rebase`, `--skip-merge`, `--worktree-root`, and
`--max-wall-clock-seconds`.

## Self-Review

- Spec coverage: package split, compatibility wrapper, pure helper tests, and
  verification commands are covered.
- Placeholder scan: no TODO/TBD placeholders remain.
- Type consistency: module names and helper names match the spec and test code.
- Execution mode: implementation should run inline in this session because the
  split is cross-cutting and the dirty worktree makes parallel code edits risky.
