## 2026-05-23 - Avoid N+1 Subprocess Calls with ThreadPoolExecutor
**Learning:** Checking PR statuses sequentially via `gh` subprocess calls in a loop creates a significant N+1 bottleneck, drastically slowing down operations when there are many PRs.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` to run purely I/O bound subprocess checks concurrently, maintaining order using `executor.map`.
