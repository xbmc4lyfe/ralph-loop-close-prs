## 2026-05-27 - Parallelize gh CLI calls
**Learning:** Subprocess calls to the GitHub CLI ('gh') are a significant performance bottleneck. When checking states for multiple PRs sequentially, it creates an N+1 delay issue.
**Action:** Use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to eliminate sequential delays when making multiple independent subprocess CLI calls (e.g., checking PR open states). Ensure the original order is preserved by using `executor.map`.
