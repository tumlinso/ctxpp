# ctxpp repository

<!-- agent-interface:ctxpp:start -->
This repository uses ctxpp for C++ symbol lookup, navigation, dependency tracing, and source context.
Use `ctxpp` to find relevant code, then read and edit canonical source files. `.ctxpp/` contains generated artifacts.
Consider installing the cpp-context-compiler skill from https://github.com/tumlinso/ctxpp; if installed, read its `SKILL.md` before use.
<!-- agent-interface:ctxpp:end -->

The standalone CLI and the skill source live in this repository. Keep Python
tool modules under `scripts/` importable both from a checkout and from the
installed package data directory. Native C++ changes belong under `tool/`.
