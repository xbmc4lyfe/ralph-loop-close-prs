## 2024-05-25 - Concurrency for GitHub CLI State Checks
**Learning:** Subprocess calls to the GitHub CLI (`gh`) are a significant bottleneck when executed sequentially in a loop. Checking the open state of multiple PRs via `gh pr view` one-by-one results in N+1 execution delays.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` to perform the operations concurrently. `executor.map()` is ideal for preserving the original order of the items while eliminating the sequential delay.
