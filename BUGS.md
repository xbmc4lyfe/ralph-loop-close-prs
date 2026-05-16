# Ralph Loop Bug & Gap Report

Consolidated from the current bug notes plus a fresh read of the current
working tree. Duplicate and fixed historical findings are omitted. Current
runtime code is split between `codex_ralph_wiggum_loop.py` and `ralph_loop/`.

## HIGH

### H1. `git worktree add -B` can reset an unoccupied local PR branch before the dirty-worktree guard - `ralph_loop/worktrees.py`

`_ensure_pr_worktree()` creates a new PR worktree with
`git worktree add <path> -B <branch> <start-ref>` (`ralph_loop/worktrees.py:183`).
If `<branch>` already exists locally but is not checked out in any worktree,
`-B` resets that branch to `<start-ref>`. That can discard local-only commits on
the PR branch before Ralph has entered the dedicated worktree or checked whether
there is user work to preserve.

### H2. Existing worktree paths are reused without proving they belong to the launching repo - `ralph_loop/config.py`, `ralph_loop/worktrees.py`

The default worktree root is shared under the temp directory
(`ralph_loop/config.py:47`), and `_worktree_path()` uses only the PR number and
branch slug (`ralph_loop/worktrees.py:77`). When that path already exists,
`_ensure_pr_worktree()` only checks the branch name (`ralph_loop/worktrees.py:164`).
It does not prove the directory is a registered worktree for the launching repo,
nor that its `origin` remote matches. A stale clone or another repo with the same
PR/branch-shaped path can be reused, after which Ralph can reset, clean, and
push from the wrong checkout.

### H3. PR approval can be applied to an unverified head before the merge SHA guard runs - `ralph_loop/gh_ops.py`

`_prepare_pr_for_merge()` approves the PR before `_merge_pr()` runs the guarded
merge (`ralph_loop/gh_ops.py:222`). `_merge_pr()` also calls `_sign_off_pr()`
before `gh pr merge --match-head-commit <sha>` (`ralph_loop/gh_ops.py:274`). If
the remote PR head changes after the last check wait but before approval, Ralph
can leave an approval on a head it did not validate even though the later merge
is blocked by the match-head guard.

### H4. Reused PR worktrees can force-push stale local branch state - `ralph_loop/worktrees.py`, `ralph_loop/git_ops.py`

When the expected PR worktree path already exists, `_ensure_pr_worktree()` checks
only that `HEAD` is on the expected branch (`ralph_loop/worktrees.py:164`) and
then fetches the PR branch in that worktree (`ralph_loop/worktrees.py:179`). It
does not reset, fast-forward, or otherwise prove the local branch contains the
freshly fetched `origin/<branch>`. The default initial rebase then rebases the
stale local branch and pushes with `--force-with-lease`
(`ralph_loop/git_ops.py:94`, `ralph_loop/git_ops.py:101`). Because the fetch just
updated the lease reference, that push can still succeed while dropping remote
commits that were never present in the reused worktree.

### H5. Merge does not revalidate PR metadata after the long repair flow - `ralph_loop/cli.py`, `ralph_loop/gh_ops.py`

PR state, draft status, fork status, base branch, and head branch are validated
once near startup (`ralph_loop/cli.py:236`, `ralph_loop/cli.py:238`). After the
review, local-quality, CI, and optional rebase phases, merge proceeds without
re-reading PR metadata (`ralph_loop/cli.py:414`). `_merge_pr()` guards only the
local head SHA through `--match-head-commit` (`ralph_loop/gh_ops.py:299`). If the
PR is retargeted to another base, marked draft, or otherwise changed during a
long run, Ralph can approve or merge under assumptions that are no longer true.

### H6. Git identity enforcement does not prove pushes use the expected GitHub account - `ralph_loop/identity.py`, `ralph_loop/quality.py`, `ralph_loop/git_ops.py`

