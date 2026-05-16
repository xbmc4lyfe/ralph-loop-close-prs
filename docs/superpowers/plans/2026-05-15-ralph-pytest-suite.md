# Ralph Pytest Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an all-pytest Ralph test suite with less duplication, realistic local git/process coverage, safe fake GitHub/Codex/just command paths, and no remaining converted unittest files.

**Architecture:** Keep direct unit tests for pure helpers, move repeated setup into pytest fixtures, and add integration tests that use temp repositories and fake executables. Black-box CLI coverage runs the compatibility entry point in subprocesses with isolated environment and temp worktree roots.

**Tech Stack:** Python 3.8-compatible pytest, `tmp_path`, `monkeypatch`, local `git`, subprocess fake executables.

---

### Task 1: Consolidate Shared Pytest Harness

**Files:**
- Modify: `tests/conftest.py`
- Test: `tests/test_process_and_git.py`

- [ ] **Step 1: Add real git and fake executable fixtures**

Add helpers named `run_git`, `git_repo`, `bare_origin`, `fake_bin`, and
`install_fake_executable`. `git_repo` must initialize a real repo, configure a
test identity, create an initial commit, and return the repo path. Fake
executables must write their argv to a command log.

- [ ] **Step 2: Run collection**

Run: `python3 -m pytest --collect-only -q`

Expected: all current tests collect.

### Task 2: Replace Mocked Git Behavior With Temp-Repo Tests

**Files:**
- Modify: `tests/test_process_and_git.py`
- Modify: `tests/test_codex_and_quality.py`
- Test: `tests/test_process_and_git.py`
- Test: `tests/test_codex_and_quality.py`

- [ ] **Step 1: Add failing temp-repo coverage**

Add tests that call real git through Ralph helpers for branch detection,
dirty-tree detection, reset/clean behavior, staging generated-artifact filters,
signed commit command behavior with command shims where signing would otherwise
be environment-dependent, and push behavior to a local bare origin.

- [ ] **Step 2: Verify failures identify missing harness coverage**

Run: `python3 -m pytest tests/test_process_and_git.py tests/test_codex_and_quality.py -q`

Expected: new tests fail only where the harness or assertions need adjustment.

- [ ] **Step 3: Refactor tests and fixtures until green**

Keep runtime code unchanged unless a test exposes a real defect. If runtime code
must change, add or keep the failing pytest first, then make the smallest fix.

### Task 3: Add Fake GitHub/Codex/Just CLI Coverage

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/test_gh_and_identity.py`
- Create: `tests/test_cli_blackbox.py`
- Test: `tests/test_gh_and_identity.py`
- Test: `tests/test_cli_blackbox.py`

- [ ] **Step 1: Add fake command scripts**

Use `install_fake_executable` to create fake `gh`, `codex`, and `just` scripts
that read environment variables for deterministic responses and append argv to
log files.

- [ ] **Step 2: Add fake-gh tests**

Cover JSON parsing, transient retry, label creation fallback, approval skipping,
and merge command construction through the fake executable path rather than
only monkeypatching `_run_command`.

- [ ] **Step 3: Add black-box CLI tests**

Add subprocess tests for `codex_ralph_wiggum_loop.py --help`, dry-run safety,
and one safe skip-merge happy path using fake commands and a temp worktree root.

### Task 4: Remove Converted Unittest Artifacts

**Files:**
- Delete: `tests/test_helpers.py`
- Modify: test modules only if needed to preserve equivalent coverage

- [ ] **Step 1: Confirm equivalent pytest coverage**

Run: `python3 -m pytest --collect-only -q`

Expected: helper behaviors formerly in `tests/test_helpers.py` are collected in
pytest modules.

- [ ] **Step 2: Delete only converted unittest files**

Delete `tests/test_helpers.py`. Do not delete non-unittest files.

### Task 5: Final Validation And Docs Check

**Files:**
- Modify: `README.md` only if test layout or validation commands changed.

- [ ] **Step 1: Syntax check**

Run:

```bash
python3 -m py_compile codex_ralph_wiggum_loop.py ralph_loop/*.py tests/*.py
```

Expected: no output and exit code 0.

- [ ] **Step 2: Full pytest**

Run:

```bash
python3 -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: CLI help**

Run:

```bash
python3 codex_ralph_wiggum_loop.py --help
```

Expected: help text prints and exit code is 0.

## Self-Review

- Spec coverage: cleanup, fixture harness, fake commands, black-box CLI tests,
  and deletion limits are covered by Tasks 1-5.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: fixtures and file names match the current pytest layout.
