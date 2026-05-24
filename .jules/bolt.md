## 2024-05-24 - Avoid N+1 sequential delays for subprocess API calls
**Learning:** Subprocess calls to the GitHub CLI ('gh') inside loops, like when iterating over multiple PRs in `_filter_to_still_open_prs`, are a significant performance bottleneck due to the overhead of the subprocess boundary and network request. Checking states sequentially causes N+1 execution delays.
**Action:** When executing subprocess CLI commands in loops, use concurrency tools such as `concurrent.futures.ThreadPoolExecutor`. Preserve required ordering by using mapping methods like `executor.map()`.
