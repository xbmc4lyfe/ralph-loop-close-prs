## 2026-05-19 - gh CLI Subprocess Bottleneck
**Learning:** Subprocess calls to the GitHub CLI (`gh`), executed via `_gh_json` or `_gh_run_with_retry`, create a significant performance bottleneck due to network delays when executed sequentially (N+1 queries).
**Action:** Use concurrency (e.g., `concurrent.futures.ThreadPoolExecutor`) when batch processing data involving the `gh` CLI.
