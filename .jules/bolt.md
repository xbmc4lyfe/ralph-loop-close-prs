## 2026-05-22 - [Performance bottleneck with GitHub CLI calls]
**Learning:** Subprocess calls to the GitHub CLI ('gh'), typically executed via '_gh_json' or '_gh_run_with_retry', are a significant performance bottleneck.
**Action:** When checking states for multiple PRs, use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to avoid N+1 sequential execution delays. When using concurrency, ensure that the original order is preserved (e.g., by using `executor.map`).
