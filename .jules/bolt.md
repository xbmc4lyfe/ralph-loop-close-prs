## 2024-05-24 - Avoid N+1 sequential subprocess delays for GitHub CLI checks
**Learning:** Sequential execution of subprocess calls (e.g. `gh pr view` for each PR in a fan-out loop) creates a significant performance bottleneck due to cumulative process spawning and network request delays.
**Action:** Use `concurrent.futures.ThreadPoolExecutor` along with `executor.map()` to check PR states concurrently. This significantly reduces total wait time while maintaining the original execution order expected by the surrounding logic.
