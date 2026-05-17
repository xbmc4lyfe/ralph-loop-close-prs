"""Shared exceptions for the Ralph loop."""
from __future__ import annotations

class CommandError(RuntimeError):
    """Raised when a subprocess command fails."""


class CodexEnvironmentError(CommandError):
    """Raised when codex exec fails due to an environmental/auth problem.

    These failures (e.g. 401 Unauthorized, repeated reconnect attempts,
    persistent transport errors) are not recoverable by re-running the same
    command in a tight loop, so the supervisor should back off significantly
    instead of immediately respawning the child.
    """


class RebaseConflictError(CommandError):
    """Raised when git rebase stops on conflicts that need branch changes."""


CODEX_ENV_FAILURE_EXIT_CODE = 75
REBASE_CONFLICT_EXIT_CODE = 65

# Exit code used when a ralph child detects that another ralph loop already
# owns this PR's worktree/lock. The supervisor uses this to apply a long
# backoff (no point re-spawning fast against an active sibling).
LOOP_ALREADY_RUNNING_EXIT_CODE = 64
