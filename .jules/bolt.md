## 2024-05-24 - Avoid N+1 sequential execution delays for gh CLI subprocess calls
**Learning:** Subprocess calls to the GitHub CLI ('gh') are a significant performance bottleneck. When checking states for multiple PRs sequentially, the overhead of spawning multiple subprocesses causes noticeable delays (N+1 problem).
**Action:** Use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) with `.map()` when checking states for multiple PRs to execute `gh` CLI commands in parallel while preserving the original order of PRs.
