## 2024-05-29 - Optimize gh CLI calls with ThreadPoolExecutor
**Learning:** Subprocess calls to the GitHub CLI ('gh') are a significant performance bottleneck. When iterating over items like PR numbers to check their status using `gh pr view`, executing them sequentially leads to N+1 delays.
**Action:** Use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) to execute these checks in parallel. Ensure that the original order is preserved using `executor.map` and handle per-item exceptions safely to avoid breaking the entire loop.
