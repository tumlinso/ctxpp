---
name: cpp-context-compiler
description: Minimize Codex context for C++ by indexing symbols, generating budgeted semantic slices, enforcing context-efficient authoring, and optionally sharding or safely compacting source. Use when a repository contains .ctxpp.toml or the user explicitly asks for machine-legible, context-dense, token-minimized, sharded, or model-optimized C++. Never rewrite ordinary C++ implicitly.
---

# C++ Context Compiler

## Use ctxpp

Use ctxpp when a repository has `.ctxpp.toml` or the user asks for C++ symbol
navigation, dependency tracing, bounded source context, or ctxpp maintenance.
It complements the repository's chosen workflow host; it does not replace that
host's task or lifecycle instructions.

Canonical C++ is authoritative. `.ctxpp/` bundles (or artifacts in `--state-dir`) are generated retrieval artifacts; map edits back to canonical ranges. Use `--read-only` with retrieval commands or `export` when the published index must not refresh or write caches.

## Route one mode

- Retrieve: `status`; `where`/`route`; `slice --intent ...`. Read [retrieval](references/retrieval.md).
- Task/CUDA route: use `packet --task-spec ... --consumer <name>` when the host supplies an authorized task specification; it carries selected paths, target identity, and bounded performance context together.
- Author: retrieve, edit canonical source, test, rescan/lint. Read [authoring](references/authoring.md); add [topology](references/topology.md) only for boundaries/shards.
- Audit: `audit`, no mutation. Read [objective](references/objective.md).
- Compact view: `view`; edit targets stay verbatim. Read [compact views](references/compact-views.md).
- Plan source optimization: explicit compaction/sharding request only; dry-run only. Read [topology](references/topology.md), [transforms](references/source-transforms.md), [hazards](references/cpp-semantic-hazards.md), and [verification](references/verification.md).
- Apply: explicit mutation intent plus an existing plan; revalidate, transact, verify, retain reverse plan. Never commit unless asked.
- Explain/expand: `explain`/`expand`, no mutation.

Read [comment contracts](references/comment-contracts.md) only for comment changes, [evaluation](references/evaluation.md) only for skill evaluation, and [configuration](references/configuration.md) only for setup/CLI details.

## Open

1. On explicit use, run `ctxpp --root ROOT init`. Initialization preserves an existing `.ctxpp.toml`, builds the optional parser when needed, discovers the compilation database, and scans. It does not install packages or guess unknown build commands.
2. Check `status`, then use `where SYMBOL` for direct locations, `route WORDS` for a natural-language query, or `inspect SYMBOL --intent edit --budget N` for bounded edit context. Use `packet` when the workflow host provides a task specification; use `slice` for a bounded semantic view. Retrieval refreshes only relevant stale translation units; an ordinary edit does not need a whole-repository rescan.
3. Use a hash-verified, current, verbatim canonical target returned by `inspect` directly with normal patch-context checks; a duplicate read or whole-repository scan is unnecessary. Reopen mapped canonical ranges when the view transforms or compacts source. Test the change, then use status or targeted retrieval to confirm freshness. Explicit source-rewrite plans require current semantic evidence for affected files.

Initialization is idempotent: never install packages or blindly configure an unknown project. In opted-in repositories, ordinary C++ work uses retrieval/authoring only. Never compact unrelated source; do not activate implicitly for routine C++ work in unconfigured repositories.

## Gates

- Preserve behavior, language modes, APIs/ABI, performance structure, diagnostics, and user work.
- Exclude generated/vendor/dependency/build trees. Refuse `.ctxpp/` edits.
- Semantic dependencies, renames, and plans require Clang identities/ranges; never semantic-regex rewrite.
- Disable plans without compilation commands, tooling, configuration agreement, exact baseline hashes, or required verification.
- Tool failure falls back to readable canonical inspection, never hand-minification.
- Measure configured tokens, label estimates, reject negligible changes; abbreviations stay stable, scoped, mapped, collision-free, and net-profitable.

Validate with `python -m unittest discover -s tests -v` and the skill-creator validator after metadata changes.
