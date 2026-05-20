## 2024-05-18 - [Subprocess calls as bottlenecks]
**Learning:** Sequential `gh` subprocess calls inside loops (like `_pr_is_still_open` inside `_filter_to_still_open_prs`) create an N+1 execution delay that severely degrades performance when fanning out over many PRs.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` to run CLI subprocess calls concurrently when independent (e.g. state checking).
