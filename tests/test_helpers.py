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
            {
                "name": "lint",
                "bucket": "fail",
                "state": "FAILURE",
                "link": "https://x",
            },
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
