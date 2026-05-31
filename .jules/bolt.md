## 2024-05-24 - N+1 Process Executions in gh cli
**Learning:** Checking states for multiple PRs sequentially with _gh_json and _gh_run_with_retry spawns multiple gh cli processes which is a significant performance bottleneck.
**Action:** Use concurrent.futures.ThreadPoolExecutor for _gh_run_with_retry and _gh_json to run them concurrently when multiple PR checks need to be done.
