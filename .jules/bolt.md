## 2024-05-27 - Use concurrency for sequential gh CLI checks
**Learning:** Subprocess calls to the GitHub CLI ('gh') are a significant performance bottleneck. When checking states for multiple PRs (e.g., in `_filter_to_still_open_prs`), running them sequentially causes N+1 delays.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` to perform independent `gh` CLI checks concurrently, preserving the original order using `executor.map`.