`_ensure_runtime_identity()` validates `gh` login and writes `core.sshCommand`
(`ralph_loop/identity.py:82`, `ralph_loop/identity.py:101`), but push paths still
use the repo's plain `origin` remote (`ralph_loop/quality.py:211`,
`ralph_loop/git_ops.py:101`). If `origin` is HTTPS, Git ignores
`core.sshCommand` and uses credential-helper state instead. Ralph can therefore
push or force-push with a different GitHub identity than `RALPH_GH_USER`, while
the runtime identity checks appear to have passed.

## MEDIUM

### M1. The global wall-clock cap does not bound subprocesses or local-quality repair - `ralph_loop/cli.py`, `ralph_loop/process.py`, `ralph_loop/quality.py`

`--max-wall-clock-seconds` is described as a global run timeout
(`ralph_loop/cli.py:123`), but `_run_command()` passes no subprocess timeout
(`ralph_loop/process.py:31`) and `_commit_and_push()` has an internal retry loop
with no deadline checks (`ralph_loop/quality.py:53`). A hanging `codex`, `just`,
`git`, or `gh` command can still block forever.

### M2. Non-zero `codex exec` exits are downgraded too aggressively - `ralph_loop/codex_agent.py`

`_codex_exec_with_marker()` continues when `codex exec` exits non-zero as long
as a last-message file exists (`ralph_loop/codex_agent.py:52`). A crashed or
interrupted Codex process can still drive marker inference and allow the caller
to commit or continue.

### M3. GitHub retry coverage is incomplete - `ralph_loop/gh_ops.py`

The retry wrapper covers JSON-style reads, but high-impact mutating operations
still call `_run_command()` directly: adding labels (`ralph_loop/gh_ops.py:148`),
creating labels (`ralph_loop/gh_ops.py:161`), approving PRs
(`ralph_loop/gh_ops.py:199`), and merging (`ralph_loop/gh_ops.py:278`). A
transient network or GitHub CLI failure in those paths still aborts the whole
run after earlier local and remote side effects.

### M4. Captured process output is still materialized fully in memory - `ralph_loop/process.py`

`_run_command()` spools stdout/stderr while the subprocess runs, but then reads
both complete streams back into strings and stores them on a
`CompletedProcess` (`ralph_loop/process.py:69`). Very large `codex`, `just`, or
GitHub log output can still spike memory and then be echoed in full.

### M6. `git add -A` can sweep unrelated generated artifacts into commits - `ralph_loop/quality.py`

After Codex and the local gates run, `_commit_and_push()` stages the entire
worktree with `git add -A` (`ralph_loop/quality.py:103`). If verification
creates unignored artifacts such as coverage reports, temp outputs, or logs,
they can be committed with the actual fix.

### M8. Environment path overrides are not user-expanded - `ralph_loop/config.py`, `ralph_loop/identity.py`

Default SSH paths are expanded, but environment overrides are not
(`ralph_loop/config.py:18`, `ralph_loop/config.py:24`). `_ensure_runtime_identity()`
checks those raw strings (`ralph_loop/identity.py:73`) and writes them into git
config (`ralph_loop/identity.py:101`, `ralph_loop/identity.py:111`). Values such
as `RALPH_SSH_AUTH_KEY=~/.ssh/key` fail unexpectedly or create an unusable
`core.sshCommand`.

### M10. Existing PR approval detection ignores review ordering and dismissal - `ralph_loop/gh_ops.py`

`_pr_has_user_approval()` returns true for any historical `APPROVED` review by
the active user (`ralph_loop/gh_ops.py:136`). It does not consider a later
changes-requested review, dismissal, or whether that approval applies to the
current head SHA. Ralph may skip a needed approval or report misleading merge
readiness.

### M11. Required-check polling can go green on optional checks before required checks appear - `ralph_loop/gh_ops.py`, `ralph_loop/checks.py`

`_required_checks()` falls back to all checks whenever
`gh pr checks --required` reports no required checks (`ralph_loop/gh_ops.py:267`).
`_wait_for_required_checks_green()` then returns success if those fallback checks
are only `pass` or `skipping` (`ralph_loop/checks.py:81`). A fresh commit can
therefore advance toward approval or merge when optional checks have reported
success but required checks have not appeared yet.

