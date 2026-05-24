## 2024-05-24 - Parallelize PR open state checks
**Learning:** Sequential subprocess execution for batched network items produces a severe N+1 bottleneck, slowing down checking PR states.
**Action:** Mitigate N+1 bottleneck when checking PR states by using `ThreadPoolExecutor.map` for concurrency, ensuring list order is preserved.
