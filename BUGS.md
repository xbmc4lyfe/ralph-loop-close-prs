# Ralph Loop Bug & Gap Report

Consolidated from the current bug notes plus a fresh read of the current
working tree and recent `.ralph-logs/` output. Duplicate and already-fixed
historical findings are omitted from this active list.

## HIGH

No currently verified high-severity findings remain in this active tracker.

## MEDIUM

### M11. Required-check polling can go green on optional checks before required checks appear - `ralph_loop/gh_ops.py`, `ralph_loop/checks.py`

`_required_checks()` still falls back to all checks whenever
`gh pr checks --required` reports no required checks. `_wait_for_required_checks_green()`
can then return success if those fallback checks are only `pass` or `skipping`.
A fresh commit can therefore advance toward approval or merge when optional
checks have reported success but required checks have not appeared yet.

## LOW / DEFERRED

### L5. `_round_numbers` remains a very thin helper - `ralph_loop/runtime.py`

`_round_numbers()` is still a small bounded/unbounded generator. It is
acceptable as-is, but the abstraction is only useful if future loop code shares
it.

## OPEN GAPS

- The pytest suite passes, but coverage still needs broader integration-style
  behavior around git/GitHub side effects.
- No structured JSON-lines logging option.
- No telemetry for round counts, phase duration, or total wall-clock time.
- `COAUTHOR_LINE` is env-overridable, but there is still no validation that the
  configured email matches the active signer or intended attribution.
