import argparse
import itertools
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from unittest.mock import Mock

import pytest

from ralph_loop import cli


class DummyLock:
    def __init__(self):
        self.release = Mock(name="lock.release")


def completed(returncode=0, stdout="", stderr="", args=None):
    return subprocess.CompletedProcess(
        args=args or ["cmd"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def make_args(**overrides):
    values = {
        "pr": 7,
        "base": "main",
        "max_review_rounds": 1,
        "max_ci_rounds": 1,
        "max_local_quality_rounds": 0,
        "poll_seconds": 1,
        "checks_timeout_seconds": 2,
        "model": None,
        "skip_rebase": True,
        "skip_merge": True,
        "dry_run": False,
        "worktree_root": "/tmp/ralph-worktrees",
        "max_wall_clock_seconds": 0,
        "json_log": None,
        "directory": None,
        "recursive": False,
        "all_prs": False,
        "fan_out_log_dir": None,
        "fan_out_stuck_timeout_seconds": 900,
        "fan_out_respawn_backoff_seconds": 5,
        "fan_out_env_failure_backoff_seconds": 300,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def open_pr(**overrides):
    values = {
        "number": 7,
        "url": "https://example.test/pr/7",
        "state": "OPEN",
        "isDraft": False,
        "isCrossRepository": False,
        "baseRefName": "main",
        "headRefName": "feature",
    }
    values.update(overrides)
    return values


class CliHarness:
    def __init__(self, monkeypatch, worktree, *, args=None, pr_data=None):
        self.monkeypatch = monkeypatch
        self.args = args or make_args()
        self.pr_data = pr_data if pr_data is not None else open_pr()
        self.worktree = str(worktree)
        self.lock = DummyLock()

        self.parse_args = Mock(return_value=self.args, name="cli._parse_args")
        self.signal = Mock(name="signal.signal")
        self.ensure_identity = Mock(name="cli._ensure_runtime_identity")
        self.git_branch = Mock(return_value="current", name="cli._git_branch")
        self.pr_view = Mock(return_value=self.pr_data, name="cli._pr_view")
        self.acquire_lock = Mock(
            return_value=self.lock, name="cli._acquire_loop_lock"
        )
        self.ensure_worktree = Mock(
            return_value=self.worktree, name="cli._ensure_pr_worktree"
        )
        self.validate_identity = Mock(name="cli._validate_identity_and_signing")
        self.working_dirty = Mock(return_value=False, name="cli._working_tree_dirty")
        self.mark_review = Mock(name="cli._mark_pr_needs_review")
        self.rebase = Mock(name="cli._rebase_onto_base")
        self.git_head = Mock(return_value="sha", name="cli._git_head_sha")
        self.review_round = Mock(
            return_value=(True, []), name="cli._run_review_fix_round"
        )
        self.commit_push = Mock(return_value="no_changes", name="cli._commit_and_push")
        self.wait_checks = Mock(
            return_value=(True, [{"name": "unit", "bucket": "pass"}])
        )
        self.ci_fix = Mock(return_value=True, name="cli._run_ci_fix_round")
        self.reset_changes = Mock(name="cli._reset_generated_changes")
        self.prepare_merge = Mock(name="cli._prepare_pr_for_merge")
        self.merge_pr = Mock(name="cli._merge_pr")
        self.pr_review_comments = Mock(
            return_value=[], name="cli._pr_review_comments"
        )
        self.reply_review_comment = Mock(
            return_value=True, name="cli._reply_to_pr_review_comment"
        )

    def install(self):
        self.monkeypatch.setattr(cli, "_parse_args", self.parse_args)
        self.monkeypatch.setattr(cli.signal, "signal", self.signal)
        self.monkeypatch.setattr(
            cli, "_ensure_runtime_identity", self.ensure_identity
        )
        self.monkeypatch.setattr(cli, "_git_branch", self.git_branch)
        self.monkeypatch.setattr(cli, "_pr_view", self.pr_view)
        self.monkeypatch.setattr(cli, "_acquire_loop_lock", self.acquire_lock)
        self.monkeypatch.setattr(cli, "_ensure_pr_worktree", self.ensure_worktree)
        self.monkeypatch.setattr(
            cli, "_validate_identity_and_signing", self.validate_identity
        )
        self.monkeypatch.setattr(cli, "_working_tree_dirty", self.working_dirty)
        self.monkeypatch.setattr(cli, "_mark_pr_needs_review", self.mark_review)
        self.monkeypatch.setattr(cli, "_rebase_onto_base", self.rebase)
        self.monkeypatch.setattr(cli, "_git_head_sha", self.git_head)
        self.monkeypatch.setattr(cli, "_run_review_fix_round", self.review_round)
        self.monkeypatch.setattr(cli, "_commit_and_push", self.commit_push)
        self.monkeypatch.setattr(
            cli, "_wait_for_required_checks_green", self.wait_checks
        )
        self.monkeypatch.setattr(cli, "_run_ci_fix_round", self.ci_fix)
        self.monkeypatch.setattr(
            cli, "_reset_generated_changes", self.reset_changes
        )
        self.monkeypatch.setattr(cli, "_prepare_pr_for_merge", self.prepare_merge)
        self.monkeypatch.setattr(cli, "_merge_pr", self.merge_pr)
        self.monkeypatch.setattr(
            cli, "_pr_review_comments", self.pr_review_comments
        )
        self.monkeypatch.setattr(
            cli, "_reply_to_pr_review_comment", self.reply_review_comment
        )
        return self


@pytest.fixture
def completed_process():
    return completed


@pytest.fixture
def spy():
    return Mock


@pytest.fixture
def run_git():
    def run(cwd, *args, check=True):
        return subprocess.run(
            ["git"] + list(args),
            cwd=str(cwd),
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    return run


@pytest.fixture
def git_repo(tmp_path, run_git):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init")
    run_git(repo, "checkout", "-b", "main")
    run_git(repo, "config", "user.name", "Test User")
    run_git(repo, "config", "user.email", "test@example.invalid")
    run_git(repo, "config", "commit.gpgsign", "false")
    (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
    run_git(repo, "add", "tracked.txt")
    run_git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def bare_origin(tmp_path, run_git):
    origin = tmp_path / "origin.git"
    run_git(tmp_path, "init", "--bare", str(origin))
    return origin


@dataclass
class FakeCommand:
    path: Path
    log_path: Path


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv(
        "PATH",
        "{}{}{}".format(bin_dir, os.pathsep, os.environ.get("PATH", "")),
    )
    return bin_dir


@pytest.fixture
def install_fake_executable(fake_bin, tmp_path):
    def install(name, body):
        path = fake_bin / name
        log_path = tmp_path / "{}.argv.log".format(name)
        script = "#!/usr/bin/env python3\nimport os, sys\nLOG_PATH = {!r}\n{}\n".format(
            str(log_path),
            body,
        )
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
        return FakeCommand(path=path, log_path=log_path)

    return install


@pytest.fixture
def command_log():
    def read(path):
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    return read


@pytest.fixture
def cli_args():
    return make_args


@pytest.fixture
def pr_data():
    return open_pr


@pytest.fixture
def cli_harness(monkeypatch, tmp_path):
    counter = itertools.count(1)

    def factory(*, args=None, pr_data=None, worktree=None):
        if worktree is None:
            worktree = tmp_path / "worktrees" / "pr-{}".format(next(counter))
            worktree.mkdir(parents=True)
        return CliHarness(
            monkeypatch,
            worktree,
            args=args,
            pr_data=pr_data,
        ).install()

    return factory
