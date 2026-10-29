# CTXPP indexed artifacts, version 2

`index.sqlite` is the canonical `CTXPP-INDEX/2` artifact. Scans do not emit a
JSONL index or full JSON export. `ctxpp export` returns the small
`CTXPP-EXPORT/2` descriptor. `ctxpp export --legacy-json` streams the compatible
`CTXPP-EXPORT/1` envelope for legacy consumers; `ctxpp export --legacy-jsonl`
streams reconstructed `CTXPP-INDEX/1` records. The two legacy flags are mutually
exclusive. Both work with `--read-only` and leave generated state untouched.
The JSON envelope computes its manifest hash over reconstructed canonical JSONL
bytes while streaming, then appends the manifest after the records array. `ctxpp migrate` imports existing version 1
records and validated TU cache payloads without invoking the compiler. Migration
preserves the original files for rollback and parity checks. `ctxpp compress-cache`
losslessly replaces captured raw TU cache files with verified `.jsonl.z` files,
updates their provenance transactionally, then retires only unchanged raw copies.

## Publication and identity

The SQLite `PRAGMA user_version` is `2`. `metadata(key TEXT PRIMARY KEY,value
TEXT)` values are JSON, including scalar values. Keys are `schema` (number 2),
`generation` (opaque string), `source_root`, `index_meta`, `manifest`, `freshness`,
`tu_state`, and `readiness`. The original index meta record retains its original
format and fields for lossless compatibility. The artifact schema is independent
of that reconstructed record format.

A full publication creates a temporary database in the destination directory, commits,
fsyncs the file, and atomically replaces `index.sqlite`. The database is published
without WAL files. Readers use a SQLite URI with `mode=ro` and begin a read
transaction before checking the generation. Existing readers retain their opened
generation across replacement. Embedded metadata is authoritative. The
`manifest.json`, cache JSON files, and `export.json` are compatibility projections;
publication interruption cannot produce a partly committed SQLite generation.

Localized updates use a durable SQLite transaction (`journal_mode=DELETE`,
`synchronous=FULL`, `BEGIN IMMEDIATE`) to replace affected rows and metadata.
The expected generation is checked inside the transaction, preventing a stale
producer from overwriting another writer. The caller holds the publication lock.
A pinned reader sees its old snapshot; if it blocks the update beyond a short
lock wait, the writer rolls back and closes its transaction, copies the committed
base to a private file (preferring a filesystem reflink), applies the same delta
there, fsyncs it, and atomically replaces the canonical database. This rare path
copies bytes without decoding the graph. The normal path writes affected pages
only. Profile counters distinguish reader-contention fallback and copied bytes.

The descriptor fields are `format: CTXPP-EXPORT/2`, `schema: CTXPP-INDEX/2`,
`root`, `source_root`, `state_dir`, `database: index.sqlite`, `generation`,
`artifact_id` (equal to generation), `index_meta`, `index_integrity`, and
`freshness_scope: as-recorded-at-index-time`. The database path is relative to the
state directory. A reader must compare descriptor and database generation and
source root before using the artifact. A stale descriptor fails closed.

## Relational records

* `files(path TEXT PRIMARY KEY,file_order INTEGER,data BLOB)` stores compressed
  complete file records. `data` is UTF-8 JSON compressed with zlib.
* `symbols` has nonunique indexed `id TEXT` (semantic identity) and
  `symbol_order INTEGER PRIMARY KEY` (representation identity). Duplicate semantic
  identities from distinct representations are preserved. Searchable scalar
  columns are `name`, `qualified_name`, `kind`, `file`, `line`, `end_line`,
  `start_byte`, `end_byte`, `definition`, `is_test`, and `search_text`.
  `data BLOB` contains the complete compressed symbol except an array-valued occurrences field.
* `occurrences(symbol_row_id INTEGER,occurrence_order INTEGER,translation_unit
  TEXT,data BLOB)` contains complete compressed occurrence objects, ordered by
  `(symbol_row_id,occurrence_order)`. If `symbols.has_occurrences` is 1, reconstruct
  `occurrences` from these rows; otherwise the residual data retains any non-array value and preserves an absent key. The row ID is the
  symbol's `symbol_order`, never its potentially repeated semantic ID.
* `strings(id INTEGER PRIMARY KEY,value TEXT UNIQUE)` interns repeated edge
  strings. Edge identities and source paths do not repeat long USRs in every row.
