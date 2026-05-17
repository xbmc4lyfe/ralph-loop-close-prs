# End-to-End Guide

This repository is centered on one CLI entry point:
`codex_ralph_wiggum_loop.py`. The implementation lives in the `ralph_loop/`
package.

The script takes an open GitHub pull request and drives it through a repeatable sequence:

1. Resolve the target PR and branch.
2. Move work onto a dedicated per-PR git worktree.
3. Run Codex review/fix rounds.
4. Run local quality gates before every push.
5. Wait for GitHub checks.
6. If CI fails, run Codex CI repair rounds.
7. Rebase the branch onto the base branch again.
8. Optionally merge the PR.

This guide explains the current CLI behavior, including the assumptions it makes
and the control flow it follows.

## What the script is for

The script is opinionated automation for a very specific workflow:

- a PR already exists on GitHub
- the PR is open and not draft
- Codex is available locally as a CLI
- `gh` can inspect and merge the PR
- local verification uses `just ci` and `just test`
- commits are signed and attributed to a specific GitHub identity

It is not a general PR bot or a reusable library. It is a local operator script for driving one PR at a time through review, repair, CI, and merge.

## High-level execution flow

At a high level, `main()` does this:

1. Parse CLI arguments.
2. Validate git and GitHub identity requirements.
3. Resolve the PR from `--pr` or the current branch.
4. Acquire a per-PR filesystem lock.
5. Verify the PR is open, not draft, and targets the expected base branch.
6. Create or reuse the dedicated worktree for the PR head branch.
7. Refuse to continue if that worktree is dirty.
8. Mark the PR as needing review.
9. Optionally rebase the branch onto the base branch before any repair work.
10. Run the review/fix loop until `/review` passes.
11. Wait for required GitHub checks to go green.
12. If checks fail, run the CI repair loop until they go green.
13. Optionally rebase again and wait for checks again.
14. Optionally prepare and merge the PR.

## Preconditions and environment assumptions

The script expects all of the following:

- `git`, `gh`, `codex`, and `just` are installed and on `PATH`
- `gh` is authenticated as `xbmc4lyfe`
- git commit signing is enabled
- a git signing key is configured
- the SSH identity files used by the script already exist

The script also enforces and/or sets this runtime identity:

- `git config user.name xbmc4lyfe`
- `git config user.email xbmc4lyfe@users.noreply.github.com`
- `git config core.sshCommand "ssh -i /Users/allen/.ssh/id_ed25519_xbmc4lyfe -o IdentitiesOnly=yes -o IdentityAgent=none"`

That means the script is intentionally tied to one operator identity.
Environment overrides for SSH key paths and the worktree root expand a leading
`~`. `RALPH_SSH_COMMAND` is parsed with normal shell quoting so leading `~`
path tokens, including `key=~/path` values, can be expanded before writing
`core.sshCommand`; commands that cannot be parsed are left as provided.

## CLI surface

The script exposes these arguments:

- `--pr <number>`: target a specific positive PR number; if omitted, the current branch is used as the PR reference
- `--base <branch>`: expected PR base branch, default `main`
- `--max-review-rounds <n>`: cap the review/fix loop; `0` means unbounded
- `--max-ci-rounds <n>`: cap the CI repair loop; `0` means unbounded
- `--max-local-quality-rounds <n>`: cap local repair rounds for `just ci` / `just test`; `0` means unbounded
- `--poll-seconds <n>`: interval for polling GitHub checks
- `--checks-timeout-seconds <n>`: timeout for one wait-for-checks cycle
- `--model <name>`: optional Codex model override
- `--skip-rebase`: skip the initial and final rebase
- `--skip-merge`: stop after the branch is green
- `--dry-run`: resolve and validate the PR, then stop before local or remote mutations
- `--worktree-root <path>`: override the root directory for PR worktrees
- `--max-wall-clock-seconds <n>`: cap total runtime at loop, subprocess, local-quality repair, and check-polling boundaries; `0` means unbounded
- `--json-log <path>`: append structured JSON-lines events to a file

The integer parsers `_nonneg_int()` and `_pos_int()` enforce valid numeric flag values before execution starts.

`--dry-run` is intentionally preflight-only. It reads the current branch when
needed, fetches PR metadata with `gh pr view`, applies the normal PR metadata
checks, prints the planned target plus a downstream simulation plan, and exits.
It does not update git config, acquire the PR lock, create or reuse a worktree,
change directories, add labels, run Codex, run quality gates, wait for checks,
rebase, push, reset generated changes, approve, merge, or delete branches.

## PR resolution and validation

The script resolves the PR with `_pr_view()`, which runs:

- `gh pr view <ref> --json number,url,state,headRefName,baseRefName,isDraft,isCrossRepository`

