---
name: cpp-context-compiler
description: Minimize Codex context for C++ by indexing symbols, generating budgeted semantic slices, enforcing context-efficient authoring, and optionally sharding or safely compacting source. Use when a repository contains .ctxpp.toml or the user explicitly asks for machine-legible, context-dense, token-minimized, sharded, or model-optimized C++. Never rewrite ordinary C++ implicitly.
---

# C++ Context Compiler

Treat canonical C++ as the only editable, compiler-facing source. Use generated `.ctxpp/` bundles as read-only retrieval artifacts; never edit or copy changes back from them without locating the mapped canonical range.

## Select one mode

- **Retrieve**: run `ctxpp status`, `where` or `route`, then `slice --intent ...`. Read [references/retrieval.md](references/retrieval.md).
- **Author**: retrieve first, edit canonical source, test, then rescan and lint. Read [references/authoring.md](references/authoring.md); read [references/topology.md](references/topology.md) only for file boundaries or sharding.
- **Audit**: run `ctxpp audit`; do not mutate source. Read [references/objective.md](references/objective.md).
- **Generate compact view**: run `ctxpp view`; keep the edit target verbatim for edit work. Read [references/compact-views.md](references/compact-views.md).
- **Plan optimization**: require an explicit request for source compaction or sharding. Read [references/topology.md](references/topology.md), [references/source-transforms.md](references/source-transforms.md), [references/cpp-semantic-hazards.md](references/cpp-semantic-hazards.md), and [references/verification.md](references/verification.md). Produce a dry-run plan only.
- **Apply optimization**: require explicit mutation intent and an existing plan. Revalidate hashes, apply transactionally, verify, and retain the reverse plan. Never commit unless asked.
- **Explain/expand**: use `ctxpp explain` or `expand`; do not mutate source.

Read [references/comment-contracts.md](references/comment-contracts.md) only when creating or rewriting comments. Read [references/evaluation.md](references/evaluation.md) only when evaluating this skill. Read [references/configuration.md](references/configuration.md) for configuration or command details.

## Required opening

1. Locate the repository root and `.ctxpp.toml`.
2. Run `scripts/ctxpp doctor` and `scripts/ctxpp status`.
3. If the index is stale, run `scripts/ctxpp scan`.
4. Resolve a narrow target, request an intent-appropriate slice, and open canonical source only for the exact edit range.

For an ordinary C++ task in an opted-in repository, use this skill for retrieval and authoring guidance only. Never compact unrelated source. In an unconfigured repository, do not activate implicitly for routine implementation, review, explanation, bug fixing, or refactoring.

## Hard safety behavior

- Preserve behavior, active language modes, APIs, ABI, performance-sensitive structure, diagnostics, and user changes as hard gates.
- Exclude generated, vendored, dependency, and build trees unless explicitly included.
- Require Clang-derived identities and ranges for semantic dependencies, renaming, and source plans. Regex may only assist discovery or formatting diagnostics.
- Disable source plans when compilation commands, semantic tooling, configuration agreement, or exact baseline hashes are missing.
- If tooling fails, state the limitation and inspect normal readable C++ directly. Do not hand-minify.
- Measure tokens with the configured adapter; label lexical/byte estimates. Reject negligible transformations.
- Keep meaningful top-level names and local semantic anchors. Abbreviations must be stable, scoped, collision-free, mapped, and profitable after glossary cost.
- Refuse edits to paths under `.ctxpp/`.

Run `python -m unittest discover -s tests -v` from this skill to validate the toolkit. Use the skill-creator validator after changing metadata.
