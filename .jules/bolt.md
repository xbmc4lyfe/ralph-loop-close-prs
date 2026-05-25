## 2024-06-25 - Parallelize GitHub CLI Subprocess Calls
**Learning:** Subprocess calls to the GitHub CLI (`gh`), typically executed via `_gh_json` or `_gh_run_with_retry`, are a significant performance bottleneck. Specifically, checking states for multiple PRs sequentially introduces severe N+1 delays.
**Action:** When performing independent CLI checks across multiple entities (e.g., checking PR open status), use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to avoid sequential execution delays. Use `executor.map` to preserve the original order of items.
