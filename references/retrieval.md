# Retrieval and slicing

Resolve targets by qualified name, stable ID, file:line, test, path, diagnostic location, or deterministic lexical query. On ambiguity, show a ranked candidate list instead of opening every match.

Use intents to prioritize candidates:

- `understand`: target, containing contract/type, definitions, representative callees.
- `edit`: verbatim target, used types, invariants, mutations, tests, important callers.
- `debug`: diagnostic site, caller/error paths, mutations and state.
- `test`: fixtures/helpers, state setup, target implementation.
- `api`: public declaration/definition, invariants, compatibility tests.
- `performance`: layouts, specialization, allocation, threading, launch/benchmark context.

Inclusion classes are mandatory syntax, mandatory semantics, high-value behavior, validation, and optional neighborhood. Select complete semantic units best-first by utility per token; prerequisites may force small declarations. Never emit the full transitive closure automatically or truncate an entity.

If mandatory material exceeds budget, emit whole highest-value mandatory units, mark `sufficient=0`, state mandatory tokens, and provide omission routes. Treat templates, inline/constexpr definitions, concepts, and required macros as body-required.

For edits, use the bundle only to route. Reopen the mapped canonical target before editing.
