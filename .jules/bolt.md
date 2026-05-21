## 2024-05-21 - ThreadPoolExecutor for GitHub CLI Batching
**Learning:** Sequential subprocess calls to the GitHub CLI ('gh') can become a significant performance bottleneck during fan-out initialization where status checks for multiple PRs are performed.
**Action:** Used `concurrent.futures.ThreadPoolExecutor` to execute `_pr_is_still_open` checks concurrently in `_filter_to_still_open_prs`, thereby preventing N+1 execution delays without altering application logic or dependencies.
