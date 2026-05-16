"""GitHub check polling and formatting helpers."""
from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

from .errors import CommandError
from .gh_ops import _required_checks
from .process import _print_step
from .runtime import _check_wall_clock

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
        workflow = " workflow={}".format(rec["workflow"]) if rec.get("workflow") else ""
        link = " {}".format(rec["link"]) if rec.get("link") else ""
        lines.append(
            "- {} [{}]{}{}".format(rec["name"], rec["state"], workflow, link)
        )
    return "\n".join(lines)


def _wait_for_required_checks_green(
    *,
    branch: str,
    poll_seconds: int,
    timeout_seconds: int,
    treat_optional_as_blocking: bool = True,
    deadline: Optional[float] = None,
) -> Tuple[bool, list]:
    _print_step("Waiting for required checks on PR branch {}".format(branch))
    started = time.monotonic()

    def sleep_for_next_poll():
        elapsed = time.monotonic() - started
        remaining_timeout = timeout_seconds - elapsed
        if remaining_timeout <= 0:
            raise CommandError(
                "Timed out waiting for required checks after {}s.".format(
                    timeout_seconds
                )
            )
        delay = min(float(poll_seconds), remaining_timeout)
        if deadline is not None:
            remaining_deadline = deadline - time.monotonic()
            if remaining_deadline <= 0:
                _check_wall_clock(deadline)
            delay = min(delay, remaining_deadline)
        time.sleep(delay)

    while True:
        _check_wall_clock(deadline)
        checks, were_required = _required_checks(branch)
        if not checks:
            _print_step("No checks reported yet; waiting.")
            if (time.monotonic() - started) > timeout_seconds:
                raise CommandError(
                    "Timed out waiting for checks to appear after {}s.".format(
                        timeout_seconds
                    )
                )
            sleep_for_next_poll()
            continue
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
            if not were_required:
                _print_step("Required checks have not appeared yet; waiting.")
                sleep_for_next_poll()
                continue
            return True, checks
        if "pending" not in buckets:
            return False, checks
        if (time.monotonic() - started) > timeout_seconds:
            raise CommandError(
                "Timed out waiting for required checks after {}s.".format(
                    timeout_seconds
                )
            )
        sleep_for_next_poll()