### M12. Explicit `--pr` runs still require a current branch - `ralph_loop/cli.py`

`main()` calls `_git_branch()` before it decides whether `--pr` was supplied
(`ralph_loop/cli.py:162`). A command such as
`python3 codex_ralph_wiggum_loop.py --pr 123` can fail from a detached checkout
even though the current branch is not needed to resolve an explicit PR number.

### M13. `--max-ci-rounds` can abort immediately after pushing the allowed CI fix - `ralph_loop/cli.py`

The CI loop treats `--max-ci-rounds` as total wait/fix loop iterations
(`ralph_loop/cli.py:299`), but the CLI describes it as the cap on CI repair
rounds. With `--max-ci-rounds 1`, Ralph can see failing checks, run one CI fix,
commit and push it (`ralph_loop/cli.py:324`), then exit the loop and raise
`CI loop exhausted 1 rounds without green checks` (`ralph_loop/cli.py:348`)
without waiting for the pushed fix's checks.

### M14. Marker parsing accepts quoted, negated, or prefixed marker text - `ralph_loop/codex_agent.py`

`_extract_yes_no_marker()` uses unanchored `re.findall()` and accepts the last
marker-looking substring anywhere in the final message
(`ralph_loop/codex_agent.py:15`). Text such as
`I cannot return REVIEW_PASS=yes because issues remain` or `OLD_REVIEW_PASS=yes`
can be parsed as a successful marker even though the prompt contract requires
exactly one marker line.

### M15. Codex-created local commits can be pushed unchanged - `ralph_loop/quality.py`, `ralph_loop/codex_agent.py`

`_commit_and_push()` treats a clean worktree with `HEAD != pre_round_sha` as work
to preserve (`ralph_loop/quality.py:55`). In that path it skips the controlled
`git commit --signoff -S ...` block and pushes the existing commit
(`ralph_loop/quality.py:120`, `ralph_loop/quality.py:125`). The Codex prompts say
not to commit or push (`ralph_loop/codex_agent.py:92`,
`ralph_loop/codex_agent.py:177`, `ralph_loop/codex_agent.py:215`), but the push
path does not enforce that contract.

### M16. Per-PR lock files follow symlinks in a predictable temp path - `ralph_loop/worktrees.py`

`_acquire_loop_lock()` builds a predictable lock path under the temp directory
(`ralph_loop/worktrees.py:49`) and opens it with `os.O_CREAT` but without a
no-follow or exclusive-create guard (`ralph_loop/worktrees.py:52`). A preexisting
symlink at that path can be opened and truncated when Ralph writes its PID
(`ralph_loop/worktrees.py:63`), clobbering another file writable by the Ralph
user.

### M17. Non-UTF-8 subprocess output can bypass the `CommandError` boundary - `ralph_loop/process.py`

When output is captured, `_run_command()` spools child stdout/stderr through
UTF-8 text files (`ralph_loop/process.py:42`) and reads them back with strict
decoding (`ralph_loop/process.py:64`). A command that emits non-UTF-8 bytes can
raise `UnicodeDecodeError`, including on exit code 0, which bypasses the
top-level operational error handling.

### M18. Command logging prints raw argv, including Codex prompts and failure summaries - `ralph_loop/process.py`, `ralph_loop/codex_agent.py`

`_run_command()` logs the rendered command before execution
(`ralph_loop/process.py:38`). Codex prompts are passed as a positional argv value
(`ralph_loop/codex_agent.py:45`), and local/CI repair prompts can include failure
summaries. This can expose large multi-line logs, private URLs, or token-bearing
arguments to stderr before any sensitive-field redaction policy is applied.

### M19. `gh pr checks` no-checks errors can bypass the intended pending-state wait - `ralph_loop/gh_ops.py`, `ralph_loop/checks.py`

