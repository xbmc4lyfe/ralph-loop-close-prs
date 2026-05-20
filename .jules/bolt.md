## 2024-05-20 - [Avoid N+1 sequential execution delays for gh CLI]
**Learning:** Subprocess calls to the GitHub CLI ('gh'), typically executed via '_gh_json' or '_gh_run_with_retry', are a significant performance bottleneck. When checking states for multiple PRs, sequential execution causes N+1 delays.
**Action:** Use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to execute these independent API calls in parallel, significantly reducing the total wait time when verifying the open state of multiple PRs.
