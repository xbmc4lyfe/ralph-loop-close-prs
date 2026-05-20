## 2024-03-24 - [GitHub CLI Subprocess Calls as Bottleneck]
**Learning:** Checking states for multiple PRs by invoking the GitHub CLI (`gh`) via subprocess calls causes a significant performance bottleneck due to sequential N+1 delays.
**Action:** When handling states or information for multiple PRs simultaneously, use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to execute the subprocess calls in parallel and avoid sequential blocking.
