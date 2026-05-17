## 2026-05-17 - Optimize _is_generated_artifact_path
**Learning:** Checking for directory overlaps with `any(part in _GENERATED_ARTIFACT_DIRS for part in parts)` involves significant Python loop overhead compared to set intersection.
**Action:** Use `not _GENERATED_ARTIFACT_DIRS.isdisjoint(parts)` which executes entirely in C and is ~7x faster for these kinds of path traversal checks.
