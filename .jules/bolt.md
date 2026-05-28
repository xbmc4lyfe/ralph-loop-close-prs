## 2024-05-28 - Parallelize PR state checks
**Learning:** Subprocess calls to the GitHub CLI ('gh') are a significant performance bottleneck. When checking states for multiple PRs, they introduce N+1 sequential execution delays.
**Action:** Use concurrency (`concurrent.futures.ThreadPoolExecutor` and `executor.map`) to parallelize these checks while preserving original order.
