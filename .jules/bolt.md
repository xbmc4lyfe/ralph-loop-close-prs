## 2024-05-19 - [Subprocess bottlenecks with gh CLI]
**Learning:** Sequential subprocess calls to the GitHub CLI ('gh') via functions like '_gh_json' or '_gh_run_with_retry' are a significant performance bottleneck when processing multiple PRs (e.g. checking if they are still open).
**Action:** Use concurrency (such as `concurrent.futures.ThreadPoolExecutor`) to parallelize these checks and avoid N+1 execution delays.
