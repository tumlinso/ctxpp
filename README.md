# ctxpp

ctxpp indexes C and C++ repositories so coding agents can find relevant symbols,
callers, dependencies, tests, and canonical source ranges without opening the
whole project. Its index is retrieval support: canonical source files remain the
place to make and verify edits.

## Install

Python 3.11 or later is required. The package itself uses the Python standard
library. Install from this repository with pip:

```sh
python -m pip install .
```

The CLI is standalone. Installing this package does not install a Codex skill,
modify a repository, or register a Project Control integration. Use `ctxpp init`
only when opting a specific repository into ctxpp.

The optional native parser requires CMake 3.20, a C++17 compiler, and a usable
libclang runtime. `ctxpp init` builds it when available. The binary is placed in
the user's cache, outside the installed package. Set `CTXPP_CORE` to use an
existing binary or `CTXPP_BUILD_DIR` to select a build directory explicitly.

## Start using ctxpp

From a C++ repository:

```sh
ctxpp init
ctxpp status
ctxpp where 'Namespace::Type::method'
ctxpp route "storage capacity initialization tests"
ctxpp inspect 'Namespace::Type::method' --intent edit --budget 2400
ctxpp inspect 'Namespace::Type::method' --intent test --budget 2400
```

`init` is an explicit opt-in for one repository. It creates `.ctxpp.toml` if it
is absent, preserves existing settings, indexes the repository, and adds a
managed C++ routing block to root `AGENTS.md`. Use
`--agent-instructions=skip` when another host manages repository instructions.
The block is portable and may suggest installing the bundled
`cpp-context-compiler` skill. Initialization does not install a skill or
configure Project Control.

The skill is optional guidance for Codex. If useful, install
[`skills/cpp-context-compiler`](skills/cpp-context-compiler) in the Codex skill
scope you choose, or use the skill installer with this repository path. The
standalone CLI and `init` do not depend on the skill; installing it does not
make ctxpp a Project Control default.

A human `route` result includes bounded, source-verified context for its best
current match and alternate locations; use `--json` for the stable match list.
For test work, `inspect --intent test` gives one bounded view of a target's
declaration, related data shapes, and indexed test callsite/assertion windows.
Those source excerpts are freshness-checked and may be partial; an optional
build-target hint is derived from the compilation database and does not run a
test.

Ordinary retrieval refreshes only affected translation units. It reports
staleness and missing build context rather than claiming complete semantic
coverage. Read the canonical source range before editing; generated `.ctxpp/`
files are disposable index state, not source files.

## Native parser

`scripts/build_tool.sh` builds the parser into the same user cache used by
`ctxpp init`. The tool can use the runtime libclang backend without Clang
development headers; LibTooling support is enabled when compatible LLVM and
Clang development packages are available.

## Development

Run tests with:

```sh
python -m unittest discover -s tests -v
```

The integration suite builds its CMake fixture and native parser. Unit tests
cover packet contracts, incremental scanning, and generated-view safety.
