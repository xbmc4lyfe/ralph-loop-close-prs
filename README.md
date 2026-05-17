# Ralph Loop

This repository contains a compatibility script plus the implementation package:

- `codex_ralph_wiggum_loop.py`
- `ralph_loop/`
- `GUIDE.md`

It is a Python automation loop for taking an open GitHub PR, running Codex-driven review and repair cycles, waiting for CI, optionally repairing CI failures, then optionally rebasing and merging the PR.

For a full end-to-end walkthrough of the current behavior, see `GUIDE.md`.

## Codebase Layout

- `codex_ralph_wiggum_loop.py`: executable compatibility entry point. Keep this
  path working for existing shell history and docs.
- `ralph_loop/cli.py`: argument parsing, signal handling, and top-level
  orchestration.
- `ralph_loop/config.py`: environment-derived defaults and fixed labels.
- `ralph_loop/errors.py`: shared `CommandError`.
- `ralph_loop/process.py`: subprocess execution, command rendering, deadline
  enforcement, step logging, and bounded output formatting.
- `ralph_loop/git_ops.py`: git branch, config, status, reset, and rebase
  helpers.
- `ralph_loop/gh_ops.py`: GitHub CLI retry, JSON, PR metadata, labels, reviews,
  checks, and merge helpers.
- `ralph_loop/identity.py`: GitHub/git identity and signing setup/validation.
- `ralph_loop/worktrees.py`: per-PR lock handling and PR worktree
  creation/reuse.
- `ralph_loop/codex_agent.py`: `codex exec` calls, marker parsing, and Codex
  review/repair prompts.
- `ralph_loop/checks.py`: GitHub check bucket formatting and required-check
  polling.
- `ralph_loop/quality.py`: local `just ci` / `just test` gates and commit/push
  retry flow.
- `ralph_loop/runtime.py`: wall-clock deadline helper.
- `tests/`: pytest coverage for CLI orchestration, Codex prompts, local quality
  gates, git/GitHub helpers, identity setup, process handling, worktrees,
  check polling, and pure helper behavior.
- `GUIDE.md`: longer control-flow and safety walkthrough.
- Running the script or tests can create local `__pycache__/` directories.

## What The Script Does

The script automates this workflow:

1. Validate GitHub and git identity.
2. Resolve a PR and its head branch.
3. Create or reuse a dedicated git worktree for that PR.
4. Run a Codex `/review` loop and let Codex fix issues.
5. Run local quality gates: `just ci` and `just test`.
6. Commit and push generated fixes.
7. Wait for required GitHub checks.
8. If checks fail, ask Codex to fix CI failures and repeat.
9. Optionally rebase onto the base branch again.
10. Optionally approve and merge the PR.

## Hard Requirements

The script has several baked-in assumptions. It is not generic.

- `gh` must be installed and authenticated as `xbmc4lyfe`
- `git` must be installed
- `codex` must be installed and available on `PATH`
- `just` must be installed and available on `PATH`
- `git config user.name` must be `xbmc4lyfe`
- `git config user.email` must be `xbmc4lyfe@users.noreply.github.com`
- `git config commit.gpgsign` must be enabled
- `git user.signingkey` must already be configured
- the PR must not be a fork PR; Ralph pushes fixes to `origin <branch>`
- The following files must exist:
  - `/Users/allen/.ssh/id_ed25519_xbmc4lyfe`
  - `/Users/allen/.ssh/id_ed25519_signing.pub`

The script also sets:

- `git config user.name xbmc4lyfe`
- `git config user.email xbmc4lyfe@users.noreply.github.com`
- `git config core.sshCommand "ssh -i /Users/allen/.ssh/id_ed25519_xbmc4lyfe -o IdentitiesOnly=yes -o IdentityAgent=none"`

Environment path overrides such as `RALPH_SSH_AUTH_KEY`,
`RALPH_SSH_SIGNING_KEY`, and `RALPH_WORKTREE_ROOT` expand a leading `~`.
Path tokens inside `RALPH_SSH_COMMAND` are also expanded when the command can be
parsed with normal shell quoting.

## Worktree Behavior

Per-PR worktrees are created under:

- `/private/tmp/codex-ralph-worktrees`

The worktree path format is:

- `pr-<pr-number>-<slugged-branch-name>`

