## 2024-05-24 - Parallelize PR state checks to avoid N+1 bottleneck
**Learning:** Checking the open state of multiple PRs sequentially using the GitHub CLI (`gh`) causes a significant N+1 performance bottleneck due to multiple subprocess calls.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` with `executor.map` when checking the state of multiple PRs. This runs the network-bound subprocess calls in parallel while maintaining the original order.
