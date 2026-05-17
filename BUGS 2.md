# Ralph Loop Bug & Gap Report

Verified against current `main` after removing fixed or obsolete entries.

## Open Gaps

### M4. Large GitHub JSON responses are capped before parsing

`gh` command output is now bounded by `GH_OUTPUT_LIMIT` before JSON parsing.
This prevents unbounded memory growth from unexpectedly large GitHub CLI output,
but an oversized otherwise-valid JSON response can still fail to parse after
truncation. Treat that as an operational failure rather than silently accepting
partial machine-readable data.

### M24. Local quality failure redaction is best-effort

Local quality failure output is no longer replayed directly, common token
formats are redacted, and URLs / SSH repo URLs are replaced before the summary is
sent to Codex. This remains best-effort: unusual secret formats may still need
additional redaction patterns if they appear in real logs.
