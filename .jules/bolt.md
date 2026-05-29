## 2026-05-29 - Use ThreadPoolExecutor for _filter_to_still_open_prs
**Learning:** Checking the open status of multiple PRs sequentially is a major performance bottleneck due to the N+1 problem of invoking the `gh` CLI for each PR in series.
**Action:** Use concurrent execution (`concurrent.futures.ThreadPoolExecutor.map`) to issue multiple `gh` subprocess calls in parallel, which greatly reduces the time required to filter a list of open PRs while maintaining identical functionality.
