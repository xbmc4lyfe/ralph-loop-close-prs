## 2026-05-30 - Optimize _filter_to_still_open_prs using ThreadPoolExecutor
**Learning:** Sequential calls to `_pr_is_still_open` can be slow, especially over network boundaries. When fanning out over PRs, `gh pr view` calls were executed one after the other.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` to map `_check_pr_open_state` to all PRs in parallel, maintaining preserving order with `executor.map`.
