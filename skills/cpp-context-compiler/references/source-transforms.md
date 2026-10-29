# Canonical source transforms

Canonical mutation is dry-run by default and requires explicit user intent, `source_write=true`, a Clang-semantic index, exact baseline hashes, agreement across observed configurations, a transactional apply, required verification, and a retained reverse plan.

Proof levels:

- P0: generated view only.
- P1: identical token sequence/layout or semantic identifier references.
- P2: narrow rule-specific AST/type equivalence.
- P3: broader behavior validated by targeted/full and optional differential/IR checks.
- P4: separately approved API/ABI/performance/architecture change.

V1 rules must declare version, preconditions, forbidden cases, profitability, risks, verification, and reverse data. Favor local namespace/type aliases, semantic local renaming, nested namespaces when the language standard permits, contract normalization, redundant `else` after proven exit, exact built-in-bool return simplification, restricted scalar conditional returns, dense-only safe braces, and Clang-token whitespace compaction.

## Implemented V1 surface

The shipped canonical rewrite is `CTXPP-RENAME-LOCAL` only: profitable narrow local variables with complete Clang-derived declaration/reference ranges, no opaque/macro occurrence, no collision, no protected/public identity, and configuration agreement. `CTXPP-SHARD-SAME-TU` is the separate structural rule for contiguous complete definitions. Contract handling is lint-only; generated-view token compaction is P0. The other favorable rules above are future candidates and must not be claimed or imitated manually.

Runtime libclang provides the locally tested backend. `tool/src/libtooling_main.cpp` provides the AST-matcher/USR development backend when Clang headers and CMake packages exist; this environment does not validate that optional target. CUDA mutation remains disabled unless the active compilation database parses it without errors.

Disable by default: `auto` substitution, temporary inlining, declaration/assignment fusion, compound assignment, increment substitution, range-loop changes, lambdas, cast/init changes, moving namespace objects, removing `std::move`, exception/attribute/constexpr changes, overload/ADL changes, template spelling changes, and side-effect reordering.

Never assume assignment/operator/conditional/initialization spellings are equivalent. Compilation verifies a proof; it is not the proof.

Workflow: baseline -> semantic scan -> measure -> plan -> temporary apply -> configured verification -> retain or restore exact bytes. Never stash, reset, or overwrite unrelated work. Abort if any planned hash changed.
