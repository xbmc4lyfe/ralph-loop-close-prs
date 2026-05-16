# Ralph Pytest Suite Design

## Goal

Convert Ralph's test suite fully to pytest, remove converted unittest-only
artifacts, reduce duplicated stubbing, and add realistic local coverage that
does not touch real GitHub PRs or real Ralph worktrees.

## Scope

The suite should cover three layers:

1. Minimal conversion cleanup: remove `tests/test_helpers.py` after equivalent
   pytest coverage exists.
2. Harness-based pytest integration: use shared fixtures for temporary git
   repositories, fake executables, command logs, and isolated environment.
3. Black-box CLI simulation: run the compatibility script in subprocesses
   against fake `gh`, `codex`, and `just` commands for safe end-to-end flows.

The default test run must remain safe. It may create temporary git repositories
and temporary worktree roots under pytest's `tmp_path`, but it must not run
Ralph against a real PR, push to a real remote, approve a PR, merge a PR, or use
the user's real Ralph worktree root.

## Architecture

Shared pytest fixtures live in `tests/conftest.py`. They provide small,
purpose-built harnesses instead of repeated ad hoc monkeypatches. Unit tests
remain for pure helpers where direct assertions are clearer than scenario
setup. Integration tests use real local git repositories whenever the behavior
is about git state, staging, commits, worktrees, or pushes.

Fake executable fixtures create scripts in a temporary `bin` directory and
prepend it to `PATH`. Those scripts record argv and return deterministic JSON,
marker text, or failure output, letting subprocess-based CLI tests exercise the
same command lookup path as production while staying offline.

## File Layout

- `tests/conftest.py`: shared helpers for subprocess results, temp git repos,
  fake executable installation, command logs, CLI argument factories, and
  small spies where direct monkeypatching is still the simplest seam.
- `tests/test_process_and_git.py`: process helper behavior plus real temp-repo
  git helper coverage.
- `tests/test_worktrees_and_checks.py`: PR worktree, lock, runtime, and check
  polling behavior.
- `tests/test_gh_and_identity.py`: fake-`gh` and focused identity validation
  coverage.
- `tests/test_codex_and_quality.py`: Codex marker/prompt behavior plus
  local-quality and commit/push behavior against temp repos.
- `tests/test_cli_main.py`: direct CLI orchestration tests for branch behavior
  and error handling.
- `tests/test_cli_blackbox.py`: subprocess-driven compatibility script tests
  using fake `gh`, `codex`, and `just`.

## Deletion Policy

Delete only unittest files that have equivalent pytest coverage. Do not delete
non-unittest tests, docs, runtime files, or local runner artifacts merely because
they are not part of this refactor. Generated caches such as `__pycache__` may
remain ignored unless they interfere with validation.

## Validation

Run:

```bash
python3 -m py_compile codex_ralph_wiggum_loop.py ralph_loop/*.py tests/*.py
python3 -m pytest -q
python3 codex_ralph_wiggum_loop.py --help
```

If a validation command cannot run, report the command and the blocker.
