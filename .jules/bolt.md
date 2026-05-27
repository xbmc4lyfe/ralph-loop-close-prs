## 2024-05-24 - N+1 Query Problem with Subprocess Calls
**Learning:** Sequential subprocess calls to the GitHub CLI (`gh`) when checking states for multiple PRs (e.g., in `_filter_to_still_open_prs`) create a significant performance bottleneck due to cumulative I/O and process overhead. This operates like an N+1 query problem, but for subprocesses.
**Action:** Use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to parallelize independent, read-only subprocess calls. When doing so, ensure that the original ordering is preserved (such as by using `executor.map`).
