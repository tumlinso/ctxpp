---
name: cpp-context-compiler
description: Minimize Codex context for C++ by indexing symbols, generating budgeted semantic slices, enforcing context-efficient authoring, and optionally sharding or safely compacting source. Use when a repository contains .ctxpp.toml or the user explicitly asks for machine-legible, context-dense, token-minimized, sharded, or model-optimized C++. Never rewrite ordinary C++ implicitly.
---

# C++ Context Compiler

## Repository workflow

For substantial repository work, ALWAYS use `coding-workflow` first when it is
available. Claim or resume the authoritative task through `coding-workflow`
before using this skill for bounded implementation, inspection, or testing. Do
not directly claim todo work or begin repository mutations first. Use lower-level
skill CLIs directly only when `coding-workflow` is unavailable, explicitly out
of scope, or itself being debugged.

Canonical C++ is authoritative. `.ctxpp/` bundles are generated, read-only retrieval artifacts; map edits back to canonical ranges.

## Route one mode

- Retrieve: `status`; `where`/`route`; `slice --intent ...`. Read [retrieval](references/retrieval.md).
- Task/CUDA route: use `packet --task-spec ... --consumer <name>` so accepted paths, task/campaign identity, canonical targets, and bounded performance context stay together.
- Author: retrieve, edit canonical source, test, rescan/lint. Read [authoring](references/authoring.md); add [topology](references/topology.md) only for boundaries/shards.
- Audit: `audit`, no mutation. Read [objective](references/objective.md).
- Compact view: `view`; edit targets stay verbatim. Read [compact views](references/compact-views.md).
- Plan source optimization: explicit compaction/sharding request only; dry-run only. Read [topology](references/topology.md), [transforms](references/source-transforms.md), [hazards](references/cpp-semantic-hazards.md), and [verification](references/verification.md).
- Apply: explicit mutation intent plus an existing plan; revalidate, transact, verify, retain reverse plan. Never commit unless asked.
- Explain/expand: `explain`/`expand`, no mutation.

Read [comment contracts](references/comment-contracts.md) only for comment changes, [evaluation](references/evaluation.md) only for skill evaluation, and [configuration](references/configuration.md) only for setup/CLI details.

## Open

1. On explicit use, run `scripts/ctxpp --root ROOT init`. The agent owns initialization: config, core build, compilation-database discovery/generation, and safe verification-command inference. Preserve existing config. Ask only for a real package/authority blocker.
2. Run `doctor` and `status`, then narrow with `where`/`route` and an intent slice. Retrieval refreshes relevant stale TUs lazily; do not preemptively full-scan. Before mutation, require a fresh full semantic scan.
3. Open canonical source only for the edit range. Test changes; refresh index/routes afterward.

Initialization is idempotent: never install packages or blindly configure an unknown project. In opted-in repositories, ordinary C++ work uses retrieval/authoring only. Never compact unrelated source; do not activate implicitly for routine C++ work in unconfigured repositories.

## Gates

- Preserve behavior, language modes, APIs/ABI, performance structure, diagnostics, and user work.
- Exclude generated/vendor/dependency/build trees. Refuse `.ctxpp/` edits.
- Semantic dependencies, renames, and plans require Clang identities/ranges; never semantic-regex rewrite.
- Disable plans without compilation commands, tooling, configuration agreement, exact baseline hashes, or required verification.
- Tool failure falls back to readable canonical inspection, never hand-minification.
- Measure configured tokens, label estimates, reject negligible changes; abbreviations stay stable, scoped, mapped, collision-free, and net-profitable.

Validate with `python -m unittest discover -s tests -v` and the skill-creator validator after metadata changes.
