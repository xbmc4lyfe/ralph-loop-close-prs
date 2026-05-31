## 2024-05-24 - Fix Git argument injection via branch name
**Vulnerability:** Branch and base names fetched from remote sources or user input were being passed directly to `git` commands without validation. If a branch name started with a hyphen (e.g., `-b`), it could be interpreted as an argument by `git` commands like `git worktree add`, leading to Git argument injection.
**Learning:** All branch and base names fetched from remote sources must be validated to ensure they do not start with a hyphen before being passed to `git` commands to prevent Git argument injection vulnerabilities.
**Prevention:** Added checks in `_validate_pr_metadata` to raise a `CommandError` if `branch`, `pr_base`, or `expected_base` start with a hyphen.
