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


def _required_checks_for_ref(pr_ref: str) -> Tuple[list, bool]:
    return _required_checks(pr_ref)


def _wait_for_required_checks_green(
    *,
    branch: str,
    pr_number: Optional[int] = None,
    poll_seconds: int,
    timeout_seconds: int,
    deadline: Optional[float] = None,
    no_checks_grace_seconds: int = 120,
) -> Tuple[bool, list]:
    pr_ref = str(pr_number) if pr_number is not None else branch
    _print_step("Waiting for required checks on PR {}".format(pr_ref))
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
        checks, were_required = _required_checks_for_ref(pr_ref)
        if not checks:
            elapsed = time.monotonic() - started
            if elapsed >= no_checks_grace_seconds:
                _print_step(
                    "No checks reported after {}s grace; treating branch as "
                    "having no CI and proceeding.".format(no_checks_grace_seconds)
                )
                return True, []
            _print_step(
                "No checks reported yet; waiting (grace {}s).".format(
                    no_checks_grace_seconds
                )
            )
            sleep_for_next_poll()
            continue
        summary = _bucket_summary(checks)
        scope = "Required" if were_required else "All (no required reported)"
        _print_step("{} check buckets: {}".format(scope, summary))
        buckets = {check.get("bucket") for check in checks}
        if buckets.issubset({"pass", "skipping"}):
            if not were_required:
                elapsed = time.monotonic() - started
                if elapsed >= no_checks_grace_seconds:
                    _print_step(
                        "No required checks reported after {}s grace; accepting "
                        "fallback checks.".format(no_checks_grace_seconds)
                    )
                    return True, checks
                _print_step(
                    "No required checks reported yet; fallback checks are "
                    "green, waiting (grace {}s).".format(
                        no_checks_grace_seconds
                    )
                )
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
