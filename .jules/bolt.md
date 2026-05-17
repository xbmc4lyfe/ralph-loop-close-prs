## 2024-05-17 - [Optimized Set Intersections in Python]
**Learning:** `set.isdisjoint(iterable)` is executed in C and avoids the overhead of setting up a generator expression in Python, making it significantly faster than `any(x in set for x in iterable)` when checking for intersection between a sequence and a set, especially inside hot loops like file path scanning.
**Action:** Use `.isdisjoint()` whenever checking if any element of a sequence exists in a `set` or `frozenset` to improve performance.
