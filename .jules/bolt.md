## 2026-05-18 - Speed up PR state filtering via concurrency
**Learning:** Subprocess calls to the GitHub CLI ('gh') in python (e.g. via `_gh_json` or `_pr_view`) are a significant performance bottleneck, especially when the supervisor polls states for many PRs sequentially. The N+1 subprocess delay adds up quickly.
**Action:** When performing `gh` queries across multiple PRs (like `_pr_is_still_open`), always use a thread pool (e.g., `concurrent.futures.ThreadPoolExecutor`) to issue requests concurrently rather than sequential loops.
