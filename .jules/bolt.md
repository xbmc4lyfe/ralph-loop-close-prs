## 2024-05-24 - N+1 Query in gh API
**Learning:** Sequential calls to gh pr view (via _pr_is_still_open) for multiple PRs cause a severe performance bottleneck.
**Action:** Use ThreadPoolExecutor to run independent gh commands concurrently, preserving order.
