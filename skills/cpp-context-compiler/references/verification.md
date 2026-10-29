# Verification tiers and transactions

- V0 view: target resolution, valid mappings, whole mandatory entities, unchanged source, deterministic regeneration.
- V1 parse/compile: reparse every affected observation and compile affected translation units.
- V2 targeted behavior: symbol/file-related tests.
- V3 project behavior: configured broader suite.
- V4 compatibility: public symbols, ABI, bindings, serialization where relevant.
- V5 performance: benchmark configured sensitive paths and reject excess regression.

Normalize AST or IR only as supporting evidence; account for renames, locations, debug data, and legitimate form changes.

Before application, record root, git branch/commit/status, dirty files, targets, hashes, compilation database, commands, tokenizer, profile, and exclusions. Apply only planned files to temporary state or in-memory copies. Revalidate hashes immediately before atomic replacement.

On any failed command, parse conflict, mapping error, or stale hash, restore exact baseline bytes and remove planned created files. Retain the failed plan and report. On success, retain reverse edits, rescan, and report token/byte/line deltas plus commands. Do not create a commit.

For performance-sensitive changes, V5 is a hard gate. For public/ABI-relevant changes, default refusal; P4 requires separate permission and compatibility verification.