The script:

- reuses the expected dedicated worktree path if it already exists
- aborts any interrupted rebase in a reused PR worktree before syncing it to the
  fetched PR head
- prunes a stale git registration and retries once if git reports the desired
  worktree path is missing but still registered
- scopes fan-out stale worktree cleanup to the launching repo's `origin` remote,
  so another repo's supervisor cannot remove active PR worktrees that happen to
  share the same worktree root
- exits with the loop-already-running code if the PR branch is checked out in
  any other worktree, so fan-out supervisors back off instead of tight-looping
- acquires a persistent per-PR advisory lock at `/tmp/codex-ralph-loop-pr-<pr-number>.lock`
- changes into the PR worktree before doing repair work

## Local Quality Gates

Before committing and pushing generated changes, the script runs:

- `just ci`
- `just test`

If either fails, it asks Codex to repair the local failure before retrying.
The global wall-clock cap is checked during this repair flow, and subprocesses
are interrupted when the remaining wall-clock budget expires.

If generated changes are considered not useful, it resets the worktree with:

- `git reset --hard HEAD`
- `git clean -fdx`

That behavior is destructive inside the PR worktree and also removes ignored
generated files there.

## GitHub Check Waiting

Ralph waits for GitHub checks to appear and finish before treating a branch as
green. Empty check results and `gh pr checks` exit code 8 are treated as pending
state, not success. If checks never appear or never leave pending state before
`--checks-timeout-seconds`, the run stops with an error.

If GitHub reports no required checks but optional checks are already green,
Ralph keeps polling through the no-checks grace window before accepting the
optional-check fallback. This prevents a fresh commit from advancing before
required checks have had time to appear.

## Merge Behavior

If merge is enabled, the script will:

- add the `needs review` label to the PR
- approve the PR as the authenticated GitHub user if needed, unless that user
  authored the PR
- merge with `gh pr merge --rebase --delete-branch --match-head-commit <sha>`

## CLI

Basic help:

```bash
python3 codex_ralph_wiggum_loop.py --help
```

Important flags:

- `--pr <number>`: target a positive PR number
- `--base <branch>`: base branch, default `main`
- `--max-review-rounds <n>`: limit review/fix rounds, `0` means unlimited
- `--max-ci-rounds <n>`: limit CI repair rounds, `0` means unlimited
- `--max-local-quality-rounds <n>`: limit local `just ci` or `just test` repair rounds
- `--poll-seconds <n>`: CI polling interval, default `20`
- `--checks-timeout-seconds <n>`: timeout for one CI wait cycle, default `5400`
- `--model <name>`: pass a model override to `codex exec`
- `--skip-rebase`: skip both the initial and final rebase steps
- `--skip-merge`: stop after CI is green without merging
- `--dry-run`: resolve and validate the PR, then stop before local or remote mutations
- `--worktree-root <path>`: override the worktree root
- `--max-wall-clock-seconds <n>`: cap total runtime, including subprocesses and local quality repair, `0` means unlimited
- `--json-log <path>`: append structured JSON-lines events to a file

`--dry-run` is a safe preflight mode. It does not update git config, create or
reuse worktrees, add labels, run Codex, run quality gates, rebase, push, reset,
approve, merge, or delete branches. It also prints a downstream simulation plan
showing the mutating phases Ralph would have attempted.

Example:

```bash
python3 codex_ralph_wiggum_loop.py --pr 123 --base main --skip-merge
```

## Notes

- If you do not pass `--pr`, the script uses the current branch name as the PR reference.
- The CLI remains available through `codex_ralph_wiggum_loop.py`.
- The only generated artifact currently seen in the repo is `__pycache__/`.
- Captured subprocess stdout/stderr are bounded and truncated before being
  returned or replayed to logs.
- `codex exec` prompts are sent on stdin and redacted in command logs, so they
  are not printed as giant argv lines.
- Successful single-PR runs emit a final telemetry line with review round count,
  CI wait count, CI repair round count, local quality repair round count,
  review duration, CI duration, and total wall-clock duration.
- When `--json-log` is set, step output, dry-run simulation events, and final
  telemetry are also appended as one JSON object per line.

## Small TODO

- Continue adding focused regression coverage when new Ralph failure modes are
  found in real or mocked PR runs.
