## 2024-06-01 - Fix N+1 PR state check delay
**Learning:** Checking PR state sequentially using `gh pr view` creates an N+1 performance bottleneck during the fan-out phase.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` and `.map()` to execute these checks concurrently, preserving order while removing the sequential delay.
