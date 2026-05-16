"""Runtime identity and signing checks."""
from __future__ import annotations

import os
import random
import time

from .config import (
    GH_USER,
    GIT_EMAIL,
    GIT_NAME,
    SSH_AUTH_KEY,
    SSH_COMMAND,
    SSH_SIGNING_KEY,
)
from .errors import CommandError
from .gh_ops import _active_gh_user
from .git_ops import _git_config_get
from .process import _print_step, _run_command


_GIT_CONFIG_LOCK_RETRIES = 12
_GIT_CONFIG_LOCK_BASE_DELAY = 0.05


def _set_git_config_if_changed(key: str, value: str) -> None:
    """Write `git config <key> <value>` only if the current value differs.

    Retries on `.git/config.lock` contention so concurrent fan-out children
    sharing the same repo config don't race each other to failure.
    """
    if _git_config_get(key) == value:
        return
    last_error: CommandError = CommandError("")
    for attempt in range(_GIT_CONFIG_LOCK_RETRIES):
        try:
            _run_command(
                ["git", "config", key, value],
                check=True,
                capture_output=True,
            )
            return
        except CommandError as exc:
            text = str(exc)
            if "could not lock config file" not in text:
                raise
            last_error = exc
            delay = _GIT_CONFIG_LOCK_BASE_DELAY * (2 ** attempt)
            time.sleep(delay + random.uniform(0, delay))
            if _git_config_get(key) == value:
                return
    raise last_error

def _is_truthy(value: str) -> bool:
    return value.lower() in ("1", "true", "yes", "on")


def _looks_like_ssh_public_key(text: str) -> bool:
    head = text.strip().split()
    return bool(head) and head[0].startswith(("ssh-", "ecdsa-", "sk-ssh-", "sk-ecdsa-"))


def _validate_identity_and_signing():
    _print_step("Validating GitHub/git identity and signing configuration")
    gh_user = _active_gh_user()
    if gh_user != GH_USER:
        raise CommandError(
            "Active gh user is '{}' (expected '{}').".format(
                gh_user or "<empty>", GH_USER
            )
        )
    git_user = _git_config_get("user.name")
    if git_user != GIT_NAME:
        raise CommandError(
            "git user.name is '{}' (expected '{}').".format(
                git_user or "<empty>", GIT_NAME
            )
        )
    git_email = _git_config_get("user.email")
    if git_email != GIT_EMAIL:
        raise CommandError(
            "git user.email is '{}' (expected '{}').".format(
                git_email or "<empty>", GIT_EMAIL
            )
        )
    signing_key = _git_config_get("user.signingkey")
    if not signing_key:
        raise CommandError("git user.signingkey is not set.")
    if signing_key.startswith(("/", "~")):
        signing_key_path = os.path.expanduser(signing_key)
        if not os.path.exists(signing_key_path):
            raise CommandError(
                "Configured signing key path does not exist: {}".format(
                    signing_key_path
                )
            )
    elif not _looks_like_ssh_public_key(signing_key):
        raise CommandError(
            "git user.signingkey is set but is neither a readable path nor an "
            "SSH public key body: {!r}".format(signing_key[:80])
        )
    if not _is_truthy(_git_config_get("commit.gpgsign")):
        raise CommandError(
            "git commit.gpgsign must be enabled to ensure signed commits."
        )


def _ensure_runtime_identity():
    if not os.path.exists(SSH_AUTH_KEY):
        raise CommandError(
            "Required SSH auth key is missing: {}".format(SSH_AUTH_KEY)
        )
    if not os.path.exists(SSH_SIGNING_KEY):
        raise CommandError(
            "Required SSH signing key is missing: {}".format(SSH_SIGNING_KEY)
        )
    _print_step("Ensuring GitHub login is '{}'".format(GH_USER))
    gh_login = _active_gh_user()
    if gh_login != GH_USER:
        raise CommandError(
            "gh is authenticated as '{}' instead of '{}'.".format(gh_login, GH_USER)
        )
    origin = _run_command(
        ["git", "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
    )
    origin_url = (origin.stdout or "").strip()
    if not (
        origin_url.startswith("git@")
        or origin_url.startswith("ssh://")
    ):
        raise CommandError(
            "origin remote must use SSH so Ralph pushes with the configured SSH identity: {}".format(
                origin_url or "<empty>"
            )
        )
    _print_step(
        "Setting git identity and SSH/signing keys for '{}'".format(GH_USER)
    )
    _set_git_config_if_changed("user.name", GIT_NAME)
    _set_git_config_if_changed("user.email", GIT_EMAIL)
    _set_git_config_if_changed("core.sshCommand", SSH_COMMAND)
    _set_git_config_if_changed("gpg.format", "ssh")
    _set_git_config_if_changed("user.signingkey", SSH_SIGNING_KEY)
    _set_git_config_if_changed("commit.gpgsign", "true")
