# Context-efficient authoring

1. Inspect `.ctxpp.toml`, status, the nearest route, target slice, and local conventions.
2. Identify the likely maintenance unit and semantic neighbors.
3. Keep public names meaningful and local names concise, stable, and role-based.
4. Put ownership, lifetime, units, mutation, threading, failure, and nonlocal invariants in compact contracts.
5. Prefer ordinary C++ structure and profitable local aliases; never invent macro compression.
6. Use separate translation units for naturally independent components. Use same-TU fragments only when retrieval boundaries should not change linkage, initialization, macro state, or optimization.
7. Avoid one-file-per-helper and abstractions whose only purpose is shorter spelling.
8. Implement and test readable correct code before optional lexical density work.
9. Run build/tests, `ctxpp lint`, and `ctxpp scan` after edits.

A future change should normally need one route, one target shard, one or two contracts/types, and relevant tests. Preserve meaningful top-level navigation anchors even where locals are compact.
