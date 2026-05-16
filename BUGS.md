# Ralph Loop Bug & Gap Report

Consolidated from the current bug notes plus a fresh read of the current
working tree and recent `.ralph-logs/` output. Duplicate and already-fixed
historical findings are omitted from this active list.

## HIGH

No currently verified high-severity findings remain in this active tracker.

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
