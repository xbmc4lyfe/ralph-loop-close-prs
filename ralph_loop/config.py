"""Configuration constants for the Ralph loop."""
from __future__ import annotations

import os
import tempfile

GH_USER = os.environ.get("RALPH_GH_USER", "xbmc4lyfe")


GIT_NAME = os.environ.get("RALPH_GIT_NAME", GH_USER)


GIT_EMAIL = os.environ.get(
    "RALPH_GIT_EMAIL", "{}@users.noreply.github.com".format(GH_USER)
)


SSH_AUTH_KEY = os.environ.get(
    "RALPH_SSH_AUTH_KEY",
    os.path.expanduser("~/.ssh/id_ed25519_{}".format(GH_USER)),
)


SSH_SIGNING_KEY = os.environ.get(
    "RALPH_SSH_SIGNING_KEY",
    os.path.expanduser("~/.ssh/id_ed25519_signing.pub"),
)


SSH_COMMAND = os.environ.get(
    "RALPH_SSH_COMMAND",
    "ssh -i {} -o IdentitiesOnly=yes -o IdentityAgent=none".format(SSH_AUTH_KEY),
)


COAUTHOR_LINE = os.environ.get(
    "RALPH_COAUTHOR_LINE", "Co-Authored-By: Oz <oz-agent@warp.dev>"
)


NEEDS_REVIEW_LABEL = "needs review"


LOOP_ALREADY_RUNNING_MESSAGE = "found another ralph loop already for this PR"


DEFAULT_WORKTREE_ROOT = os.environ.get(
    "RALPH_WORKTREE_ROOT",
    os.path.join(tempfile.gettempdir(), "codex-ralph-worktrees"),
)


QUALITY_GATE_OUTPUT_LIMIT = 12000