* `edges` has `edge_order INTEGER PRIMARY KEY`, scalar columns listed below,
  `fields INTEGER`, and `data BLOB`. `data` is compressed residual JSON, or NULL
  for an empty object. Start with `{"record":"edge"}`, merge the residual, then
  reconstruct exactly the fields selected by the presence bitmask. Null and
  absent fields are distinct.

| Bit | Public field | SQLite column | Encoding |
| --- | --- | --- | --- |
| 0 | type | type_id | strings reference or NULL |
| 1 | from | from_id | strings reference or NULL |
| 2 | to | to_id | strings reference or NULL |
| 3 | file | file_id | strings reference or NULL |
| 4 | translation_unit | tu_id | strings reference or NULL |
| 5 | kind | kind_id | strings reference or NULL |
| 6 | configuration_hash | config_id | strings reference or NULL |
| 7 | via | via_id | strings reference or NULL |
| 8 | start | start | integer or NULL |
| 9 | end | end | integer or NULL |
| 10 | line | line | integer or NULL |
| 11 | column | column | integer or NULL |
| 12 | end_line | end_line | integer or NULL |
| 13 | end_column | end_column | integer or NULL |

`tu_contributions(cache_key TEXT PRIMARY KEY,state_key TEXT,input_fingerprint
TEXT,dependencies BLOB,cache_sha256 TEXT,payload BLOB)` stores independently
validated raw TU contributions. `dependencies` is compressed JSON and `payload`
is compressed original JSONL bytes. Provenance is taken from TU cache state and
validated payloads, never invented from merged graph records. Migration validates
current dependency hashes, index/manifest integrity, and cache JSON shape.
Missing historical caches produce no contribution row and disable localized
updates until sufficient provenance is available. New caches include their
command hash explicitly; legacy configuration identity is recovered only when
the recorded cache key matches the original meta/file-command determinants.

`provenance_symbols(cache_key TEXT,record_order INTEGER,symbol_id TEXT,
translation_unit TEXT,configuration_hash TEXT,data BLOB)` indexes individual raw
symbol contributions so a changed TU can reconstruct only impacted symbol IDs.
Its primary key is `(cache_key,record_order)`; indexes cover symbol ID and TU.
This internal index never substitutes merged records for missing raw provenance.
`prepare_incremental_provenance(root)` explicitly builds it from captured TU blobs
without parsing C++. The public graph and generation remain unchanged by this
auxiliary-index preparation.

`residual_records(record_order INTEGER,data BLOB)` preserves unknown record
kinds. Standard iteration is meta, files in file order, symbols in symbol order,
edges in edge order, then residual records. Normal indexes already use this order.

## Bounded retrieval and limits

Requested file reads use the `files.path` primary key. Exact symbol searches use
indexes on semantic ID, name, and qualified name. Symbol location lookup uses the
file/line index. Incident edges use indexes on `from_id` and `to_id`; match these
integer keys through `strings`. Requested symbols alone reconstruct occurrences.
Ordinary navigation never reconstructs the entire graph. Candidate decoding and
neighborhoods have explicit ceilings; a truncated neighborhood cannot claim
complete coverage. An indivisible source target larger than the budget returns
its canonical range, digest, and expansion handle with insufficient coverage.

Unchanged scans reuse metadata without decoding semantic records. Changed scans
reuse dependency-valid TU cache results and parse affected translation units.
Ordinary localized refreshes merge new TU results with indexed surviving symbol
contributions and update affected file metadata and TU-owned edges. Changes to
preferred callable locations also reattribute and re-enrich surviving edges in
those callable/dependency files. Known-TU deletion uses the same delta path.
Explicitly scoped uncompiled `.cc`, `.cpp`, `.cxx`, and `.cu` files can update
only their lexical symbols, file records, and freshness in a transaction, including
file additions and deletions. This requires complete TU dependency provenance,
unchanged configuration and compilation recipes, and no semantic ownership or
incident edges. An inferred recipe leaves the file lexical only when preflight
reports exactly `Clang driver unavailable`. Usable recipes and all other preflight
errors retain the normal semantic scan and diagnostic path. No TU payload is decoded.
Header and directory scopes retain the complete scan path. Profile counters
`lexical_file_delta_scans` and `lexical_file_scan_noops`, and the result's
`incremental.lexical_files`, distinguish changed and unchanged scoped file work.
Missing provenance falls back explicitly to complete graph reconstruction and is
reported by the `incremental_provenance_fallbacks` profile counter. An explicit
full scan may reconstruct a complete generation; ordinary indexed readers do not.
