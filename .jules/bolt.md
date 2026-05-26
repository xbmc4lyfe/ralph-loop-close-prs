## 2024-05-24 - Optimize multiple PR checks with concurrent threadpool
**Learning:** Subprocess calls to the GitHub CLI ('gh'), especially inside loops like `_filter_to_still_open_prs`, are a significant performance bottleneck due to their N+1 sequential execution delays.
**Action:** When performing sequential `gh` CLI checks across multiple PRs (e.g. `_pr_is_still_open`), use `concurrent.futures.ThreadPoolExecutor` along with `executor.map` to execute them concurrently. This minimizes the collective subprocess overhead while keeping the original ordering of PR numbers intact.