If GitHub reports API rate-limit exhaustion for a `gh` request, Ralph sleeps in
that process for five minutes and retries the same request. Other known
transient `gh` failures still use the shorter exponential retry path.

`<ref>` is either:

- the explicit PR number from `--pr`, or
- the current git branch name if `--pr` was omitted

After fetching PR metadata, `main()` verifies:

- the PR number is valid
- the PR state is `OPEN`
- the PR is not draft
- the PR is not a fork / cross-repository PR
- the PR base branch matches `--base`
- the head branch exists
- the head branch is not the same as the base branch

If any of those checks fail, execution stops immediately.

## Per-PR lock behavior

Before it starts changing anything, the script acquires a lock with `_acquire_loop_lock(pr_number=...)`.

That lock is keyed by PR number and prevents concurrent loop runs for the same PR. The lock file is persistent and the advisory lock is released in `finally`, so normal failures still unwind cleanly without creating an unlink race.

There is also a secondary concurrency guard during branch checkout/worktree
creation. If git reports that the branch is already in use by another worktree,
the script prints the loop-already-running message and exits with a dedicated
loop-owned status code. Fan-out supervisors treat that code like a long-backoff
condition instead of respawning the same PR immediately.

## Worktree management

The script does its repair work inside a dedicated PR worktree instead of the repo where it was launched.

The worktree root defaults to:

- `/private/tmp/codex-ralph-worktrees`

The worktree path is derived from the PR number and a slugged branch name.

The relevant helpers are:

- `_worktree_path(...)`
- `_worktree_for_branch(...)`
- `_ensure_pr_worktree(...)`

The worktree flow is:

1. Determine the desired worktree path.
2. Fetch the PR branch or PR head ref with `_fetch_pr_branch_or_head(...)`.
3. If the branch is already checked out at a different worktree path, exit cleanly instead of running destructive cleanup outside the dedicated path.
4. If the target path already exists, verify it is registered for the launching
   repo, verify its `origin` remote, restore the expected branch if needed, abort
   any interrupted rebase, and sync it to the fetched PR head only if doing so
   does not drop local commits.
5. Otherwise create the worktree without resetting an existing local branch. If
   git reports the desired path is missing but still registered, prune stale
   worktree registrations and retry that create operation once.

After the worktree is ready, `main()` calls `os.chdir(worktree)` and all later git operations happen there.

The script then runs `_ensure_runtime_identity()` again so the worktree-local git config is aligned with the expected operator identity.

## Dirty-worktree guard

Once inside the PR worktree, the script checks `_working_tree_dirty()`.

If the worktree is dirty, the run aborts immediately. The script requires a clean starting point because later recovery paths can use hard resets and `git clean -fdx`.

That is an important operational constraint: the dedicated worktree is treated as disposable automation state, not as a place for manual edits.

## Review label and initial rebase

Before starting review automation, the script calls `_mark_pr_needs_review(pr_target)`.

If `--skip-rebase` is not set, it then performs an initial rebase through `_rebase_onto_base(branch, base)`, which does:

1. `git fetch origin <base>`
2. `git rebase origin/<base>`
3. `git push --force-with-lease origin <branch>`

This means the script may rewrite the PR branch before any review or CI repair work begins.

## Codex execution model

Every Codex-driven round uses `_codex_exec_with_marker(...)`.

That helper:

1. Creates a temporary directory.
2. Runs `codex exec` with `--ask-for-approval never`.
3. Writes Codex's final response to a temp file via `-o`.
4. Reads the file back.
5. Extracts a yes/no marker from the final message with `_extract_yes_no_marker(...)`.

The prompt is passed to Codex on stdin, and command logging replaces the stdin
marker with `<codex prompt on stdin>` so review comments, failure summaries, and
private URLs are not printed into Ralph logs as one giant command line.

If `codex exec` exits non-zero, the helper raises `CommandError` before marker
extraction. A captured last-message may be included in the error for
diagnostics, but it is not trusted as a successful marker response.

The markers are how the outer loop decides whether Codex believes a round passed or produced a usable fix.

The script currently uses `danger-full-access` sandbox mode for these Codex runs.

## Review/fix loop

The review loop is the first major phase after setup.

Each round calls `_run_review_fix_round(round_number, base, model)`.

That helper instructs Codex to:

1. run `/review --base <base>`
2. fix actionable issues if any exist
3. avoid commit and push operations
4. run `/review --base <base>` exactly one more time
5. respond with `REVIEW_PASS=yes` or `REVIEW_PASS=no`

If the marker is missing, the script tries `_infer_review_pass_without_marker(...)` as a fallback by inspecting the final text for phrases like "no findings" or "issues remain".

### When review does not pass

If Codex reports `REVIEW_PASS=no`, the script still tries to preserve useful fixes by calling `_commit_and_push(...)`.

In that path:

