# Generated compact views

Views are source-derived, generated, read-only artifacts with exact provenance. Emit `CTXPP/1`, intent, token status, target, dependencies, omission counts, and a separate source map. Refuse any edit whose target is under `.ctxpp/`.

For edit bundles, keep the target body byte-for-byte canonical. Compact dependencies more aggressively. For understand bundles, transform only when every emitted token/range has an unambiguous canonical mapping.

Profiles:

- `navigable`: retain useful boundaries; default.
- `flat-functions`: one complete top-level definition per line where safe.
- `extreme`: minimum measured bundle subject to lexical and mapping safety.

V1 may select units, drop irrelevant comments, preserve contracts/tool controls, compact whitespace with the Clang token stream, introduce local repeated-name aliases, and abbreviate local identifiers. Never alter preprocessing-required newlines or raw/escaped literal spelling.

Emit a glossary only for used abbreviations and only when bundle token savings exceed glossary cost. Keep mappings stable by semantic ID and prevent collisions within the likely slice. Never introduce Unicode C++ identifiers; measure Unicode comment notation rather than assuming it is cheaper.
