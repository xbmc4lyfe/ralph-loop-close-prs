## 2026-05-29 - Git Argument Injection Vulnerability
**Vulnerability:** Untrusted branch and base names fetched from remote sources are passed directly to `git` commands without validation. If these names start with a hyphen (e.g., `-o`), they can be interpreted as arguments by `git`, leading to command injection vulnerabilities.
**Learning:** This is a specific Git command injection risk in automation scripts that rely on branching and external branch names without validating input strings.
**Prevention:** Always validate branch and base names fetched from remote sources to ensure they do not start with a hyphen before passing them to `git` commands.
