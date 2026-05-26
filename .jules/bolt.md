## 2026-05-26 - Concurrent execution of gh CLI calls to prevent N+1 delays
**Learning:** Subprocess calls to the GitHub CLI ('gh') are a significant performance bottleneck. When checking states for multiple PRs sequentially (e.g., `_filter_to_still_open_prs`), it results in N+1 execution delays.
**Action:** When performing status checks or data gathering for multiple PRs, use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor.map`) to avoid N+1 delays while preserving the original order.
