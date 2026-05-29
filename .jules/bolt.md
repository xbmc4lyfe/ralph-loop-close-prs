## 2026-05-29 - Concurrent PR check optimizations
**Learning:** Checking states for multiple PRs sequentially causes N+1 execution delays due to slow `gh` CLI subprocess calls.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` when validating states of multiple PRs. Using `executor.map` ensures results remain ordered and are mapped back effectively to the initial fan out loop.