- no pre-push review gate is required
- local quality gates still run before any commit or push
- if the generated changes are discarded or no useful changes exist, the loop retries with a fresh Codex session

This lets the script carry partial progress across review rounds.

### When review passes

If Codex reports `REVIEW_PASS=yes`, the script still runs `_commit_and_push(...)` before finishing the phase.

In this path:

- local quality gates still run
- if local quality required a repair round, the script can re-enable a review gate before pushing
- the phase only exits once review passed and any resulting changes have been committed or intentionally skipped

If the review loop never reaches a pass state before the configured round cap, the script raises `CommandError`.

## Commit and push behavior

`_commit_and_push(...)` is the gatekeeper for any Codex-generated changes.

It handles four concerns:

1. deciding whether there is anything new to preserve
2. optionally running a pre-push review gate
3. running local quality gates
4. committing and pushing if the branch is ready

### Detecting whether there is work to save

The function compares:

- whether the working tree is dirty
- whether `HEAD` changed relative to `pre_round_sha`

If neither changed, it returns `no_changes`.

### Optional pre-push review gate

If `require_review_gate=True`, it runs `_run_pre_push_review_gate(...)`.

That helper tells Codex to:

- run `/review --base <base>` exactly once
- not modify files
- return `PRE_PUSH_REVIEW_OK=yes` or `PRE_PUSH_REVIEW_OK=no`

This review gate runs Codex with a read-only sandbox.

If the review gate fails, the script discards generated changes with `_reset_generated_changes(pre_round_sha)` and returns `discarded`.

### Local quality gates

Before every commit/push, `_run_local_quality_gates()` runs:

- `just ci`
- `just test`

If both pass, the function proceeds.

If either fails, the script captures bounded combined failure output, truncates
it for the repair prompt/logging, and enters the local quality repair flow.

### Local quality repair rounds

The helper `_run_local_quality_fix_round(...)` asks Codex to:

- inspect the local failure output
- fix the underlying issue in the repository
- run the relevant local verification
- return `LOCAL_QUALITY_FIX_READY=yes` or `LOCAL_QUALITY_FIX_READY=no`

If Codex says `yes`, `_commit_and_push(...)` retries the local quality gates.

If Codex says `no`, or if the configured local repair round limit is exhausted, the script resets generated changes back to the pre-round SHA and either returns `discarded` or raises `CommandError`.

The global wall-clock deadline is checked during this repair loop, and the
subprocess helper interrupts `just`, `codex`, `git`, and `gh` commands when the
remaining run budget expires.

### Reset behavior for generated changes

The discard path uses `_reset_generated_changes(...)`, which can run:

- `git reset --hard <target>`
- `git clean -fdx`

That cleanup removes ignored generated files as well as ordinary untracked files
inside the dedicated PR worktree.

This is destructive inside the PR worktree. It is designed to throw away automation-generated changes that are not worth keeping.

### Commit format

When the working tree is dirty after all gates pass, the script commits with:

- `git add -u`
- `git add -- <non-generated untracked paths>`
- `git commit --signoff -S -m "fix: codex loop <iteration label>" -m <coauthor line>`

If the working tree is clean but new commits already exist, it skips creating another commit and just pushes.

Pushes are always sent to:

- `git push origin <branch>`

## Waiting for GitHub checks

After the review loop succeeds, the script waits for GitHub checks through `_wait_for_required_checks_green(...)`.

Single-PR runs poll by PR number instead of head branch so a numeric branch name
cannot be confused with a different GitHub PR number. Internally this calls the
required-check helper for that PR reference, which prefers:

- `gh pr checks --required <pr-number>`

and falls back to:

- `gh pr checks <pr-number>`

if no required checks are reported.

For each poll cycle, it summarizes the current check buckets, such as:

- `pass`
- `pending`
- `fail`
- `cancel`
- `skipping`

The result rules are:

- if no checks are reported yet, keep polling until checks appear or the timeout expires
- if required checks are reported and all buckets are `pass` or `skipping`, treat the branch as green
- if only fallback optional checks are reported and all buckets are `pass` or `skipping`, keep polling through the no-checks grace window before accepting the fallback as green
- if any check is still `pending`, keep polling
- if checks are failing and none are pending, return failure to the CI loop
- if polling exceeds the timeout, raise `CommandError`

`gh pr checks` exit code 8 is treated as pending state, including the case
where the command returns no JSON output yet.

## CI repair loop

If the checks come back failed, the script enters the CI repair loop.

Each round calls `_run_ci_fix_round(round_number, checks, model)`.

That helper:

1. extracts the failing or canceled checks
2. formats a small failure summary including names, states, workflows, and links
3. asks Codex to diagnose the failures, using GitHub logs as needed
4. asks Codex to fix the underlying issue locally
5. asks Codex to return `CI_FIX_READY=yes` or `CI_FIX_READY=no`

