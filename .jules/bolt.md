## 2025-05-22 - [Optimize PR state polling concurrency]
**Learning:** Checking GitHub PR open/closed states (`gh pr view`) individually in a loop caused substantial N+1 sequential execution delays, especially when checking respawn eligibility and fan-out limits for multiple PRs at once. Subprocesses to GitHub CLI are a major bottleneck.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` to handle subprocess calls to GitHub CLI when checking state for multiple PRs simultaneously to eliminate sequential bottlenecks.
