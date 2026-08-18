# Configuration and commands

Copy `assets/default.ctxpp.toml` to the repository as `.ctxpp.toml`. Presence opts into retrieval/authoring, never automatic source rewriting.

Commands:

- `ctxpp doctor [--root R]`: compilers, Clang runtime/dev support, database, standards, extensions, tokenizer, commands, parse issues, write safety.
- `scan`: incremental file/command hashing and semantic JSONL index; degraded file index if Clang is unavailable.
- `status`: freshness, failures, profile, config, write safety.
- `where TARGET`, `route QUERY`: compact ranked navigation.
- `slice TARGET --intent edit --budget 2000 [--depth N]`: budgeted source bundle.
- `view TARGET [--layout navigable]`: read-only compact bundle and map.
- `explain X`, `expand TARGET`: mappings/abbreviations/decisions or canonical rendering.
- `audit [PATH...]`, `lint [PATH...]`: measurements and policy diagnostics.
- `plan [PATH...] --rule semantic-local-rename`: dry-run source plan.
- `shard PATH`: dry-run same-TU fragment plan.
- `apply PLAN`: explicit transactional application; `verify PLAN_OR_PATHS`: verification stack.

Index artifacts under `.ctxpp/` are generated. JSON/JSONL keys and record ordering are stable. Tokenizer values support `auto`, a supported base encoding, or `external:<command>`; external adapters read UTF-8 on stdin and print one integer. Cache by content hash plus tokenizer identity/version.

Source extensions: `.h`, `.hh`, `.hpp`, `.hxx`, `.cc`, `.cpp`, `.cxx`, and configured `.cu`/`.cuh`. Exclude build, third-party, vendor, generated, dependency, `.git`, and `.ctxpp` trees by default.
