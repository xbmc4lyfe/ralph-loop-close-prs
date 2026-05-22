## 2026-05-22 - Sequential gh CLI calls are a bottleneck
**Learning:** Subprocess calls to the GitHub CLI (`gh`), typically executed via `_gh_json` or `_gh_run_with_retry`, are a significant performance bottleneck. When checking states for multiple PRs, performing this sequentially introduces substantial N+1 delays.
**Action:** Use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to evaluate state across multiple PRs concurrently to avoid sequential execution delays. Use methods like `executor.map` to ensure that original order is preserved when needed.
