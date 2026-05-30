## 2024-05-30 - Parallelize sequential GH CLI calls
**Learning:** Checking the state of multiple PRs via the GitHub CLI ('gh pr view') sequentially inside a loop (e.g. `_filter_to_still_open_prs`) causes severe N+1 performance bottlenecks because spawning subprocesses is inherently slow.
**Action:** When a Python script performs many independent, latency-bound operations like external CLI calls, always use concurrency (like `concurrent.futures.ThreadPoolExecutor` with `executor.map`) to issue them in parallel and dramatically cut down the total execution time while preserving order.
