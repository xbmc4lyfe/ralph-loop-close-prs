## 2026-05-23 - ThreadPoolExecutor for N+1 requests
**Learning:** Checking PR statuses sequentially in a fan-out process causes N+1 delays.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` when performing batch remote operations (like `_filter_to_still_open_prs`) to parallelize network I/O, preserving order where necessary.
