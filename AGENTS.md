# Agent Instructions

Ralph is a Python CLI for driving an existing GitHub PR through Codex review,
repair, local quality gates, CI monitoring, optional rebase, and optional merge.
It automates high-impact git and GitHub operations, so treat verified behavior
as more important than guesses.

## Superpowers System

Superpowers skills are installed through Codex native skill discovery:

```text
~/.agents/skills/superpowers -> ~/.codex/superpowers/skills
```

Codex discovers these skills at startup. Restart Codex after installing or
updating the symlink.

## Read First

- Use `README.md` for the project map, CLI summary, requirements, and examples.
- Use `GUIDE.md` for detailed control flow, preconditions, worktree behavior,
  destructive cleanup, CI repair behavior, and merge behavior.
- Use `BUGS.md` for known safety gaps and hardening work.
- `codex_ralph_wiggum_loop.py` is the compatibility entry point.
- `ralph_loop/` contains the implementation modules.
- `tests/` contains helper, CLI, git/GitHub, process, worktree, check, Codex,
  and quality regression tests.

## Reference-Only Material

- `agents-source/` contains downloaded AGENTS.md examples.
- `agents2-research/` contains local research notes when present.
- These folders are ignored local reference material. Do not treat them as
  active repo rules and do not commit them.

## Operating Rules

- Check `git status --short --branch` before editing.
- The worktree may contain unrelated user changes; do not revert or reformat
  them.
- Keep changes small and tied to requested Ralph behavior.
- Read `README.md` and the relevant `GUIDE.md` section before changing runtime
  behavior.
- Do not run Ralph against a real PR unless the user explicitly asks. It can
  push commits, add labels, approve PRs, rebase, merge, delete branches, and run
  destructive cleanup inside PR worktrees.
- When discussing behavior, separate verified facts from inference.

## Ralph Invariants

- Runtime identity is intentional. Preserve the `RALPH_*` identity, SSH, signing,
  and worktree overrides unless the user asks to change them.
- PR work belongs in the dedicated PR worktree under `RALPH_WORKTREE_ROOT` or
  the default temp root. Do not move destructive reset or clean behavior into
  the launching checkout.
- Preserve per-PR lock behavior; two Ralph runs must not race the same PR.
- Keep target-repo quality gates before generated commits and pushes.
- Keep Codex marker parsing strict. If a prompt contract changes, update both
  the prompt text and the marker extraction or fallback logic.
- Treat GitHub CLI and network failures as retryable only when they match known
  transient markers. Do not hide permission, identity, or merge-safety failures
  behind broad retries.

## Python Conventions

- Preserve Python 3.8 compatibility unless the user explicitly raises the
  minimum.
- Use explicit, Python 3.8-compatible type hints for new helpers.
- Prefer focused modules and helpers over adding branching to
  `ralph_loop.cli.main()`.
- Use `subprocess` with argument lists, not shell strings, unless there is a
  concrete reason.
- Keep CLI-facing text precise: say what failed, what was expected, and which
  command or config value is involved.
- Comment only non-obvious operational constraints, especially around
  destructive git behavior.

## Validation

- Syntax check:
  `python3 -m py_compile codex_ralph_wiggum_loop.py ralph_loop/*.py tests/*.py`
- Primary regression path: `python3 -m pytest`
- For focused changes, run the matching pytest module or test node, for example:
  `python3 -m pytest tests/test_cli_main.py -q`
- For CLI-surface changes, run:
  `python3 codex_ralph_wiggum_loop.py --help`
- For real git/GitHub side effects, validate with mocks or a safe dry-run path
  first. Do not use a real user PR as the first test unless the user explicitly
  authorizes that.

## Docs, Security, And Git

- Update `README.md` for user-facing CLI behavior, requirements, or examples.
- Update `GUIDE.md` when control flow, safety guarantees, worktree behavior,
  destructive cleanup, CI repair behavior, or merge behavior changes.
- Keep docs consistent with current code. If docs and code disagree, verify the
  code path before editing docs.
- Never commit secrets, tokens, private keys, `.env` files, local credential
  paths beyond documented placeholders, caches, temp worktrees, run logs, or
  generated local artifacts.
- Redact tokens, private repository URLs, and SSH key material from copied logs.
- Stage only files intentionally changed for the task.
- Keep commits focused and use imperative commit subjects.
- Do not add assistant-generated boilerplate to commits or docs.
- Before claiming completion, report which validation commands ran and any known
  gaps.
