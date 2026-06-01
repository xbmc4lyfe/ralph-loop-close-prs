## 2024-05-18 - Concurrent fan-out PR state checking
**Learning:** Subprocess calls to the GitHub CLI (`gh`), especially when checking states for multiple PRs sequentially (e.g. `gh pr view` for N PRs), create a significant bottleneck. This leads to N+1 delays in the supervisor loop.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` and `executor.map` to concurrently evaluate PR states, significantly reducing the waiting time when fanning out over many open PRs, while safely wrapping exceptions in a helper to maintain order.
