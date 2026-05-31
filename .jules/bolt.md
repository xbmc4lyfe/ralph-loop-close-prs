## 2026-05-31 - Eliminate N+1 sequential execution delays for gh pr view commands
**Learning:** Subprocess calls to the GitHub CLI ('gh') are a significant performance bottleneck. Running sequentially for tasks like checking the state of multiple PRs causes N+1 delays, degrading overall processing speed for the app.
**Action:** When checking states for multiple PRs, use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) and `executor.map` to preserve the original order while avoiding sequential bottlenecks.
