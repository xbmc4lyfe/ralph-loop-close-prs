## 2026-05-19 - Concurrent gh CLI executions
**Learning:** Subprocess calls to the GitHub CLI (`gh`), typically executed via `_gh_json` or `_gh_run_with_retry`, are a significant performance bottleneck. When checking states for multiple PRs, checking them sequentially introduces N+1 sequential execution delays.
**Action:** When evaluating states across multiple PRs (e.g. `_filter_to_still_open_prs`), use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to evaluate the items in parallel.
