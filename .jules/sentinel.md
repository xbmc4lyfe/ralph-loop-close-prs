## 2026-05-19 - Fix potential Git argument injection via branch names
**Vulnerability:** Git argument injection via malicious branch names.
**Learning:** Automated bots processing PRs must sanitize remote branch names. If a branch name starts with a hyphen, commands like `git checkout <branch>` or `git fetch origin <branch>` can interpret the branch name as a command-line option, leading to arbitrary code execution or unintended side effects.
**Prevention:** Validate all branch and base names fetched from remote sources to ensure they do not start with a hyphen.
