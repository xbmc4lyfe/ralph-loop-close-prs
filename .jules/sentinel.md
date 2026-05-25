## 2025-05-25 - Prevent Git Argument Injection in Branch and Remote Names
**Vulnerability:** Git argument injection vulnerability where branch and remote names fetched from remote sources could start with a hyphen, causing Git to parse them as unintended command-line flags (e.g., `-oProxyCommand=...`).
**Learning:** External inputs like PR branch names or base names from GitHub can be maliciously crafted to execute arbitrary code when passed directly to subprocess calls of Git commands, even when `shell=False`.
**Prevention:** Always validate branch, remote, and ref names before passing them to Git commands, ensuring they do not start with a hyphen.
