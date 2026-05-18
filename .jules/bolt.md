## 2024-05-18 - Avoid Sequential subprocess.Popen Calls for External CLI
**Learning:** Subprocess calls to the GitHub CLI ('gh'), typically executed via '_gh_json' or '_gh_run_with_retry' inside `_pr_is_still_open`, are a significant performance bottleneck when checking state for multiple PRs. Sequential execution causes N+1 delays.
**Action:** When iterating over multiple PRs to check their state, always use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to avoid sequential blocking.
