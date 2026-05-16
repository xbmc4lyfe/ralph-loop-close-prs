# Bug & Gap Report — `codex_ralph_wiggum_loop.py`

Remaining items only — every HIGH and most MEDIUM/LOW findings have been
fixed. The entries below are deliberately *not* fixed, with the reason
recorded so a future pass can decide.

---

## MEDIUM (deferred)

### M5. `_round_numbers(0)` is unbounded with no global wall-clock cap — `_round_numbers`
With `--max-*-rounds=0` (the default for review/CI/local-quality loops), a
pathological Codex session that always reports `..._READY=yes` without
making real progress can spin indefinitely. Mitigations now in place — per-
round SHA reset (M7) and the local-quality cap — make the spin less
expensive, but a true global timeout would require threading a deadline
through the loop scaffolding and was out of scope for the targeted-fix
pass. Suggested follow-up: add `--max-wall-clock-seconds` and check it at
the top of each round.

### M9. No retry/backoff on transient `gh` failures — *throughout*
A flaky `gh api` / `gh pr view` / `gh pr checks` call still aborts the
entire loop. Adding retries changes failure semantics (could mask real
auth/network breakage), so this needs an explicit decision on retry policy
(count, backoff, which calls are safe to retry).

### M10. `_codex_exec_with_marker` discards Codex’s exit signal — `_codex_exec_with_marker`
Still uses `check=True`, so a non-zero `codex` exit raises before any
partial last-message can be read. The fix would be to switch to
`check=False` and decide based on both exit code and tempfile contents.
Behavioral change — left for a follow-up that can also reason about how
the surrounding loops should treat “codex itself crashed.”

### M15. `os.chdir(worktree)` is process-global with no restore — `main`
Subsequent helpers depend on the post-chdir cwd. Restoring would require
threading `cwd=` through the rest of the call graph or pushing/popping in
a context manager — invasive enough to defer.

### M19. `_run_command` buffers entire process output in memory — `_run_command`
`capture_output=True` still loads full stdout/stderr into a single string.
A long `just test` or chatty Codex run can balloon memory. Fix would be to
stream to a temp file for large commands and only summarize on failure.

---

## LOW (deferred)

### L8. `_round_numbers` is a thin helper — `_round_numbers`
Used in two places. Inlining would duplicate the bounded/unbounded branch.
Kept as-is.

### L12. No signal handling — *throughout*
Ctrl+C still leaves in-flight subprocesses to die without graceful
teardown. Fine for an automation script; documented for awareness.

### L16. No `--dry-run` flag — *throughout*
Given the destructive operations (force-with-lease push, rebase merge,
`gh pr merge --delete-branch`), an opt-in dry run would aid debugging.
Out of scope for this pass.

### L18. `_validate_identity_and_signing` runs before `os.chdir(worktree)` — `main`
It reads git config from wherever the script was launched, then
`_ensure_runtime_identity` re-runs after chdir to set the worktree-local
config. If launched from a non-repo directory the validator’s error is
confusing. Tied to M15.

---

## Gaps (still open)

- **No tests.** Even unit-level coverage of pure helpers
  (`_extract_yes_no_marker`, `_slug`, `_bucket_summary`, `_round_numbers`,
  `_truncate_for_log`, `_failing_check_records`, `_format_failing_checks`,
  `_nonneg_int`, `_pos_int`) would catch regressions.
- **No structured (JSON-line) logging option.**
- **No telemetry on round counts / wall clock per phase.**
- **No top-level docstring describing the orchestration phases**
  (validate → worktree → review loop → CI loop → rebase → merge).
- **`COAUTHOR_LINE`** is now env-overridable but there is still no
  validation that the configured email matches the active signer.
