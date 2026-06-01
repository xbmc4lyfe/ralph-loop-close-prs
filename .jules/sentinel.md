## 2024-05-30 - Prevent Git Argument Injection
**Vulnerability:** Git argument injection risk where ref names starting with a hyphen (like `-o`) could be interpreted as option flags by the `git` CLI executable when interpolated via `subprocess`.
**Learning:** Python `subprocess.run` calls that inject unvalidated inputs (especially branches/refs) into command argument arrays are vulnerable to flag injection since `git` may parse positional arguments as options if they begin with a hyphen.
**Prevention:** Always validate that git ref parameters do not start with a hyphen (`-`) before passing them to subprocesses, because `git` enforces that valid references never begin with a hyphen.
