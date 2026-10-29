# Configuration and commands

## Quick start

From a C++ repository, run `ctxpp init`. This creates a starter `.ctxpp.toml`, builds the optional local C++ parser when available, and indexes the project. Then try `ctxpp packet 'Namespace::Type::method' --budget 1800` for a source-grounded, bounded context packet. `ctxpp status` explains what is ready and what still needs attention.

Generated indexes normally live in `<repository>/.ctxpp/`. To keep all generated state outside the checkout, pass `--state-dir /path/to/private/ctxpp-state` on every command (or set `CTXPP_STATE_DIR`). The source tree and `.ctxpp.toml` remain canonical; only generated state moves. Existing projects keep using `.ctxpp/` by default.

For integrations that must not refresh or write state, use `ctxpp --root R --state-dir S --read-only --json export`. This reads the last published index, checks that the index matches its manifest, and reports freshness as it was recorded at scan time. It never scans source files, initializes a project, or updates caches. `load_export(root, state_path)` in `ctxpp_lib` provides the same read-only operation. To refresh intentionally, run `ctxpp --root R --state-dir S scan` separately.

Copy `assets/default.ctxpp.toml` to the repository as `.ctxpp.toml`. Presence opts into retrieval/authoring, never automatic source rewriting.

Commands:

- `ctxpp init [--root R]`: agent-owned bootstrap. Create `.ctxpp.toml` only when absent, preserve existing configuration, build the local semantic core when needed, discover the compilation database, and scan. It never installs packages or blindly configures an unknown project. The invoking agent must resolve reported `agent_actions` when safely possible instead of delegating template copying or metadata generation to the user.
- `ctxpp doctor [--root R]`: compilers, Clang runtime/dev support, database, standards, extensions, tokenizer, commands, parse issues, write safety.
- `scan`: incremental file/command hashing and semantic JSONL index; degraded file index if Clang is unavailable.
- `status`: freshness, failures, profile, config, write safety.
- `export`: read-only JSON export of the already-published index. Pair with `--read-only` for retrieval commands to forbid implicit refreshes.
- `where TARGET`, `route QUERY`: compact ranked navigation.
- `slice TARGET --intent edit --budget 2000 [--depth N]`: budgeted source bundle.
- `view TARGET [--layout navigable]`: read-only compact bundle and map.
- `explain X`, `expand TARGET`: mappings/abbreviations/decisions or canonical rendering.
- `audit [PATH...]`, `lint [PATH...]`: measurements and policy diagnostics.
- `plan [PATH...] --rule semantic-local-rename`: dry-run source plan.
- `shard PATH`: dry-run same-TU fragment plan.
- `apply PLAN`: explicit transactional application; `verify PLAN_OR_PATHS`: verification stack.

Index artifacts under `.ctxpp/` (or the selected state directory) are generated. JSON/JSONL keys and record ordering are stable. Index records carry source hashes, configuration and compiler determinants; the manifest ties the records to the indexed source files. Tokenizer values support `auto`, a supported base encoding, or `external:<command>`; external adapters read UTF-8 on stdin and print one integer. Cache by content hash plus tokenizer identity/version.

Resource admission is automatic. The compatible positive `tool.max_workers` value remains an upper bound; omission or `"auto"` removes that bound. `CTXPP_MAX_WORKERS` and `CTXPP_MAX_MEMORY` are optional diagnostic ceilings.

Source extensions: `.h`, `.hh`, `.hpp`, `.hxx`, `.cc`, `.cpp`, `.cxx`, and configured `.cu`/`.cuh`. Exclude build, third-party, vendor, generated, dependency, `.git`, and `.ctxpp` trees by default.