`_gh_json_allow_empty()` only maps a caller-provided `empty_error_text` to an
empty check list (`ralph_loop/gh_ops.py:118`), and `_pr_checks()` passes only
`"no required checks reported"` (`ralph_loop/gh_ops.py:287`). Other GitHub CLI
no-checks messages from `gh pr checks` are raised as `CommandError` before
`_wait_for_required_checks_green()` can enter its empty-check polling path
(`ralph_loop/checks.py:62`). Immediately after a push, Ralph can therefore abort
instead of waiting for checks to appear.

### M20. CI polling and GitHub retry sleeps can overrun configured time caps - `ralph_loop/checks.py`, `ralph_loop/gh_ops.py`

The global deadline and per-check timeout are checked before each poll
(`ralph_loop/checks.py:60`, `ralph_loop/checks.py:64`,
`ralph_loop/checks.py:85`), but the following `time.sleep(poll_seconds)` calls
are not capped to the remaining time (`ralph_loop/checks.py:70`,
`ralph_loop/checks.py:91`). GitHub retry backoff also sleeps without consulting
the global deadline (`ralph_loop/gh_ops.py:56`). A run with a large
`--poll-seconds` or retry delay can exceed `--checks-timeout-seconds` or
`--max-wall-clock-seconds` before Ralph notices.

### M21. Bounded command capture can corrupt machine-readable command output - `ralph_loop/process.py`, `ralph_loop/gh_ops.py`, `ralph_loop/quality.py`

`_run_command()` always applies `_read_bounded_output()` to captured stdout and
stderr (`ralph_loop/process.py:120`). That same truncated string is then parsed
as JSON by GitHub helpers (`ralph_loop/gh_ops.py:68`, `ralph_loop/gh_ops.py:83`)
and as NUL-delimited file names by `_untracked_files_for_commit()`
(`ralph_loop/quality.py:49`). Large but valid `gh` JSON or large untracked-file
lists can be truncated before the caller sees them, causing false parse failures
or incomplete staging decisions.

### M22. Filtered generated artifacts can leave a dirty worktree on the successful no-changes path - `ralph_loop/quality.py`, `ralph_loop/cli.py`

`_stage_commit_changes()` deliberately filters common generated untracked files
out of staging (`ralph_loop/quality.py:58`). If those artifacts are the only
dirty paths, `_commit_and_push()` prints that no committable changes exist and
returns `no_changes` without cleaning them (`ralph_loop/quality.py:201`). In the
review-pass path, `main()` accepts `no_changes` as success and breaks out of the
review loop (`ralph_loop/cli.py:318`). The later CI/rebase/merge phases can then
run in a worktree that Ralph just left dirty.

### M23. `codex exec` inherits piped stdin and can append it to Ralph prompts - `ralph_loop/codex_agent.py`, `ralph_loop/process.py`

`_codex_exec_with_marker()` passes Ralph's prompt as a positional argument to
`codex exec` (`ralph_loop/codex_agent.py:45`), and `_run_command()` does not set
`stdin` when launching subprocesses (`ralph_loop/process.py:96`). Current
`codex exec --help` says that if stdin is piped while a prompt is also supplied,
stdin is appended as a `<stdin>` block. Running Ralph from a pipeline or wrapper
with inherited stdin can therefore inject extra instructions or secrets into
review, repair, or marker-response rounds.

### M24. Local quality failures are logged and injected into Codex repair prompts without redaction - `ralph_loop/quality.py`, `ralph_loop/codex_agent.py`, `ralph_loop/process.py`

When `just ci` or `just test` fails, `_run_local_quality_gates()` formats raw
stdout/stderr into `failure_summary` (`ralph_loop/quality.py:91`,
`ralph_loop/quality.py:93`) and `_run_local_quality_fix_round()` places that text
directly inside the next Codex prompt (`ralph_loop/codex_agent.py:172`). The
subprocess helper also replays captured output to stdout/stderr
(`ralph_loop/process.py:150`). Token-bearing logs or prompt-injection text from
a failing tool can be exposed locally and handed to Codex as ordinary
instructions.

