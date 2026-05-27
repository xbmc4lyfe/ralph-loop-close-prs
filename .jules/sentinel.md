## 2026-05-27 - Resolved Bandit Warnings
**Vulnerability:** Static analysis warnings from Bandit highlighting insecure usage of pseudo-random generators and missing explicit verification of subprocesses without `shell=True`.
**Learning:** `random` module usage triggers B311 and `subprocess` triggers B603/B606 unless suppressed.
**Prevention:** Use `secrets.SystemRandom()` for any random jitter/generation, and document subprocess safety with `# nosec B603` inline pragmas to keep automated security scans clean.
