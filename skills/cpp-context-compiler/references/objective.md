# Objective and operating boundary

Optimize expected task-conditioned context, not repository size:

`J = loaded_tokens + lh*file_hops + la*ambiguity + lr*risk + lg*glossary + lb*build_or_performance_penalty`

Behavior, parsing, build configurations, API/ABI, and configured performance are hard gates. Use token count as the primary size measure; report bytes and lines secondarily. Generated views and canonical rewrites are separate decisions.

Audit file/symbol token size, slice cost, file hops, qualifier repetition, contracts, oversized semantic units, and route quality. Prefer retrieval improvements before physical sharding, and sharding before lexical source compaction. Reject changes that reduce total text but enlarge representative task slices.

Classify every observation as verified, estimated, opaque, conflicting, stale, or excluded. Never report compilation alone as semantic proof.
