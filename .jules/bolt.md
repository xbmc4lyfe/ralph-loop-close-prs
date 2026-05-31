## 2024-05-31 - Concurrent gh PR State Checks
**Learning:** Subprocess calls to the GitHub CLI ('gh') are a significant performance bottleneck when checked sequentially. When checking states for multiple PRs, N+1 sequential execution delays must be avoided.
**Action:** Use concurrent.futures.ThreadPoolExecutor with executor.map to perform multiple independent subprocess CLI checks concurrently while preserving original order.
