## 2024-05-24 - Concurrent Subprocess Calls
**Learning:** Subprocess calls to the GitHub CLI (`gh`), typically executed via `_gh_json` or `_gh_run_with_retry`, are a significant performance bottleneck when checking states for multiple PRs sequentially.
**Action:** When checking states for multiple PRs (e.g., in fan-out operations), use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to avoid N+1 sequential execution delays.
