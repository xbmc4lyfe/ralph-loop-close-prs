# Ralph Loop

This repository currently contains one main script:

- `codex_ralph_wiggum_loop.py`

It is a Python automation loop for taking an open GitHub PR, running Codex-driven review and repair cycles, waiting for CI, optionally repairing CI failures, then optionally rebasing and merging the PR.

## Current Repo Shape

- Source code is currently a single file: `codex_ralph_wiggum_loop.py`
- The repo is not broken into modules yet
- Running the script can create a local `__pycache__/` directory

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
- The following files must exist:
  - `/Users/allen/.ssh/id_ed25519_xbmc4lyfe`
  - `/Users/allen/.ssh/id_ed25519_signing.pub`

The script also sets:

- `git config user.name xbmc4lyfe`
- `git config user.email xbmc4lyfe@users.noreply.github.com`
- `git config core.sshCommand "ssh -i /Users/allen/.ssh/id_ed25519_xbmc4lyfe -o IdentitiesOnly=yes -o IdentityAgent=none"`

## Worktree Behavior

Per-PR worktrees are created under:

- `/private/tmp/codex-ralph-worktrees`

The worktree path format is:

- `pr-<pr-number>-<slugged-branch-name>`

The script:

- reuses an existing worktree for the branch if one already exists
- acquires a per-PR lock at `/tmp/codex-ralph-loop-pr-<pr-number>.lock`
- changes into the PR worktree before doing repair work

## Local Quality Gates

Before committing and pushing generated changes, the script runs:

- `just ci`
- `just test`

If either fails, it asks Codex to repair the local failure before retrying.

If generated changes are considered not useful, it resets the worktree with:

- `git reset --hard HEAD`
- `git clean -fd`

That behavior is destructive inside the PR worktree.

## Merge Behavior

If merge is enabled, the script will:

- add the `needs review` label to the PR
- approve the PR as the authenticated GitHub user if needed
- merge with `gh pr merge --rebase --delete-branch --match-head-commit <sha>`

## CLI

Basic help:

```bash
python3 codex_ralph_wiggum_loop.py --help
```

Important flags:

- `--pr <number>`: target PR number
- `--base <branch>`: base branch, default `main`
- `--max-review-rounds <n>`: limit review/fix rounds, `0` means unlimited
- `--max-ci-rounds <n>`: limit CI repair rounds, `0` means unlimited
- `--max-local-quality-rounds <n>`: limit local `just ci` or `just test` repair rounds
- `--poll-seconds <n>`: CI polling interval, default `20`
- `--checks-timeout-seconds <n>`: timeout for one CI wait cycle, default `5400`
- `--model <name>`: pass a model override to `codex exec`
- `--skip-rebase`: skip both the initial and final rebase steps
- `--skip-merge`: stop after CI is green without merging
- `--worktree-root <path>`: override the worktree root

Example:

```bash
python3 codex_ralph_wiggum_loop.py --pr 123 --base main --skip-merge
```

## Notes

- If you do not pass `--pr`, the script uses the current branch name as the PR reference.
- This repo currently has no modular package structure; the logic is all in one Python file.
- The only generated artifact currently seen in the repo is `__pycache__/`.
