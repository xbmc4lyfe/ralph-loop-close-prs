## 2024-05-28 - Optimize PR state checks with concurrency
**Learning:** Subprocess calls to the GitHub CLI (`gh`), typically executed via `_gh_json` or `_gh_run_with_retry`, are a significant performance bottleneck. When checking states for multiple PRs (e.g., `_pr_is_still_open`), doing it sequentially causes O(N) delays.
**Action:** When evaluating states for multiple PRs, use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to avoid N+1 sequential execution delays. When using concurrency, ensure that the original order is preserved (e.g., by using `executor.map`).
