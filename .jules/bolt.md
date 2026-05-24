## 2024-06-25 - Subprocess bottleneck
**Learning:** Subprocess calls to the GitHub CLI ('gh') via `_gh_json` or `_gh_run_with_retry` are a significant performance bottleneck. When checking states for multiple PRs, using N+1 sequential execution delays causes a significant slowdown.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` to execute multiple `gh` calls concurrently when checking the state of many PRs.
