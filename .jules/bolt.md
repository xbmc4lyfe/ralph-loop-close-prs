## 2024-05-30 - Concurrent PR status checking
**Learning:** Subprocess calls to the GitHub CLI ('gh') sequentially, like in `_filter_to_still_open_prs` checking state for multiple PRs, are a significant performance bottleneck due to N+1 network requests.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` and `.map` to fetch PR states concurrently, reducing the overall latency of the fan-out supervisor, whilst keeping the logic grounded in checking `_pr_is_still_open` independently.