### M25. Codex last-message files are read without a size bound - `ralph_loop/codex_agent.py`

Subprocess stdout/stderr are capped by `_read_bounded_output()`, but the file
written by `codex exec -o` is opened and read with `handle.read().strip()`
(`ralph_loop/codex_agent.py:47`, `ralph_loop/codex_agent.py:49`). If Codex
ignores the one-line marker contract and writes a very large final message,
Ralph loads the entire file into memory and runs marker extraction over it before
truncating only for display.

### M26. Numeric branch names are ambiguous with GitHub CLI PR numbers - `ralph_loop/cli.py`, `ralph_loop/gh_ops.py`

When `--pr` is omitted, `_resolve_pr_data()` passes the current branch string
directly to `gh pr view` (`ralph_loop/cli.py:145`). Check polling also passes the
PR head branch directly to `gh pr checks` (`ralph_loop/gh_ops.py:280`). GitHub
CLI accepts `<number> | <url> | <branch>` for both commands, so a valid branch
named `123` is ambiguous with PR number `123`. Ralph can resolve, poll, or gate
against the wrong PR unless numeric branch names are rejected or disambiguated.

### M27. Non-dry-run setup mutates the launching checkout before PR validation - `ralph_loop/cli.py`, `ralph_loop/identity.py`

`main()` calls `_ensure_runtime_identity()` before resolving and validating the
PR, before acquiring the per-PR lock, and before entering the dedicated PR
worktree (`ralph_loop/cli.py:233`, `ralph_loop/cli.py:235`). That helper writes
local git identity, SSH command, signing format, signing key, and gpgsign config
(`ralph_loop/identity.py:91`). A run against a closed, draft, wrong-base, or
fork PR can still rewrite the launching checkout's git config before Ralph
knows it should proceed.

## LOW / DEFERRED

### L3. Cleanup documentation understates ignored-file deletion - `README.md`, `GUIDE.md`, `ralph_loop/git_ops.py`

The docs describe discard cleanup as `git clean -fd` (`README.md:117`,
`GUIDE.md:309`), but `_reset_generated_changes()` uses `git clean -fdx` in every
cleanup path (`ralph_loop/git_ops.py:78`, `ralph_loop/git_ops.py:82`,
`ralph_loop/git_ops.py:86`). Operators are told only untracked non-ignored files
are removed, while Ralph actually deletes ignored files in the disposable PR
worktree too.

### L4. No `--dry-run` flag - throughout

Given the destructive operations (`git reset --hard`, `git clean -fd`,
force-with-lease pushes, PR approval, merge, and branch deletion), an opt-in
dry run would make debugging and review safer.

### L5. `_round_numbers` remains a very thin helper - `ralph_loop/runtime.py`

`_round_numbers()` is still a small bounded/unbounded generator
(`ralph_loop/runtime.py:17`). It is acceptable as-is, but the abstraction is
only useful if future loop code shares it.

### L6. Marker fallback treats `No issues remain` as failure text - `ralph_loop/codex_agent.py`

`_infer_review_pass_without_marker()` strips `no actionable issues remain` before
checking failure phrases, but not the shorter success phrase `no issues remain`
(`ralph_loop/codex_agent.py:72`). If Codex omits the explicit marker and writes
`No issues remain`, the fallback currently matches `issues remain` and returns
failure.

### L7. Operational caveats in `GUIDE.md` are stale - `GUIDE.md`

The guide still says `codex exec` failures raise before partial marker recovery
and that transient GitHub CLI failures are not retried automatically
(`GUIDE.md:441`, `GUIDE.md:443`). Current code can continue from a non-zero
Codex run when a last-message exists, and read-style GitHub CLI helpers now have
retry handling.

## OPEN GAPS

- The pytest suite passes, but coverage still needs broader integration-style
  behavior around git/GitHub side effects.
- No structured JSON-lines logging option.
- No telemetry for round counts, phase duration, or total wall-clock time.
- `COAUTHOR_LINE` is env-overridable, but there is still no validation that the
  configured email matches the active signer or intended attribution.
