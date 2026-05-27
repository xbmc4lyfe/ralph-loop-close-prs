## 2024-05-24 - Optimize N+1 sequential subprocess execution in fan-out PR checking
**Learning:** Subprocess calls to the GitHub CLI (`gh`), typically executed via `_gh_json` or `_gh_run_with_retry`, can become a significant performance bottleneck when executed sequentially in a loop (e.g., N+1 sequential execution delays when checking states for multiple PRs).
**Action:** When checking states for multiple PRs, use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to avoid N+1 sequential execution delays. Ensure that the original order is preserved (e.g., by using `executor.map`).
