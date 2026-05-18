## 2026-05-17 - Concurrent gh CLI Calls
**Learning:** Checking states sequentially for multiple PRs using subprocess calls to the GitHub CLI (`gh`) causes severe N+1 performance bottlenecks because each call is slow. The `gh` CLI can safely be run concurrently from multiple threads.
**Action:** When performing independent `gh` CLI actions on lists of resources, use `concurrent.futures.ThreadPoolExecutor` to speed up the loop while keeping error handling localized.
