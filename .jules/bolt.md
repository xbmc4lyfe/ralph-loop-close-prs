## 2026-05-31 - Use ThreadPoolExecutor for GitHub PR lookups
**Learning:** Checking states for multiple PRs using sequential `gh` subprocess calls is a significant performance bottleneck.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` to perform PR state checks concurrently, preserving order with `executor.map`.
