"""Configuration constants for the Ralph loop."""
from __future__ import annotations

import os
import shlex
import tempfile

GH_USER = os.environ.get("RALPH_GH_USER", "xbmc4lyfe")


GIT_NAME = os.environ.get("RALPH_GIT_NAME", GH_USER)


GIT_EMAIL = os.environ.get(
    "RALPH_GIT_EMAIL", "{}@users.noreply.github.com".format(GH_USER)
)


def _expand_user_path(path: str) -> str:
    return os.path.expanduser(path)


def _env_path(name: str, default: str) -> str:
    return _expand_user_path(os.environ.get(name, default))


def _expand_user_command_token(token: str) -> str:
    if token.startswith("~"):
        return os.path.expanduser(token)
    name, separator, value = token.partition("=")
    if separator and value.startswith("~"):
        return "{}{}{}".format(name, separator, os.path.expanduser(value))
    return token


def _expand_user_paths_in_command(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    return shlex.join([_expand_user_command_token(token) for token in tokens])


SSH_AUTH_KEY = _env_path(
    "RALPH_SSH_AUTH_KEY",
    "~/.ssh/id_ed25519_{}".format(GH_USER),
)


SSH_SIGNING_KEY = _env_path(
    "RALPH_SSH_SIGNING_KEY",
    "~/.ssh/id_ed25519_signing.pub",
)


SSH_COMMAND = _expand_user_paths_in_command(
    os.environ.get(
        "RALPH_SSH_COMMAND",
        "ssh -i {} -o IdentitiesOnly=yes -o IdentityAgent=none".format(SSH_AUTH_KEY),
    )
)


COAUTHOR_LINE = os.environ.get(
    "RALPH_COAUTHOR_LINE", "Co-Authored-By: Oz <oz-agent@warp.dev>"
)


NEEDS_REVIEW_LABEL = "needs review"


LOOP_ALREADY_RUNNING_MESSAGE = "found another ralph loop already for this PR"


DEFAULT_WORKTREE_ROOT = _env_path(
    "RALPH_WORKTREE_ROOT",
    os.path.join(tempfile.gettempdir(), "codex-ralph-worktrees"),
)


QUALITY_GATE_OUTPUT_LIMIT = 12000
