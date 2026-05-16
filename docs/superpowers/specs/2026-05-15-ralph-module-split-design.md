# Ralph Module Split Design

## Goal

Split `codex_ralph_wiggum_loop.py` into a small Python package while preserving
the existing CLI contract:

```bash
python3 codex_ralph_wiggum_loop.py --help
python3 codex_ralph_wiggum_loop.py --pr 123 --base main --skip-merge
```

The refactor starts from the current dirty worktree state, including the
uncommitted GH retry, spooled output, wall-clock timeout, signal handling, and
cwd restoration behavior already present in the script.

## Non-Goals

- Do not change the PR automation workflow.
- Do not rename or remove CLI flags.
- Do not change git, GitHub, Codex, or `just` command semantics.
- Do not add a packaging system unless it is required for local tests.
- Do not clean unrelated dirty files such as `.DS_Store`, `__pycache__/`, or
  documentation changes outside this refactor.

## Architecture

Create a package named `ralph_loop/` and keep
`codex_ralph_wiggum_loop.py` as a compatibility entry point. The wrapper keeps
the Python version guard and delegates to `ralph_loop.cli.main()`.

The package is split by operational responsibility:

- `ralph_loop.config`: environment-derived constants and fixed labels.
- `ralph_loop.errors`: shared `CommandError`.
- `ralph_loop.process`: subprocess execution, step logging, command formatting,
  output truncation, and completed-process output formatting.
- `ralph_loop.git_ops`: git config/status/branch/reset/rebase helpers.
- `ralph_loop.gh_ops`: GitHub CLI retries, JSON helpers, PR metadata, labels,
  reviews, checks, and merge operations.
- `ralph_loop.identity`: runtime identity and signing validation/setup.
- `ralph_loop.worktrees`: PR lock handling, branch slugging, PR head fetches,
  and worktree creation/reuse.
- `ralph_loop.codex_agent`: `codex exec` marker handling and Codex prompt
  rounds for review, pre-push review, local quality repair, and CI repair.
- `ralph_loop.checks`: check bucket summaries, failing-check formatting, and
  required-check polling.
- `ralph_loop.quality`: local `just ci` / `just test` gates and commit/push
  retry loop.
- `ralph_loop.runtime`: wall-clock deadline and round-number helpers.
- `ralph_loop.cli`: argument parsing, signal handling, and top-level workflow
  orchestration.

This is a movement refactor, not a behavior rewrite. Names may keep their
leading underscore when that minimizes churn.

## Data Flow

`cli.main()` parses args, validates identity, resolves the PR, acquires a PR
lock, switches into the PR worktree, then runs the existing phases:

1. mark needs-review and optionally rebase,
2. run Codex review/fix rounds,
3. run local quality gates before commit/push,
4. wait for required checks,
5. run CI repair rounds if needed,
6. optionally rebase again,
7. optionally approve and merge.

The command boundary remains `process._run_command()`. Modules call through
that helper rather than creating new subprocess wrappers.

## Testing

Add `unittest` coverage for pure helper seams before moving production code:

- marker extraction and review-pass inference,
- branch slugging and PR head ref formatting,
- check bucket/failure formatting,
- round-number and integer parser validation,
- command output truncation and completed-process formatting.

Add compatibility verification by running:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile codex_ralph_wiggum_loop.py ralph_loop/*.py
python3 codex_ralph_wiggum_loop.py --help
```

## Risks

The current worktree is dirty before this refactor begins. The module split will
therefore make the current uncommitted script behavior harder to separate from
the mechanical file movement. The mitigation is to keep the wrapper thin,
preserve function names where practical, add focused tests, and avoid unrelated
cleanup.

## Self-Review

- No placeholders remain.
- Scope is limited to a module split plus focused helper tests.
- The CLI compatibility requirement is explicit.
- Dirty-worktree risk is called out and accepted as the working base for this
  requested immediate implementation.