If Codex says `yes`, the script runs `_commit_and_push(...)` with the review gate enabled before push.

If that push succeeds, the script returns to waiting for GitHub checks.

If Codex says `no`, or the generated changes are discarded, the script retries the next CI round with a fresh Codex session.

If the configured CI round cap is reached without green checks, the script raises `CommandError`.

## Final rebase and second check wait

If `--skip-rebase` is not set, the script does a second `_rebase_onto_base(...)` after CI is green.

That is followed by another `_wait_for_required_checks_green(...)` call.

If checks fail after this final rebase, the script stops with `CommandError("Required checks failed after rebase.")`.

So "green before final rebase" is not enough. The branch must also survive the final rebase cleanly.

## Merge preparation and merge

If `--skip-merge` is not set, the script finishes by:

1. calling `_prepare_pr_for_merge(str(pr_number))`
2. calling `_merge_pr(pr_target)`

`_merge_pr(...)`:

1. captures the current `HEAD` SHA
2. signs off / approves the PR if needed
3. runs `gh pr merge <pr> --rebase --delete-branch --match-head-commit <sha>`

That `--match-head-commit` guard makes the merge conditional on the head SHA still matching the local expectation.

## How retries work

The three main retrying phases are:

- review rounds
- local quality repair rounds
- CI repair rounds

Review and CI loops advance explicit round counters. If a max round count is
`0`, the loop is intentionally unbounded. If it is positive, the loop stops at
that cap. Local quality repair rounds use the same bounded/unbounded counter
inside `_commit_and_push(...)`.

That means the default behavior can continue indefinitely unless one of these happens:

- the phase succeeds
- a command errors out
- a configured timeout is hit
- the operator interrupts the process

Successful single-PR runs emit a final telemetry line to stderr with:

- review round count
- CI wait count
- CI repair round count
- local quality repair round count
- review phase duration
- CI phase duration
- total wall-clock duration

If `--json-log <path>` is set, Ralph appends the same step stream as structured
JSON-lines records. Dry-run validation and simulation steps use
`dry_run.*` event names, and the final successful telemetry record uses
`run.telemetry`.

## Failure model

The script uses `CommandError` for expected operational failures.

Examples include:

- PR state problems
- branch/worktree mismatches
- exhausted repair loop caps
- final check failures
- command failures from `git`, `gh`, `just`, or `codex`

At the process boundary, `__main__` catches `CommandError`, prints `ERROR: ...` to stderr, and exits with status `1`.

Codex environment failures use a dedicated exit code so fan-out supervisors can
use a longer respawn backoff. Concurrency-style early exits for PRs already
owned by another Ralph loop also use a dedicated status code for the same
long-backoff behavior.

## Filesystem and state side effects

A normal run may:

- create or reuse a worktree under `/private/tmp/codex-ralph-worktrees`
- update git config in the active repo/worktree
- create commits
- push commits to GitHub
- force-push during rebases
- add review-related labels or approvals to the PR
- merge the PR and delete the branch remotely
- reset and clean the worktree when throwing away generated changes

This is not a read-only tool. It mutates both local git state and remote GitHub state.
Use `--dry-run` when you only want the safe PR preflight; it exits before the
mutating worktree, git, GitHub, Codex, and merge phases.

## Operational caveats

A few current characteristics matter when running it:

- the script assumes the dedicated PR worktree is safe to hard-reset and clean
- `os.chdir(worktree)` is process-global and remains in effect for later helpers
- `codex exec` failures are fatal even when a partial last-message exists
- many loops are unbounded by default unless you set explicit caps
- known transient GitHub CLI failures are retried automatically before surfacing
  as operational errors

Those are not hidden behaviors. They are part of the current design.

## Minimal usage examples

Run against a specific PR and stop before merge:

```bash
python3 codex_ralph_wiggum_loop.py --pr 123 --base main --skip-merge
```

Run against the PR associated with the current branch:

```bash
python3 codex_ralph_wiggum_loop.py
```

Run with explicit round caps:

```bash
python3 codex_ralph_wiggum_loop.py \
  --pr 123 \
  --max-review-rounds 5 \
  --max-ci-rounds 3 \
  --max-local-quality-rounds 2
```

## Reading order in the code

If you want to inspect the script source efficiently, this is a sensible reading order:

1. `_parse_args()`
2. `main()`
3. `_ensure_pr_worktree(...)`
4. `_run_review_fix_round(...)`
5. `_commit_and_push(...)`
6. `_run_local_quality_gates()` and `_run_local_quality_fix_round(...)`
7. `_wait_for_required_checks_green(...)`
8. `_run_ci_fix_round(...)`
9. `_rebase_onto_base(...)`
10. `_merge_pr(...)`

That sequence mirrors the real orchestration path closely enough that you can understand the script without reading every helper in source order.
