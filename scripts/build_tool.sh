#!/usr/bin/env bash
set -euo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
tool_dir=$(CDPATH= cd -- "$script_dir/../tool" && pwd)
build_dir=${CTXPP_BUILD_DIR:-"$tool_dir/build"}
cmake -S "$tool_dir" -B "$build_dir" -G "Unix Makefiles" -DCMAKE_BUILD_TYPE=Release
cmake --build "$build_dir" --parallel "${CTXPP_BUILD_JOBS:-2}"
"$build_dir/ctxpp-core" doctor
