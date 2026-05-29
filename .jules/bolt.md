## 2024-05-29 - Optimize PR status checking with concurrent execution
**Learning:** Checking PR statuses sequentially using the GitHub CLI (`gh pr view`) can be a significant performance bottleneck, especially when verifying multiple PRs, leading to N+1 query problems.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` to check PR statuses concurrently. Use `executor.map` to preserve the original order of the PRs. This applies when there are list comprehensions or loops fetching network or process-based data.
