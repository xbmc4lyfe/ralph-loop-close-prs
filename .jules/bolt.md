## 2026-05-21 - [Concurrency in gh API requests]
**Learning:** Checking the state of multiple PRs using sequential `gh` CLI commands (`_pr_is_still_open` which calls `_gh_run_with_retry` mapping to `subprocess.run`) causes an N+1 performance bottleneck. In `_filter_to_still_open_prs`, doing this sequentially for a repo with many PRs can delay the entire process by several seconds.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` when performing operations involving network latency through the `gh` CLI across multiple items, like checking PR statuses.
