# Topology and same-TU sharding

Prefer task-coherent boundaries between complete top-level declarations/definitions. Never split a body, macro-dependent region, conditional block, ordered initialization region, or attached contract/comment.

For a monolithic source, default to preserved-order fragments included by the original build entry:

```text
optimizer.cpp
optimizer/init.inc
optimizer/score.inc
optimizer/INDEX.ctx
```

Never include a `.cpp`. Use `.inc`, `.ipp`, or `.inc.cuh`; keep the host source as the build-system entry. This retains one translation unit, internal linkage, macro/include state, initialization order, and whole-TU optimization.

Construct a weighted symbol graph. Must-link templates with definitions, tightly coupled inline members, macro-state regions, private-state collaborators, and ordered preprocessor regions. Weight calls, layouts, shared mutation/invariants, tests, and adjacency. Collapse must-links, cluster contiguous regions, split at the lowest safe cut, preserve order, generate before/after representative slices, and reject context regression.

Defaults: target 600-1800 tokens, soft max 2400, hard warning 3200, minimum 200, at most 8 fragments per host before reevaluation. Reject tiny fragments whose routing/file-hop overhead exceeds savings.

Route files list `fragment:role|symbols:...|uses:...|mut:...|tokens:n` and must be materially smaller than opening every fragment. Plan first; apply only with explicit intent and verification.
