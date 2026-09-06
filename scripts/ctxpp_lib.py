from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ctxpp_runtime import (ResourceScheduler, build_query_store, count as work_count, open_query_store,
                           run_process_measured, span, timed)
from ctxpp_rank import rank as rank_candidates, terms as ranked_terms
from ctxpp_recipe import (infer as infer_recipe, observed_records, persist_successful, preflight as recipe_preflight,
                          run_captured, successful_records, translate as translate_recipe)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


VERSION = "0.1.0"
SOURCE_EXTENSIONS = (".h", ".hh", ".hpp", ".hxx", ".inc", ".ipp", ".cc", ".cpp", ".cxx", ".cu", ".cuh")
DEFAULT_EXCLUDES = ("build/**", "cmake-build-*/**", "third_party/**", "vendor/**", "generated/**", ".git/**", ".ctxpp/**")
CONTRACT_FIELDS = ("in", "out", "req", "ens", "inv", "mut", "own", "thr", "sync", "err", "cost", "num", "abbr", "why")


class CtxppError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@timed("file_hashing")
def sha256_file(path: Path) -> str:
    work_count("files_hashed")
    return sha256_bytes(path.read_bytes())


def git_dirty_paths(root: Path) -> set[str] | None:
    proc = subprocess.run(["git", "-c", "status.relativePaths=true", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                          cwd=root, text=False, capture_output=True, check=False)
    if proc.returncode != 0:
        return None
    parts = proc.stdout.split(b"\0")
    dirty: set[str] = set()
    index = 0
    while index < len(parts):
        entry = parts[index]
        if not entry:
            index += 1
            continue
        status = entry[:2]
        dirty.add(entry[3:].decode("utf-8", errors="surrogateescape"))
        if b"R" in status or b"C" in status:
            index += 1
            if index < len(parts) and parts[index]:
                dirty.add(parts[index].decode("utf-8", errors="surrogateescape"))
        index += 1
    return dirty


def atomic_write(path: Path, data: bytes) -> None:
    work_count("cache_writes" if ".ctxpp" in path.parts else "atomic_writes")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


@contextlib.contextmanager
def publication_lock(root: Path):
    path = root / ".ctxpp/cache/publish.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def view_cache_lock(root: Path, target: dict[str, Any], intent: str, budget: int, layout: str,
                    tokenizer_config: str, core: Path | None):
    request = _view_request_path(root, target, intent, budget, layout, tokenizer_config, core)
    lock_path = request.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def find_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        if (path / ".ctxpp.toml").is_file():
            return path
    return current


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "profile": "view",
        "tokenizer": "auto",
        "notation": "token-min",
        "source_write": False,
        "preserve_public_api": True,
        "preserve_abi": True,
        "allow_cuda": True,
        "sources": ["include/**/*.h", "include/**/*.hpp", "src/**/*.cc", "src/**/*.cpp", "src/**/*.cxx", "tests/**/*.cpp"],
        "exclude": list(DEFAULT_EXCLUDES),
        "budgets": {
            "default_slice_tokens": 2400,
            "route_tokens": 400,
            "fragment_target_tokens": 1200,
            "fragment_soft_max_tokens": 2400,
            "fragment_hard_max_tokens": 3200,
            "fragment_min_tokens": 200,
            "max_fragments_per_host": 8,
        },
        "identifiers": {
            "rename_locals": True,
            "rename_parameters": False,
            "rename_private_members": False,
            "rename_internal_functions": False,
            "minimum_net_token_saving": 4,
            "allow_unicode_identifiers": False,
        },
        "comments": {"contract_language": True, "compress_public_docs": False, "preserve_special_comments": True},
        "rewrites": {
            "remove_redundant_else": True,
            "simplify_exact_bool": True,
            "scalar_ternary": False,
            "remove_braces": False,
            "replace_explicit_type_with_auto": False,
            "invent_macros": False,
            "using_namespace": False,
        },
        "verification": {"build": [], "targeted_tests": [], "full_tests": [], "abi": [], "performance": []},
        "protected": {"paths": [], "symbols": [], "name_patterns": []},
    }


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


@timed("configuration_parsing")
def load_config(root: Path) -> tuple[dict[str, Any], bool]:
    path = root / ".ctxpp.toml"
    if not path.is_file():
        return default_config(), False
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    cfg = _merge(default_config(), raw)
    if cfg.get("version") != 1:
        raise CtxppError(f"unsupported .ctxpp.toml version: {cfg.get('version')!r}")
    return cfg, True


def is_excluded(rel: str, cfg: dict[str, Any]) -> bool:
    rel = rel.replace(os.sep, "/")
    return any(fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel + "/", pattern) for pattern in cfg.get("exclude", DEFAULT_EXCLUDES))


def source_paths(root: Path, cfg: dict[str, Any], scoped: Iterable[str] = (),
                 compilation_records: Iterable[dict[str, Any]] = ()) -> list[Path]:
    candidates: set[Path] = set()
    scopes = [Path(x) for x in scoped]
    if scopes:
        for raw in scopes:
            path = raw if raw.is_absolute() else root / raw
            if path.is_file():
                candidates.add(path.resolve())
            elif path.is_dir():
                candidates.update(p.resolve() for p in path.rglob("*") if p.is_file())
    else:
        for pattern in cfg.get("sources", []):
            candidates.update(p.resolve() for p in root.glob(pattern) if p.is_file())
        for record in compilation_records:
            directory = Path(record.get("directory", root))
            path = Path(record.get("file", ""))
            path = path if path.is_absolute() else directory / path
            if path.is_file():
                candidates.add(path.resolve())
        git = subprocess.run(["git", "ls-files", "--cached", "--others", "--modified", "--exclude-standard", "-z"],
                             cwd=root, capture_output=True, check=False)
        if git.returncode == 0:
            for raw in git.stdout.split(b"\0"):
                if raw:
                    path = root / raw.decode("utf-8", errors="surrogateescape")
                    if path.is_file() and path.suffix in SOURCE_EXTENSIONS:
                        candidates.add(path.resolve())
        if not candidates:
            candidates.update(p.resolve() for p in root.rglob("*") if p.is_file() and p.suffix in SOURCE_EXTENSIONS)
    result = []
    for path in candidates:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if path.suffix in SOURCE_EXTENSIONS and not is_excluded(rel, cfg):
            result.append(path)
    return sorted(result, key=lambda p: p.relative_to(root).as_posix())


@timed("compdb_discovery")
def find_compdb(root: Path, cfg: dict[str, Any]) -> Path | None:
    configured = cfg.get("compilation_database") or cfg.get("tool", {}).get("compilation_database")
    candidates = []
    if configured:
        candidate = Path(configured)
        candidates.append(candidate if candidate.is_absolute() else root / candidate)
    candidates += [root / "compile_commands.json", root / "build/compile_commands.json"]
    candidates += sorted(root.glob("cmake-build-*/compile_commands.json"))
    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


@timed("compdb_parsing")
def read_compdb(path: Path | None, cache_root: Path | None = None) -> list[dict[str, Any]]:
    if not path:
        return []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CtxppError(f"invalid compilation database {path}: {exc}") from exc
    digest = sha256_bytes(raw)
    cache_path = index_dir(cache_root) / "cache/compdb.json" if cache_root else None
    if cache_path:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("format") == "CTXPP-COMPDB-CACHE/1" and cached.get("hash") == digest:
                work_count("compdb_cache_hits")
                return list(cached.get("records", []))
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    try:
        records = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CtxppError(f"invalid compilation database {path}: {exc}") from exc
    records = records if isinstance(records, list) else []
    if cache_path:
        atomic_write(cache_path, (stable_json({"format": "CTXPP-COMPDB-CACHE/1", "hash": digest, "records": records}) + "\n").encode())
    return records


def command_argv(record: dict[str, Any]) -> list[str]:
    if isinstance(record.get("arguments"), list):
        return [str(x) for x in record["arguments"]]
    return shlex.split(str(record.get("command", "")))


def normalized_clang_args(record: dict[str, Any], source: Path) -> list[str]:
    argv = command_argv(record)
    if argv:
        argv = argv[1:]
    result: list[str] = []
    skip = False
    source_real = source.resolve()
    directory = Path(record.get("directory", source.parent)).resolve()
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg in ("-o", "--output"):
            skip = True
            continue
        if arg in ("-c", "--compile"):
            continue
        possible = Path(arg)
        if not possible.is_absolute():
            possible = directory / possible
        try:
            if possible.resolve() == source_real:
                continue
        except OSError:
            pass
        result.append(arg)
    # Clang must use its own builtin headers. Injecting GCC's private include
    # directory shadows Clang intrinsics (xmmintrin.h) with incompatible builtins.
    return result


@dataclass
class ParseExecution:
    job: dict[str, Any]
    completed: subprocess.CompletedProcess[str]
    peak_memory_mb: int


def compdb_by_file(records: list[dict[str, Any]]) -> dict[Path, list[dict[str, Any]]]:
    result: dict[Path, list[dict[str, Any]]] = {}
    for record in records:
        directory = Path(record.get("directory", "."))
        file = Path(record.get("file", ""))
        path = file if file.is_absolute() else directory / file
        try:
            path = path.resolve()
        except OSError:
            continue
        result.setdefault(path, []).append(record)
    for values in result.values():
        values.sort(key=lambda x: stable_json(x))
    return result


@dataclass(frozen=True)
class _CppToken:
    text: str
    start: int
    end: int
    kind: str = "token"


def _cpp_tokens(data: bytes) -> list[_CppToken]:
    """Conservative byte lexer for range recovery; it never supplies semantic proof."""
    result: list[_CppToken] = []
    i = 0
    while i < len(data):
        byte = data[i]
        if byte in b" \t\r\n\f\v":
            i += 1; continue
        if data.startswith(b"//", i):
            end = data.find(b"\n", i + 2); end = len(data) if end < 0 else end
            result.append(_CppToken(data[i:end].decode(errors="replace"), i, end, "comment")); i = end; continue
        if data.startswith(b"/*", i):
            end = data.find(b"*/", i + 2); end = len(data) if end < 0 else end + 2
            result.append(_CppToken(data[i:end].decode(errors="replace"), i, end, "comment")); i = end; continue
        raw_prefix = next((prefix for prefix in (b'u8R"', b'uR"', b'UR"', b'LR"', b'R"') if data.startswith(prefix, i)), None)
        if raw_prefix:
            delimiter_start = i + len(raw_prefix)
            opening = data.find(b"(", delimiter_start, min(len(data), delimiter_start + 17))
            if opening < 0:
                result.append(_CppToken(raw_prefix.decode(), i, i + len(raw_prefix), "opaque")); i += len(raw_prefix); continue
            delimiter = data[delimiter_start:opening]
            closing = data.find(b")" + delimiter + b'"', opening + 1)
            end = len(data) if closing < 0 else closing + len(delimiter) + 2
            result.append(_CppToken("<raw-string>", i, end, "literal")); i = end; continue
        if byte in (ord('"'), ord("'")):
            quote = byte; start = i; i += 1
            while i < len(data):
                if data[i] == ord("\\"): i += 2
                elif data[i] == quote: i += 1; break
                else: i += 1
            result.append(_CppToken("<literal>", start, min(i, len(data)), "literal")); continue
        line_start = data.rfind(b"\n", 0, i) + 1
        if byte == ord("#") and not data[line_start:i].strip():
            start = i
            while True:
                end = data.find(b"\n", i)
                if end < 0: end = len(data); break
                if end > start and data[end - 1] == ord("\\"): i = end + 1; continue
                break
            result.append(_CppToken(data[start:end].decode(errors="replace"), start, end, "preprocessor")); i = end; continue
        if byte == ord("_") or chr(byte).isalpha():
            start = i; i += 1
            while i < len(data) and (data[i] == ord("_") or chr(data[i]).isalnum()): i += 1
            result.append(_CppToken(data[start:i].decode(errors="replace"), start, i, "identifier")); continue
        result.append(_CppToken(chr(byte), i, i + 1)); i += 1
    return result


def _attached_comment_start(data: bytes, declaration_start: int) -> int:
    start = declaration_start
    cursor = data.rfind(b"\n", 0, declaration_start) + 1
    while cursor > 0:
        line_end = cursor - 1
        line_start = data.rfind(b"\n", 0, line_end) + 1
        line = data[line_start:line_end].strip()
        if not line:
            break
        if line.startswith((b"//", b"///", b"//!")):
            start = line_start; cursor = line_start; continue
        if line.endswith(b"*/"):
            block_start = data.rfind(b"/*", 0, line_end)
            if block_start >= 0:
                start = block_start; cursor = block_start; continue
        break
    return start


def _lexical_declarations(data: bytes) -> list[dict[str, Any]]:
    tokens = _cpp_tokens(data)
    declarations: list[dict[str, Any]] = []
    keywords = {"class", "struct", "enum", "union"}
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.text not in keywords:
            continue
        name_index = index + 1
        if token.text == "enum" and name_index < len(tokens) and tokens[name_index].text in ("class", "struct"):
            name_index += 1
        while name_index < len(tokens) and tokens[name_index].kind == "comment": name_index += 1
        if name_index >= len(tokens) or tokens[name_index].kind != "identifier":
            continue
        name_token = tokens[name_index]
        cursor = name_index + 1
        opening = -1
        terminator = -1
        while cursor < len(tokens):
            current = tokens[cursor]
            if current.text == "{": opening = cursor; break
            if current.text == ";": terminator = cursor; break
            if current.text == "}" or current.kind == "preprocessor": break
            cursor += 1
        declaration_start = token.start
        # Preserve immediately adjacent template/export/attribute prefixes without crossing a prior entity.
        prior = index - 1
        while prior >= 0 and tokens[prior].kind == "comment": prior -= 1
        boundary = prior
        while boundary >= 0 and tokens[boundary].text not in (";", "{", "}"):
            boundary -= 1
        prefix = tokens[boundary + 1:index]
        if any(item.text in ("template", "export", "[") for item in prefix):
            declaration_start = prefix[0].start
        declaration_start = _attached_comment_start(data, declaration_start)
        definition = opening >= 0
        body_start = body_end = None
        range_valid = True
        if definition:
            depth = 0
            closing = -1
            for position in range(opening, len(tokens)):
                current = tokens[position]
                if current.kind == "preprocessor": range_valid = False
                if current.text == "{": depth += 1
                elif current.text == "}":
                    depth -= 1
                    if depth == 0:
                        closing = position; break
            if closing < 0:
                range_valid = False; declaration_end = name_token.end
            else:
                body_start, body_end = tokens[opening].start, tokens[closing].end
                after = closing + 1
                while after < len(tokens) and tokens[after].kind == "comment": after += 1
                if after < len(tokens) and tokens[after].text == ";":
                    declaration_end = tokens[after].end
                else:
                    declaration_end = tokens[closing].end; range_valid = False
        elif terminator >= 0:
            declaration_end = tokens[terminator].end
        else:
            declaration_end = name_token.end; range_valid = False
        declarations.append({"name": name_token.text, "keyword": token.text,
                             "name_start": name_token.start, "name_end": name_token.end,
                             "declaration_start": declaration_start, "declaration_end": declaration_end,
                             "body_start": body_start, "body_end": body_end, "definition": definition,
                             "range_valid": range_valid, "target_complete": range_valid})
    return declarations


def lexical_symbols(root: Path, paths: Iterable[Path], semantic: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Route-only declarations for files without current semantic records."""
    semantic_files = {str(symbol.get("file", "")) for symbol in semantic}
    semantic_definitions = {(str(symbol.get("file", "")), str(symbol.get("name", "")))
                            for symbol in semantic if symbol.get("definition")}
    result: list[dict[str, Any]] = []
    declaration_words = {"class": "LexicalClassDecl", "struct": "LexicalStructDecl",
                         "enum": "LexicalEnumDecl", "union": "LexicalUnionDecl"}
    for path in paths:
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        identifiers = sorted(_lexical_identifiers(data))
        for declaration in _lexical_declarations(data):
            name = declaration["name"]
            if (rel, name) in semantic_definitions:
                continue
            start, end = declaration["declaration_start"], declaration["declaration_end"]
            line = data[:start].count(b"\n") + 1
            end_line = data[:end].count(b"\n") + 1
            definition = bool(declaration["definition"])
            kind = declaration_words[declaration["keyword"]]
            result.append({"record": "symbol", "id": f"lex:{rel}:{declaration['name_start']}", "name": name,
                           "qualified_name": name, "kind": kind, "file": rel,
                           "start": start, "end": end, "name_start": declaration["name_start"],
                           "name_end": declaration["name_end"], "body_start": declaration["body_start"],
                           "body_end": declaration["body_end"], "line": line, "end_line": end_line, "column": 1,
                           "end_column": 1, "definition": definition, "degraded": True,
                           "semantic_origin": "lexical_only", "readiness": "route-ready",
                           "representation_kind": "lexical-complete-definition" if definition else "lexical-declaration",
                           "range_valid": declaration["range_valid"], "target_complete": declaration["target_complete"],
                           "body_present": definition and declaration["range_valid"], "range_version": 2,
                           "source_hash": sha256_bytes(data), "tokens": max(1, (end - start) // 4),
                           "signature": data[start:(declaration["body_start"] or end)].decode(errors="replace").strip(),
                           "contract": "", "lexical_terms": " ".join(identifiers),
                           "occurrences": [{"file": rel, "start": start, "end": end, "line": line,
                                            "end_line": end_line, "definition": definition}]})
        # Preserve the pre-existing namespace routing anchors.  They are locations,
        # not complete slice representations.
        cpp_tokens = _cpp_tokens(data)
        for index, token in enumerate(cpp_tokens[:-1]):
            name_token = cpp_tokens[index + 1]
            if token.text != "namespace" or name_token.kind != "identifier":
                continue
            name = name_token.text
            if (rel, name) in semantic_definitions:
                continue
            line = data[:name_token.start].count(b"\n") + 1
            result.append({"record": "symbol", "id": f"lex:{rel}:{name_token.start}", "name": name,
                           "qualified_name": name, "kind": "LexicalNamespaceDecl", "file": rel,
                           "start": name_token.start, "end": name_token.end, "name_start": name_token.start,
                           "name_end": name_token.end, "line": line, "end_line": line, "column": 1,
                           "end_column": len(name) + 1, "definition": True, "degraded": True,
                           "semantic_origin": "lexical_only", "readiness": "route-ready",
                           "representation_kind": "name-only", "range_valid": False,
                           "target_complete": False, "body_present": False, "range_version": 2,
                           "source_hash": sha256_bytes(data), "tokens": 1,
                           "signature": f"namespace {name}", "contract": "",
                           "lexical_terms": " ".join(identifiers), "occurrences": []})
        if rel not in semantic_files and not any(symbol.get("file") == rel for symbol in result):
            name = path.stem
            result.append({"record": "symbol", "id": f"lex-file:{rel}", "name": name,
                           "qualified_name": name, "kind": "LexicalFile", "file": rel, "start": 0,
                           "end": len(data), "line": 1, "end_line": line_count(data), "definition": False,
                           "degraded": True, "semantic_origin": "lexical_only", "readiness": "route-ready",
                           "tokens": max(1, len(data) // 4), "signature": "", "contract": "",
                           "lexical_terms": " ".join(identifiers), "occurrences": []})
    return sorted(result, key=lambda symbol: (symbol["qualified_name"], symbol["file"], symbol["start"]))


def lexical_overlay(root: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads((index_dir(root) / "cache/lexical-overlay.json").read_text(encoding="utf-8"))
        return list(payload.get("symbols", [])) if (payload.get("format") == "CTXPP-LEXICAL-OVERLAY/1"
                                                       and payload.get("range_version") == 2) else []
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def refresh_lexical_overlay(root: Path, paths: Iterable[Path]) -> None:
    selected = list(paths)
    rels = {path.relative_to(root).as_posix() for path in selected}
    symbols = [symbol for symbol in lexical_overlay(root) if symbol.get("file") not in rels]
    symbols.extend(lexical_symbols(root, selected, []))
    symbols.sort(key=lambda symbol: (symbol.get("qualified_name", ""), symbol.get("file", ""), symbol.get("start", 0)))
    payload = {"format": "CTXPP-LEXICAL-OVERLAY/1", "range_version": 2, "symbols": symbols}
    atomic_write(index_dir(root) / "cache/lexical-overlay.json", (stable_json(payload) + "\n").encode())


def summarize_diagnostics(root: Path, failures: list[dict[str, Any]], operation: str) -> list[dict[str, Any]]:
    if not failures:
        return []
    log_dir = index_dir(root) / "logs"
    digest = sha256_bytes(("\n".join(stable_json(item) for item in failures)).encode())[:16]
    log_path = log_dir / f"{operation}-{digest}.log"
    atomic_write(log_path, ("\n".join(stable_json(item) for item in failures) + "\n").encode())
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for failure in failures:
        message = str(failure.get("error", "")).strip()
        first = next((line.strip() for line in message.splitlines() if line.strip()), "unknown diagnostic")
        first = first[:512]
        category = str(failure.get("category") or _diagnostic_category(first))
        key = (category, first)
        group = groups.setdefault(key, {"file": failure.get("file"), "configuration": failure.get("configuration"),
                                        "error": first, "category": category, "count": 0, "affected_files": set()})
        group["count"] += 1
        if failure.get("file"):
            group["affected_files"].add(str(failure["file"]))
    summary = []
    for group in sorted(groups.values(), key=lambda value: (value["category"], value["error"]))[:12]:
        group["affected_file_count"] = len(group.pop("affected_files"))
        group["details_log"] = log_path.relative_to(root).as_posix()
        summary.append(group)
    for old in sorted(log_dir.glob("*.log"), key=lambda path: path.stat().st_mtime_ns)[:-16]:
        try: old.unlink()
        except OSError: pass
    return summary


def _diagnostic_category(message: str) -> str:
    lower = message.lower()
    if "unknown argument" in lower or "unsupported" in lower or "unrecognized" in lower:
        return "command_translation"
    if "cuda" in lower and ("installation" in lower or "toolkit" in lower or "version" in lower):
        return "toolchain_compatibility"
    if "no such file" in lower or "file not found" in lower:
        return "missing_dependency"
    if "internal" in lower:
        return "index_internal"
    return "source_parse"


def find_core(skill_root: Path) -> Path | None:
    override = os.environ.get("CTXPP_CORE")
    candidates = [Path(override)] if override else []
    candidates += [skill_root / "tool/build/ctxpp-core", skill_root / "tool/build-release/ctxpp-core"]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    return None


def run_core(core: Path, arguments: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    work_count("core_calls")
    with span(f"core_{arguments[0] if arguments else 'unknown'}"):
        return subprocess.run([str(core), *arguments], cwd=cwd, text=True, capture_output=True, check=False)


@dataclass(frozen=True)
class TokenCount:
    count: int
    exact: bool
    identity: str
    version: str


class Tokenizer:
    def __init__(self, root: Path, configured: str, *, defer_writes: bool = False):
        self.root = root
        self.configured = configured
        self.defer_writes = defer_writes
        self.dirty = False
        self._identity_cache: tuple[str, str] | None = None
        self.cache_path = root / ".ctxpp/token-cache.json"
        try:
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.cache = {}

    def _adapter(self, text: str) -> TokenCount | None:
        spec = self.configured
        if spec.startswith("external:"):
            command = spec.split(":", 1)[1]
            proc = subprocess.run(shlex.split(command), input=text, text=True, capture_output=True, cwd=self.root, check=False)
            if proc.returncode != 0:
                raise CtxppError(f"external tokenizer failed: {proc.stderr.strip()}")
            try:
                count = int(proc.stdout.strip())
            except ValueError as exc:
                raise CtxppError("external tokenizer must print one integer") from exc
            return TokenCount(count, True, spec, "external-v1")
        if spec == "auto" or spec.startswith("tiktoken:"):
            try:
                import tiktoken  # type: ignore
            except ModuleNotFoundError:
                return None
            encoding_name = spec.split(":", 1)[1] if ":" in spec else "cl100k_base"
            encoding = tiktoken.get_encoding(encoding_name)
            return TokenCount(len(encoding.encode(text)), True, f"tiktoken:{encoding_name}", getattr(tiktoken, "__version__", "unknown"))
        return None

    def _cache_identity(self) -> tuple[str, str]:
        if self._identity_cache is not None:
            return self._identity_cache
        if self.configured.startswith("external:"):
            argv = shlex.split(self.configured.split(":", 1)[1])
            fingerprints = []
            for index, token in enumerate(argv):
                candidate = Path(shutil.which(token) or token) if index == 0 else Path(token)
                if not candidate.is_absolute():
                    candidate = self.root / candidate
                try:
                    candidate = candidate.resolve()
                except OSError:
                    pass
                if candidate.is_file():
                    stat = candidate.stat()
                    # Hash adapters exactly. Large interpreter binaries contribute their installed
                    # identity without rereading tens of MiB on every short query.
                    identity = sha256_file(candidate) if index or stat.st_size <= 4 * 1024**2 else f"{stat.st_size}:{stat.st_mtime_ns}"
                    fingerprints.append((str(candidate), identity))
            version = sha256_bytes(stable_json(fingerprints).encode())[:16] if fingerprints else "external-v1"
            self._identity_cache = (self.configured, f"external-v1:{version}")
            return self._identity_cache
        if self.configured == "auto" or self.configured.startswith("tiktoken:"):
            try:
                import tiktoken  # type: ignore
                encoding_name = self.configured.split(":", 1)[1] if ":" in self.configured else "cl100k_base"
                self._identity_cache = (f"tiktoken:{encoding_name}", getattr(tiktoken, "__version__", "unknown"))
                return self._identity_cache
            except ModuleNotFoundError:
                pass
        self._identity_cache = ("utf8-bytes/4", "estimate-v1")
        return self._identity_cache

    def cache_identity(self) -> str:
        identity, version = self._cache_identity()
        return f"{self.configured}:{identity}:{version}"

    @staticmethod
    def _estimate(text: str) -> int:
        if not text:
            return 0
        # Conservative byte/BPE proxy. This is measurement only, never semantic parsing.
        return max(1, (len(text.encode("utf-8")) + 3) // 4)

    def count(self, text: str) -> TokenCount:
        work_count("tokenizer_calls")
        content_hash = sha256_bytes(text.encode("utf-8"))
        identity, version = self._cache_identity()
        cache_key = f"{self.configured}:{identity}:{version}:{content_hash}"
        cached = self.cache.get(cache_key)
        if isinstance(cached, dict):
            work_count("token_cache_hits")
            return TokenCount(int(cached["count"]), bool(cached["exact"]), str(cached["identity"]), str(cached["version"]))
        work_count("token_cache_misses")
        result = self._adapter(text) or TokenCount(self._estimate(text), False, "utf8-bytes/4", "estimate-v1")
        self.cache[cache_key] = result.__dict__
        self.dirty = True
        if not self.defer_writes:
            self.flush()
        return result

    def flush(self) -> None:
        if not self.dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.cache_path.with_suffix(self.cache_path.suffix + ".lock")
        with lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    current = json.loads(self.cache_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    current = {}
                current.update(self.cache)
                atomic_write(self.cache_path, (stable_json(current) + "\n").encode())
                self.cache = current
                self.dirty = False
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def language_standard(args: Iterable[str]) -> str:
    for arg in args:
        if arg.startswith("-std="):
            return arg.split("=", 1)[1]
    return "compiler-default"


def line_count(data: bytes) -> int:
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def classify_file(rel: str) -> str:
    if rel.startswith(("third_party/", "vendor/")):
        return "vendor"
    if rel.startswith(("generated/", "build/")):
        return "generated"
    if rel.startswith("include/"):
        return "public"
    if rel.startswith("tests/"):
        return "test"
    return "internal"


def index_dir(root: Path) -> Path:
    return root / ".ctxpp"


_INDEX_CACHE: dict[Path, tuple[tuple[int, int, int], list[dict[str, Any]]]] = {}


def load_index(root: Path) -> list[dict[str, Any]]:
    path = index_dir(root) / "index.jsonl"
    if not path.is_file():
        raise CtxppError("semantic index missing; run ctxpp scan")
    stat = path.stat()
    identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    cached = _INDEX_CACHE.get(path)
    if cached and cached[0] == identity:
        work_count("index_cache_hits")
        return cached[1]
    work_count("index_cache_misses")
    work_count("bytes_read_from_index", stat.st_size)
    result = []
    with span("index_load"):
        lines = path.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, 1):
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise CtxppError(f"invalid index record at line {number}: {exc}") from exc
    _INDEX_CACHE[path] = (identity, result)
    return result


def partition_index(records: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    meta = next((x for x in records if x.get("record") == "meta"), {})
    files = [x for x in records if x.get("record") == "file"]
    symbols = [x for x in records if x.get("record") == "symbol"]
    edges = [x for x in records if x.get("record") == "edge"]
    return meta, files, symbols, edges


def index_meta(root: Path) -> dict[str, Any]:
    store = open_query_store(root)
    if store:
        try:
            return store.meta()
        finally:
            store.close()
    path = index_dir(root) / "index.jsonl"
    if not path.is_file():
        raise CtxppError("semantic index missing; run ctxpp scan")
    try:
        with path.open(encoding="utf-8") as stream:
            return json.loads(stream.readline())
    except json.JSONDecodeError as exc:
        raise CtxppError(f"invalid index record at line 1: {exc}") from exc


def _merge_core_records(core_records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    symbol_map: dict[str, dict[str, Any]] = {}
    edge_map: dict[str, dict[str, Any]] = {}
    observations = []
    for record in core_records:
        kind = record.get("record")
        if kind == "observation":
            observations.append(record)
        elif kind == "symbol":
            sid = record["id"]
            occurrence = {k: record.get(k) for k in ("file", "start", "end", "line", "column", "end_line", "end_column", "definition", "translation_unit", "configuration_hash")}
            existing = symbol_map.get(sid)
            if not existing:
                record["occurrences"] = [occurrence]
                symbol_map[sid] = record
            else:
                existing.setdefault("occurrences", []).append(occurrence)
                if record.get("definition") and not existing.get("definition"):
                    saved = existing["occurrences"]
                    symbol_map[sid] = record
                    symbol_map[sid]["occurrences"] = saved
        elif kind == "edge":
            key = stable_json(record)
            edge_map[key] = record
    symbols = sorted(symbol_map.values(), key=lambda x: (x.get("qualified_name", ""), x.get("file", ""), x.get("start", 0), x["id"]))
    for symbol in symbols:
        symbol["occurrences"] = sorted({stable_json(x): x for x in symbol.get("occurrences", [])}.values(), key=lambda x: (x.get("file", ""), x.get("start", 0)))
    by_id = {s["id"]: s for s in symbols}
    callable_kinds = {"FunctionDecl", "CXXMethod", "Constructor", "Destructor", "FunctionTemplate", "ConversionFunction"}
    for edge in edge_map.values():
        if edge.get("type") not in ("reference", "call", "type_use", "member_access", "macro_expansion"):
            continue
        source = by_id.get(edge.get("from", ""))
        offset = int(edge.get("start", -1))
        if (source and source.get("kind") in callable_kinds and source.get("file") == edge.get("file")
                and source.get("start", 0) <= offset <= source.get("end", -1)):
            continue
        enclosing = [s for s in symbols if s.get("kind") in callable_kinds and s.get("file") == edge.get("file")
                     and s.get("start", 0) <= offset <= s.get("end", -1)]
        if enclosing:
            edge["from"] = min(enclosing, key=lambda s: (s.get("end", 0) - s.get("start", 0), s["id"]))["id"]
    edges = sorted(edge_map.values(), key=lambda x: (x.get("type", ""), x.get("from", ""), x.get("to", ""), x.get("file", ""), x.get("start", 0)))
    return symbols, edges, sorted(observations, key=lambda x: (x.get("file", ""), stable_json(x)))


@timed("semantic_graph_reconstruction")
def enrich_semantic_edges(root: Path, symbols: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {s["id"]: s for s in symbols}
    callable_kinds = {"FunctionDecl", "CXXMethod", "CXXConstructor", "CXXDestructor", "FunctionTemplate", "ConversionFunction"}
    extras = []
    data_cache: dict[str, bytes] = {}
    for edge in edges:
        if str(edge.get("file", "")).startswith("tests/") and edge.get("to") in by_id:
            extras.append({**edge, "type": "test_relationship", "via": edge.get("type")})
        if edge.get("kind") == "TemplateRef" and edge.get("to") in by_id:
            extras.append({**edge, "type": "template_dependency", "via": edge.get("type")})
        target = by_id.get(edge.get("to", ""))
        source = by_id.get(edge.get("from", ""))
        if edge.get("type") not in ("reference", "member_access") or not target or not source:
            continue
        parent = by_id.get(target.get("parent_id", ""), {})
        if target.get("kind") not in ("VarDecl", "FieldDecl") or parent.get("kind") in callable_kinds:
            continue
        rel = edge.get("file", "")
        try:
            data = data_cache.setdefault(rel, (root / rel).read_bytes())
        except OSError:
            continue
        start, end = int(edge.get("start", 0)), int(edge.get("end", 0))
        before = data[max(0, start - 3):start].decode(errors="ignore").rstrip()
        after = data[end:min(len(data), end + 4)].decode(errors="ignore").lstrip()
        write = after.startswith(("=", "+=", "-=", "*=", "/=", "%=", "++", "--")) and not after.startswith(("==", "=>"))
        write = write or before.endswith(("++", "--"))
        extras.append({**edge, "type": "nonlocal_write" if write else "nonlocal_read", "via": edge.get("type")})
    merged = {stable_json(e): e for e in [*edges, *extras]}
    return sorted(merged.values(), key=lambda x: (x.get("type", ""), x.get("from", ""), x.get("to", ""), x.get("file", ""), x.get("start", 0)))


def _warm_scan_snapshot(root: Path, cfg: dict[str, Any], paths: list[Path], core_version: str) -> dict[str, Any] | None:
    """Validate a clean published generation without loading semantic payloads."""
    try:
        with (index_dir(root) / "index.jsonl").open(encoding="utf-8") as stream:
            meta = json.loads(stream.readline())
        manifest = json.loads((index_dir(root) / "manifest.json").read_text(encoding="utf-8"))
        freshness = json.loads((index_dir(root) / "cache/freshness.json").read_text(encoding="utf-8")).get("files", {})
    except (OSError, json.JSONDecodeError):
        return None
    if (meta.get("format") != "CTXPP-INDEX/1" or meta.get("core_version") != core_version
            or meta.get("config_hash") != sha256_bytes(stable_json(cfg).encode())):
        return None
    manifest_files = dict(manifest.get("files", {}))
    inventory_files = {path.relative_to(root).as_posix() for path in paths}
    if set(manifest_files) != inventory_files:
        return None
    git_dirty = git_dirty_paths(root)
    for rel, expected_hash in manifest_files.items():
        source = root / rel
        if not source.is_file():
            return None
        stat = source.stat()
        work_count("files_statted")
        prior = freshness.get(rel, {})
        metadata_same = prior.get("size") == stat.st_size and prior.get("mtime_ns") == stat.st_mtime_ns
        if not (metadata_same and git_dirty is not None and rel not in git_dirty):
            if sha256_file(source) != expected_hash:
                return None
    store = open_query_store(root)
    if store:
        try:
            file_count, symbol_count, edge_count = store.counts()
        finally:
            store.close()
    else:
        file_count = symbol_count = edge_count = 0
        try:
            with (index_dir(root) / "index.jsonl").open("rb") as stream:
                for line in stream:
                    file_count += b'"record":"file"' in line
                    symbol_count += b'"record":"symbol"' in line
                    edge_count += b'"record":"edge"' in line
        except OSError:
            return None
    return {"backend": meta.get("backend"), "files": file_count, "symbols": symbol_count, "edges": edge_count,
            "failures": len(meta.get("failures", [])), "index": str(index_dir(root) / "index.jsonl")}


def scan_repository(root: Path, cfg: dict[str, Any], skill_root: Path, scoped: Iterable[str] = (), *,
                    refresh_tus: set[str] | None = None) -> dict[str, Any]:
    compdb_path = find_compdb(root, cfg)
    compdb = read_compdb(compdb_path, root)
    observed = observed_records(root)
    prior_recipes = successful_records(root)
    paths = source_paths(root, cfg, scoped, [*compdb, *observed, *prior_recipes])
    by_file = compdb_by_file([*compdb, *observed, *prior_recipes])
    core = find_core(skill_root)
    tokenizer = Tokenizer(root, str(cfg.get("tokenizer", "auto")), defer_writes=True)
    core_ok = False
    core_version = "unavailable"
    core_hash = ""
    if core:
        doctor_cache_path = index_dir(root) / "cache/core-doctor.json"
        stat = core.stat()
        try:
            cached_doctor = json.loads(doctor_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached_doctor = {}
        if (cached_doctor.get("path") == str(core) and cached_doctor.get("size") == stat.st_size
                and cached_doctor.get("mtime_ns") == stat.st_mtime_ns):
            core_ok = bool(cached_doctor.get("ok"))
            core_version = str(cached_doctor.get("version", "unknown"))
            core_hash = str(cached_doctor.get("hash", ""))
        else:
            doctor = run_core(core, ["doctor"])
            core_ok = doctor.returncode == 0
            core_hash = sha256_file(core)
            try:
                core_version = json.loads(doctor.stdout.splitlines()[0]).get("version", "unknown") if core_ok else "unavailable"
            except (json.JSONDecodeError, IndexError):
                core_version = "unknown"
            atomic_write(doctor_cache_path, (stable_json({"format": "CTXPP-CORE-CACHE/1", "path": str(core),
                                                         "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                                         "hash": core_hash, "ok": core_ok, "version": core_version}) + "\n").encode())

    raw_core: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    command_facts: dict[str, list[dict[str, Any]]] = {}
    cache_hits = 0
    jobs: list[dict[str, Any]] = []
    state_payload: dict[str, Any] | None = None
    prior_index_records: list[dict[str, Any]] = []
    prior_index_files: list[dict[str, Any]] = []
    prior_failure_summaries: list[dict[str, Any]] = []
    incremental_pending_tus: set[str] = set()
    if core_ok and (compdb or observed or prior_recipes or tuple(scoped)):
        cache_dir = index_dir(root) / "cache/tu"
        state_path = index_dir(root) / "cache/tu-state.json"
        try:
            previous_state = json.loads(state_path.read_text(encoding="utf-8"))
            previous_jobs = previous_state.get("jobs", {}) if previous_state.get("format") == "CTXPP-TU-CACHE/1" else {}
        except (OSError, json.JSONDecodeError):
            previous_jobs = {}
        hash_cache: dict[str, str | None] = {}

        def current_hash(rel: str) -> str | None:
            if rel not in hash_cache:
                path = root / rel
                hash_cache[rel] = sha256_file(path) if path.is_file() else None
            return hash_cache[rel]

        scoped_values = tuple(scoped)
        recipe_files: dict[Path, list[tuple[dict[str, Any], str, float]]] = {}
        for file, commands in by_file.items():
            recipe_files[file] = [(record, str(record.get("origin", "compile_database")), 1.0) for record in commands]
        if scoped_values:
            templates = [*compdb, *observed, *prior_recipes]
            for file in paths:
                if file.suffix not in (".cc", ".cpp", ".cxx", ".cu") or file in recipe_files:
                    continue
                record, origin, confidence = infer_recipe(file, templates)
                if record:
                    recipe_files[file] = [(record, origin, confidence)]
        for file, recipes in sorted(recipe_files.items(), key=lambda x: str(x[0])):
            try:
                rel = file.relative_to(root).as_posix()
            except ValueError:
                continue
            if is_excluded(rel, cfg) or file.suffix not in SOURCE_EXTENSIONS:
                continue
            for number, (record, origin, confidence) in enumerate(recipes):
                translated = translate_recipe(record, file)
                args = translated["clang_argv"]
                needs_preflight = Path(str(translated.get("compiler_identity", ""))).name == "nvcc" or origin.startswith("inferred")
                command_hash = sha256_bytes(stable_json({"directory": record.get("directory"), "args": args}).encode())
                command_facts.setdefault(rel, []).append({"hash": command_hash, "standard": language_standard(args), "args": args,
                    "directory": record.get("directory", str(root)), "origin": origin, "confidence": confidence,
                    "source_rewrite_usable": origin in ("compile_database", "observed_standalone")})
                state_key = f"{rel}\0{number}"
                prior = previous_jobs.get(state_key, {})
                source_hash = current_hash(rel)
                input_fingerprint = sha256_bytes(stable_json({"file": source_hash, "command": command_hash,
                                                              "core": core_version, "core_hash": core_hash, "tool": VERSION}).encode())
                cache_key = sha256_bytes(stable_json({"tu": rel, "configuration": number, "command": command_hash,
                                                      "core": core_version, "core_hash": core_hash, "tool": VERSION}).encode())
                jobs.append({"file": file, "rel": rel, "number": number, "record": record, "args": args,
                             "origin": origin,
                             "translated": translated, "needs_preflight": needs_preflight,
                             "command_hash": command_hash, "cache": cache_dir / f"{cache_key}.jsonl", "state_key": state_key,
                             "prior": prior, "source_hash": source_hash, "input_fingerprint": input_fingerprint,
                             "history_key": sha256_bytes(f"parse\0{rel}\0{command_hash}".encode()), "size": file.stat().st_size,
                             "cuda": file.suffix in (".cu", ".cuh"),
                             "memory_floor_mb": 1024 if file.suffix in (".cu", ".cuh") else 256})

        outputs: dict[tuple[str, int], tuple[int, str, str, bool]] = {}
        reusable: list[dict[str, Any]] = []
        pending = []
        for job in jobs:
            prior = job["prior"]
            dependencies = prior.get("dependencies", {}) if prior.get("input_fingerprint") == job["input_fingerprint"] else {}
            dependencies_current = bool(dependencies) and all(current_hash(rel) == digest for rel, digest in dependencies.items())
            targeted_reuse = refresh_tus is not None and job["rel"] not in refresh_tus and job["cache"].is_file() and bool(prior)
            if job["cache"].is_file() and (dependencies_current or targeted_reuse):
                reusable.append(job)
                cache_hits += 1
            else:
                usable, preflight_error = recipe_preflight(job["translated"], job["file"]) if job["needs_preflight"] else (True, "")
                if usable:
                    pending.append(job)
                else:
                    outputs[(job["rel"], job["number"])] = (1, "", preflight_error, False)

        if refresh_tus is None and not tuple(scoped) and not pending:
            current = _warm_scan_snapshot(root, cfg, paths, core_version)
            if current:
                tokenizer.flush()
                return {**current, "cache_hits": cache_hits}

        # Incremental scans reuse the compatible published index for unchanged
        # semantic records.  Cached TU JSONL is materialized only when no prior
        # index can safely supply that unchanged state.
        if pending and reusable and (index_dir(root) / "index.jsonl").is_file():
            try:
                candidate_prior = load_index(root)
                candidate_meta, prior_index_files, _, _ = partition_index(candidate_prior)
                if (candidate_meta.get("format") == "CTXPP-INDEX/1"
                        and candidate_meta.get("core_version") == core_version):
                    prior_index_records = candidate_prior
                    prior_failure_summaries = list(candidate_meta.get("failures", []))
                    incremental_pending_tus = {job["rel"] for job in pending}
            except CtxppError:
                prior_index_records = []
                prior_index_files = []
        if not prior_index_records:
            for job in reusable:
                payload = job["cache"].read_text(encoding="utf-8")
                work_count("tu_cache_payloads_materialized")
                work_count("tu_cache_payload_bytes", len(payload.encode()))
                outputs[(job["rel"], job["number"])] = (0, payload, "", True)

        def execute(job: dict[str, Any]) -> ParseExecution:
            measured = run_process_measured(
                [str(core), "scan", "--root", str(root), "--file", str(job["file"]), "--", *job["args"]],
                cwd=Path(job["record"].get("directory", root)),
            )
            return ParseExecution(job, measured.completed, measured.peak_memory_mb)

        configured_ceiling = cfg.get("tool", {}).get("max_workers")
        scheduler = ResourceScheduler(root, int(configured_ceiling) if isinstance(configured_ceiling, int) and configured_ceiling > 0 else None)
        completed_jobs = scheduler.run(pending, execute)
        next_state: dict[str, Any] = {job["state_key"]: dict(job["prior"]) for job in jobs if job not in pending and job["prior"]}
        stale_during_scan = False
        for execution in completed_jobs:
            job, proc = execution.job, execution.completed
            if (sha256_file(job["file"]) if job["file"].is_file() else None) != job["source_hash"]:
                work_count("stale_parse_results_rejected")
                outputs[(job["rel"], job["number"])] = (1, "", "source changed during parse", False)
                stale_during_scan = True
                continue
            outputs[(job["rel"], job["number"])] = (proc.returncode, proc.stdout, proc.stderr, False)
            if proc.returncode == 0:
                dependencies = {job["rel"]: str(job["source_hash"])}
                for line in proc.stdout.splitlines():
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    candidate = str(parsed.get("file", ""))
                    path = root / candidate
                    if candidate and path.is_file() and not is_excluded(candidate, cfg):
                        dependencies[candidate] = str(current_hash(candidate))
                    if parsed.get("record") == "include":
                        included = str(parsed.get("to", ""))
                        included_path = root / included
                        if included and included_path.is_file() and not is_excluded(included, cfg):
                            dependencies[included] = str(current_hash(included))
                if any((sha256_file(root / rel) if (root / rel).is_file() else None) != digest for rel, digest in dependencies.items()):
                    work_count("stale_parse_results_rejected")
                    outputs[(job["rel"], job["number"])] = (1, "", "dependency changed during parse", False)
                    stale_during_scan = True
                    continue
                atomic_write(job["cache"], proc.stdout.encode())
                persist_successful(root, job["record"], job["file"], job["origin"], atomic_write)
                next_state[job["state_key"]] = {"cache": job["cache"].name, "input_fingerprint": job["input_fingerprint"],
                                                "dependencies": dict(sorted(dependencies.items()))}
                work_count("tus_parsed")
                work_count("asts_constructed")
                work_count("semantic_records_updated", len(proc.stdout.splitlines()))
        if stale_during_scan:
            raise CtxppError("source changed during semantic scan; stale results discarded; retry")
        state_payload = {"format": "CTXPP-TU-CACHE/1", "jobs": dict(sorted(next_state.items()))}

        for job in sorted(jobs, key=lambda x: (x["rel"], x["number"])):
            if (job["rel"], job["number"]) not in outputs:
                continue
            returncode, stdout, stderr, _ = outputs[(job["rel"], job["number"])]
            if returncode != 0:
                failures.append({"file": job["rel"], "configuration": job["number"], "error": stderr.strip()})
                continue
            for line in stdout.splitlines():
                try:
                    parsed = json.loads(line)
                    parsed["translation_unit"] = job["rel"]
                    parsed["configuration_hash"] = job["command_hash"]
                    raw_core.append(parsed)
                except json.JSONDecodeError as exc:
                    failures.append({"file": job["rel"], "configuration": job["number"], "error": f"invalid core JSON: {exc}"})
    symbols, edges, observations = _merge_core_records(raw_core)
    if prior_index_records:
        _, prior_index_files, prior_symbols, prior_edges = partition_index(prior_index_records)
        retained_symbols: list[dict[str, Any]] = []
        for prior_symbol in prior_symbols:
            if prior_symbol.get("degraded"):
                continue
            prior_occurrences = list(prior_symbol.get("occurrences", []))
            if prior_occurrences:
                remaining = [occurrence for occurrence in prior_occurrences
                             if occurrence.get("translation_unit") not in incremental_pending_tus]
                if not remaining:
                    continue
                retained_symbols.append({**prior_symbol, "occurrences": remaining})
            elif prior_symbol.get("file") not in incremental_pending_tus:
                retained_symbols.append(dict(prior_symbol))
        by_symbol_id = {symbol["id"]: symbol for symbol in retained_symbols}
        for symbol in symbols:
            existing = by_symbol_id.get(symbol["id"])
            if not existing:
                by_symbol_id[symbol["id"]] = symbol
                continue
            occurrences = sorted({stable_json(occurrence): occurrence
                                  for occurrence in [*existing.get("occurrences", []), *symbol.get("occurrences", [])]}.values(),
                                 key=lambda occurrence: (occurrence.get("file", ""), occurrence.get("start", 0)))
            preferred = symbol if symbol.get("definition") or not existing.get("definition") else existing
            by_symbol_id[symbol["id"]] = {**preferred, "occurrences": occurrences}
        symbols = list(by_symbol_id.values())
        retained_edges = [edge for edge in prior_edges
                          if edge.get("translation_unit") not in incremental_pending_tus
                          and not (not edge.get("translation_unit") and edge.get("file") in incremental_pending_tus)]
        edges = list({stable_json(edge): edge for edge in [*retained_edges, *edges]}.values())
    symbols.extend(lexical_symbols(root, paths, symbols))
    symbols.sort(key=lambda x: (x.get("qualified_name", ""), x.get("file", ""), x.get("start", 0), x["id"]))
    edges = enrich_semantic_edges(root, symbols, edges)
    for obs in observations:
        for diagnostic in obs.get("diagnostics", []):
            if "error:" in diagnostic.lower():
                failures.append({"file": obs.get("file"), "configuration": obs.get("configuration_hash"), "error": diagnostic})

    observed_files: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        observed_files.setdefault(obs.get("file", ""), []).append(obs)
    files: list[dict[str, Any]] = []
    tus_by_file: dict[str, set[str]] = {}
    prior_files_by_path = {str(record.get("path", "")): record for record in prior_index_files}
    for rel, record in prior_files_by_path.items():
        tus_by_file[rel] = {str(tu) for tu in record.get("translation_units", [])
                            if str(tu) not in incremental_pending_tus}
    for symbol in symbols:
        for occurrence in symbol.get("occurrences", []):
            if occurrence.get("translation_unit"):
                tus_by_file.setdefault(occurrence.get("file", symbol.get("file", "")), set()).add(occurrence["translation_unit"])
    for record in raw_core:
        if record.get("record") == "include" and record.get("to") and record.get("translation_unit"):
            tus_by_file.setdefault(str(record["to"]), set()).add(str(record["translation_unit"]))
    observations_by_tu = {obs.get("translation_unit", obs.get("file", "")): obs for obs in observations}
    known_paths = {p.relative_to(root).as_posix(): p for p in paths}
    for symbol in symbols:
        rel = symbol.get("file")
        path = root / rel
        if path.is_file() and rel not in known_paths and not is_excluded(rel, cfg):
            known_paths[rel] = path
    for rel, path in sorted(known_paths.items()):
        data = path.read_bytes()
        text = data.decode("utf-8", errors="replace")
        tc = tokenizer.count(text)
        translation_units = sorted(tus_by_file.get(rel, set()) or ({rel} if rel in command_facts else set()))
        facts = [fact for tu in translation_units for fact in command_facts.get(tu, [])]
        facts = sorted({stable_json(f): f for f in facts}.values(), key=stable_json)
        prior_file = prior_files_by_path.get(rel, {})
        prior_tus = {str(tu) for tu in prior_file.get("translation_units", [])}
        diagnostics = ([] if prior_tus & incremental_pending_tus else list(prior_file.get("diagnostics", [])))
        diagnostics.extend(d for tu in translation_units for d in observations_by_tu.get(tu, {}).get("diagnostics", []))
        new_includes = {e.get("to") for e in raw_core if e.get("record") == "include" and e.get("from") == rel}
        includes = new_includes if rel in incremental_pending_tus else set(prior_file.get("includes", [])) | new_includes
        status = "unobserved" if not facts else ("error" if any("error:" in d.lower() for d in diagnostics) else "ok")
        files.append({
            "record": "file", "path": rel, "hash": sha256_bytes(data),
            "command_hashes": sorted({x["hash"] for x in facts}),
            "standards": sorted({x["standard"] for x in facts}) or ["unknown"],
            "commands": facts, "tokens": tc.count, "token_exact": tc.exact, "tokenizer": tc.identity,
            "bytes": len(data), "lines": line_count(data), "classification": classify_file(rel),
            "translation_units": translation_units,
            "parse_status": status, "diagnostics": diagnostics,
            "symbols": sorted(s["id"] for s in symbols if s.get("file") == rel),
            "includes": sorted(include for include in includes if include),
            "route": None,
        })
    semantic_symbols = [symbol for symbol in symbols if not symbol.get("degraded")]
    lexical_count = len(symbols) - len(semantic_symbols)
    backend = "libclang-runtime" if core_ok and semantic_symbols else "degraded-text-routing"
    failure_summaries = [*prior_failure_summaries, *summarize_diagnostics(root, failures, "scan")]
    failure_summaries = list({stable_json(failure): failure for failure in failure_summaries}.values())
    meta = {
        "record": "meta", "format": "CTXPP-INDEX/1", "tool_version": VERSION, "root": str(root),
        "generated_at": 0, "deterministic": True, "backend": backend, "semantic": backend != "degraded-text-routing",
        "core_version": core_version, "compilation_database": str(compdb_path) if compdb_path else None,
        "config_hash": sha256_bytes(stable_json(cfg).encode()), "failures": failure_summaries,
        "coverage": {"semantic": len(semantic_symbols), "lexical_only": lexical_count},
        "incomplete": backend == "degraded-text-routing" or bool(failure_summaries) or bool(lexical_count),
    }
    output_records = [meta, *files, *symbols, *edges]
    out_dir = index_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    index_data = ("\n".join(stable_json(x) for x in output_records) + "\n").encode()
    index_hash = sha256_bytes(index_data)
    manifest = {"format": "CTXPP-MANIFEST/1", "index_hash": index_hash, "files": {f["path"]: f["hash"] for f in files}}
    try:
        build_query_store(root, index_hash, files, symbols, edges,
                          {**meta, "_profile": cfg.get("profile"), "_source_write": bool(cfg.get("source_write"))})
    except Exception:
        # Private acceleration is disposable; readers retain the compatible public index.
        pass
    with publication_lock(root):
        checked_jobs = jobs if refresh_tus is None else [job for job in jobs if job["rel"] in refresh_tus]
        for job in checked_jobs:
            expected = state_payload.get("jobs", {}).get(job["state_key"], {}).get("dependencies", {}) if state_payload else {}
            if expected and any(not (root / rel).is_file() or sha256_file(root / rel) != digest for rel, digest in expected.items()):
                work_count("stale_parse_results_rejected")
                raise CtxppError(f"source changed during semantic scan: {job['rel']}; retry")
        atomic_write(out_dir / "index.jsonl", index_data)
        atomic_write(out_dir / "manifest.json", (stable_json(manifest) + "\n").encode())
        if state_payload is not None:
            atomic_write(out_dir / "cache/tu-state.json", (stable_json(state_payload) + "\n").encode())
        freshness = {f["path"]: {"hash": f["hash"], "size": (root / f["path"]).stat().st_size,
                                  "mtime_ns": (root / f["path"]).stat().st_mtime_ns} for f in files if (root / f["path"]).is_file()}
        config_path = root / ".ctxpp.toml"
        freshness_payload = {"format": "CTXPP-FRESHNESS/1", "files": freshness,
                             "config_file_hash": sha256_file(config_path) if config_path.is_file() else None}
        atomic_write(out_dir / "cache/freshness.json", (stable_json(freshness_payload) + "\n").encode())
        stale_semantic_tus = [] if refresh_tus is None else sorted(set(
            job["rel"] for job in jobs if job["rel"] not in refresh_tus and any(
                (root / rel).is_file() and sha256_file(root / rel) != digest
                for rel, digest in job.get("prior", {}).get("dependencies", {}).items())))
        readiness = {"format": "CTXPP-READINESS/1", "semantic_stale_tus": stale_semantic_tus,
                     "route_ready": True, "read_ready": not stale_semantic_tus,
                     "rewrite_ready": not stale_semantic_tus and not failures and not lexical_count and backend != "degraded-text-routing"}
        atomic_write(out_dir / "cache/readiness.json", (stable_json(readiness) + "\n").encode())
    tokenizer.flush()
    return {"backend": backend, "files": len(files), "symbols": len(symbols), "edges": len(edges), "failures": len(failure_summaries),
            "cache_hits": cache_hits, "index": str(out_dir / "index.jsonl")}


def index_status(root: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    store = open_query_store(root)
    try:
        if store:
            meta = store.meta()
            files = store.files()
            file_count, symbol_count, edge_count = store.counts()
        else:
            records = load_index(root)
            meta, files, symbols, edges = partition_index(records)
            file_count, symbol_count, edge_count = len(files), len(symbols), len(edges)
    except CtxppError:
        return {"present": False, "stale": True, "reasons": ["index-missing"]}
    finally:
        if store:
            store.close()
    try:
        freshness_payload = json.loads((index_dir(root) / "cache/freshness.json").read_text(encoding="utf-8"))
        freshness = freshness_payload.get("files", {}) if freshness_payload.get("format") == "CTXPP-FRESHNESS/1" else {}
    except (OSError, json.JSONDecodeError):
        freshness = {}
    reasons = []
    git_dirty = git_dirty_paths(root)
    try:
        readiness = json.loads((index_dir(root) / "cache/readiness.json").read_text(encoding="utf-8"))
        stale_tus = readiness.get("semantic_stale_tus", []) if readiness.get("format") == "CTXPP-READINESS/1" else []
    except (OSError, json.JSONDecodeError):
        stale_tus = []
    if meta.get("config_hash") != sha256_bytes(stable_json(cfg).encode()):
        reasons.append("config-changed")
    for f in files:
        path = root / f["path"]
        if not path.is_file():
            reasons.append(f"missing:{f['path']}")
        else:
            stat = path.stat()
            prior = freshness.get(f["path"], {})
            metadata_same = prior.get("size") == stat.st_size and prior.get("mtime_ns") == stat.st_mtime_ns
            trusted_clean = metadata_same and git_dirty is not None and f["path"] not in git_dirty
            if not trusted_clean:
                if sha256_file(path) != f.get("hash"):
                    reasons.append(f"changed:{f['path']}")
    reasons.extend(f"semantic-stale:{rel}" for rel in stale_tus)
    return {
        "present": True, "stale": bool(reasons), "reasons": reasons[:20], "backend": meta.get("backend"),
        "semantic": bool(meta.get("semantic")), "incomplete": bool(meta.get("incomplete")),
        "failures": summarize_diagnostics(root, list(meta.get("failures", [])), "status"),
        "files": file_count, "symbols": symbol_count, "edges": edge_count, "profile": cfg.get("profile"),
        "source_write_configured": bool(cfg.get("source_write")),
        "source_write_safe": bool(cfg.get("source_write")) and bool(meta.get("semantic")) and not reasons and not meta.get("failures"),
    }


def _lexical_identifiers(data: bytes) -> set[str]:
    """Conservative C++ lexical anchors only; never used as semantic proof."""
    text = data.decode("utf-8", errors="replace")
    result: set[str] = set()
    i = 0
    while i < len(text):
        if text.startswith("//", i):
            end = text.find("\n", i + 2)
            i = len(text) if end < 0 else end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        char = text[i]
        if char in ('"', "'"):
            quote = char
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                elif text[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            continue
        if char == "_" or char.isalpha():
            start = i
            i += 1
            while i < len(text) and (text[i] == "_" or text[i].isalnum()):
                i += 1
            identifier = text[start:i]
            result.add(identifier.lower())
            result.update(ranked_terms(identifier))
            continue
        i += 1
    return result


def ensure_query_fresh(root: Path, cfg: dict[str, Any], skill_root: Path, query: str) -> bool:
    """Refresh only selected stale TUs; clean or unrelated queries remain compiler-free."""
    store = open_query_store(root)
    overlay_matches = [symbol for symbol in lexical_overlay(root)
                       if query in (symbol.get("id"), symbol.get("name"), symbol.get("qualified_name"))]
    exact_matches = overlay_matches
    if store:
        try:
            files = store.files()
            exact_matches = store.exact(query, 8) or overlay_matches
            exact_cached = bool(exact_matches)
        finally:
            store.close()
    else:
        files = []
        exact_cached = bool(exact_matches)
    recipe_files = set()
    for record in [*observed_records(root), *successful_records(root)]:
        directory = Path(record.get("directory", root))
        source = Path(record.get("file", ""))
        source = source if source.is_absolute() else directory / source
        try: recipe_files.add(source.resolve().relative_to(root).as_posix())
        except (OSError, ValueError): pass
    promotion = sorted({str(symbol.get("file", "")) for symbol in exact_matches
                        if symbol.get("degraded") and symbol.get("file") in recipe_files})
    if promotion:
        scan_repository(root, cfg, skill_root, promotion)
        return True
    if not exact_cached:
        query_anchors = [term for term in query_terms(query) if len(term) > 1]
        for path in source_paths(root, cfg):
            identifiers = _lexical_identifiers(path.read_bytes())
            work_count("files_lexically_refreshed")
            candidates = lexical_symbols(root, [path], []) if query.isidentifier() else []
            exact_declaration = any(query in (symbol.get("name"), symbol.get("qualified_name")) for symbol in candidates)
            if exact_declaration or (not query.isidentifier() and query_anchors and all(term in identifiers for term in query_anchors)):
                refresh_lexical_overlay(root, [path])
                try:
                    rel = path.resolve().relative_to(root.resolve()).as_posix()
                except (OSError, ValueError):
                    return True
                indexed = next((record for record in files if record.get("path") == rel), None)
                if not indexed or not indexed.get("translation_units"):
                    return True
                break
    if not store:
        return False
    try:
        freshness_payload = json.loads((index_dir(root) / "cache/freshness.json").read_text(encoding="utf-8"))
        freshness = freshness_payload.get("files", {})
    except (OSError, json.JSONDecodeError):
        freshness = {}
    try:
        readiness = json.loads((index_dir(root) / "cache/readiness.json").read_text(encoding="utf-8"))
        logical_stale = set(readiness.get("semantic_stale_tus", []))
    except (OSError, json.JSONDecodeError):
        logical_stale = set()
    dirty: set[str] = set()
    git_dirty = git_dirty_paths(root)
    for record in files:
        rel = str(record.get("path", ""))
        source = root / rel
        prior = freshness.get(rel, {})
        if not source.is_file():
            dirty.add(rel)
            continue
        stat = source.stat()
        work_count("files_statted")
        metadata_same = stat.st_size == prior.get("size") and stat.st_mtime_ns == prior.get("mtime_ns")
        trusted_clean = metadata_same and git_dirty is not None and rel not in git_dirty
        if not trusted_clean:
            if sha256_file(source) != record.get("hash"):
                dirty.add(rel)
    matches = resolve_symbols(root, query, 24)
    selected_files = {str(symbol.get("file", "")) for symbol in matches}
    by_file = {str(record.get("path", "")): record for record in files}
    refresh: set[str] = set()
    preferred_tus = {str(tu) for rel in selected_files for tu in by_file.get(rel, {}).get("translation_units", [])
                     if Path(rel).suffix in (".cc", ".cpp", ".cxx", ".cu")}
    selected_tus = {str(tu) for rel in selected_files for tu in by_file.get(rel, {}).get("translation_units", [])}

    def add_tus(rel: str) -> None:
        record = by_file.get(rel, {})
        tus = sorted(str(tu) for tu in record.get("translation_units", []))
        if tus:
            relevant = [tu for tu in tus if tu in preferred_tus]
            refresh.update(relevant or tus[:1])
        elif Path(rel).suffix in (".cc", ".cpp", ".cxx", ".cu"):
            refresh.add(rel)

    for rel in selected_files:
        record = by_file.get(rel, {})
        tus = set(str(x) for x in record.get("translation_units", []))
        if rel in dirty or tus & logical_stale:
            add_tus(rel)
    try:
        tu_state = json.loads((index_dir(root) / "cache/tu-state.json").read_text(encoding="utf-8")).get("jobs", {})
    except (OSError, json.JSONDecodeError):
        tu_state = {}
    for key, record in tu_state.items():
        tu = key.split("\0", 1)[0]
        if tu in selected_tus and any(rel in dirty for rel in record.get("dependencies", {})):
            refresh.add(tu)
    terms = [term for term in query_terms(query) if len(term) > 1]
    location_file = query.rpartition(":")[0] if query.rpartition(":")[2].isdigit() else ""
    for rel in sorted(dirty):
        source = root / rel
        if not source.is_file():
            add_tus(rel)
            continue
        identifiers = _lexical_identifiers(source.read_bytes())
        work_count("files_lexically_refreshed")
        if rel == location_file or (terms and all(term in identifiers for term in terms)):
            add_tus(rel)
    refresh.update(logical_stale & {tu for rel in selected_files for tu in by_file.get(rel, {}).get("translation_units", [])})
    if not refresh:
        return False
    scan_repository(root, cfg, skill_root, refresh_tus=refresh)
    return True


def _location_target(root: Path, target: str) -> tuple[str, int] | None:
    path_text, separator, line_text = target.rpartition(":")
    if not separator or not line_text.isdigit():
        return None
    path = Path(path_text)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            return None
    return path.as_posix().removeprefix("./"), int(line_text)


def _lexical_location_matches(overlay: list[dict[str, Any]], file: str, line: int, limit: int) -> list[dict[str, Any]]:
    matches = [symbol for symbol in overlay
               if symbol.get("file") == file
               and int(symbol.get("line", 0)) <= line <= int(symbol.get("end_line", symbol.get("line", 0)))]
    matches.sort(key=lambda symbol: (
        not (symbol.get("definition") and symbol.get("target_complete")),
        not symbol.get("target_complete"),
        int(symbol.get("end", 0)) - int(symbol.get("start", 0)),
        str(symbol.get("qualified_name", "")),
        str(symbol.get("id", "")),
    ))
    # A location denotes the tightest complete lexical entity.  Returning broader
    # containing declarations as peers would turn a precise location into ambiguity.
    return matches[:1] if matches else []


def _location_matches(root: Path, target: str, semantic: list[dict[str, Any]],
                      overlay: list[dict[str, Any]], limit: int) -> list[dict[str, Any]] | None:
    location = _location_target(root, target)
    if location is None:
        return None
    file, line = location
    lexical = _lexical_location_matches(overlay, file, line, limit)
    if semantic:
        # Preserve the established semantic ordering and identity for every valid
        # semantic containment.  Lexical candidates are a completeness fallback,
        # not competitors with a precise semantic result.
        return semantic[:limit]
    return lexical


def resolve_symbols(root: Path, target: str, limit: int = 8) -> list[dict[str, Any]]:
    overlay = lexical_overlay(root)
    overlay_exact = [symbol for symbol in overlay if target in (symbol.get("id"), symbol.get("name"), symbol.get("qualified_name"))]
    if overlay_exact:
        return sorted(overlay_exact, key=lambda symbol: (symbol.get("qualified_name") != target, symbol.get("file", "")))[:limit]
    store = open_query_store(root)
    if store:
        try:
            if target.startswith(("usr:", "c:")):
                exact = [s for s in store.exact(target, limit) if s.get("id") == target]
                if exact:
                    return exact
            location = _location_target(root, target)
            if location is not None:
                semantic = store.location(location[0], location[1], limit)
                return _location_matches(root, target, semantic, overlay, limit) or []
            exact = store.exact(target, limit)
            if exact:
                return sorted(exact, key=lambda s: (s.get("qualified_name") != target, not s.get("definition"), s.get("file", "")))[:limit]
            query = query_terms(target)
            return rank_candidates(target, [*store.candidates(query), *overlay], limit)
        finally:
            store.close()
    records = load_index(root)
    _, files, symbols, _ = partition_index(records)
    location = _location_target(root, target)
    if location is not None:
        line = location[1]
        semantic = [symbol for symbol in symbols
                    if symbol.get("file") == location[0]
                    and int(symbol.get("line", 0)) <= line <= int(symbol.get("end_line", symbol.get("line", 0)))]
        semantic.sort(key=lambda symbol: (int(symbol.get("end", 0)) - int(symbol.get("start", 0)),
                                          str(symbol.get("qualified_name", ""))))
        return _location_matches(root, target, semantic[:limit], overlay, limit) or []
    if not symbols:
        return rank_candidates(target, [*overlay, *degraded_locations(root, target, files, limit)], limit)
    if target.startswith("usr:") or target.startswith("c:"):
        exact = [s for s in symbols if s["id"] == target]
        if exact:
            return exact[:limit]
    exact = [s for s in symbols if target in (s.get("qualified_name"), s.get("name"), s.get("id"))]
    if exact:
        return sorted(exact, key=lambda s: (s.get("qualified_name") != target, not s.get("definition"), s.get("file", "")))[:limit]
    query = query_terms(target)
    return rank_candidates(target, [*symbols, *overlay], limit)


def query_terms(query: str) -> list[str]:
    return ranked_terms(query)


def degraded_locations(root: Path, target: str, files: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    results = []
    needle = target.encode()
    for record in files:
        path = root / record["path"]
        data = path.read_bytes()
        offset = data.find(needle)
        if offset >= 0:
            results.append({"record": "location", "id": f"text:{record['path']}:{offset}", "name": target,
                            "qualified_name": target, "kind": "text-match", "file": record["path"], "start": offset,
                            "end": offset + len(needle), "line": data[:offset].count(b"\n") + 1, "definition": False,
                            "tokens": max(1, len(needle) // 4), "degraded": True})
    return results[:limit]


@timed("route_ranking")
def route_symbols(root: Path, query: str, limit: int = 8) -> list[dict[str, Any]]:
    matches = resolve_symbols(root, query, limit=limit * 3)
    base_ids = {s.get("id") for s in matches}
    store = open_query_store(root)
    if store:
        try:
            edges = [edge for sid in sorted(base_ids) for edge in store.edges_for(str(sid))]
            work_count("graph_edges_traversed", len(edges))
            source_ids = {edge.get("from") for edge in edges if edge.get("type") == "test_relationship" and edge.get("to") in base_ids}
            by_id = store.symbol_ids(source_ids)
        finally:
            store.close()
    else:
        _, _, symbols, edges = partition_index(load_index(root))
        work_count("graph_edges_traversed", len(edges))
        by_id = {s["id"]: s for s in symbols}
    for edge in edges:
        if edge.get("type") == "test_relationship" and edge.get("to") in base_ids and edge.get("from") in by_id:
            matches.append({**by_id[edge["from"]], "_route_test_bonus": 20})
    matches = list({s["id"]: s for s in matches}.values())
    return rank_candidates(query, matches, limit)


INTENT_EDGE_WEIGHT = {
    "understand": {"containment": 100, "type_use": 90, "macro_expansion": 90, "call": 60, "member_access": 70, "inheritance": 80, "reference": 30},
    "edit": {"containment": 100, "type_use": 95, "macro_expansion": 95, "member_access": 90, "call": 70, "reference": 50, "inheritance": 80},
    "debug": {"call": 95, "member_access": 90, "reference": 80, "type_use": 70, "containment": 80, "macro_expansion": 80},
    "test": {"reference": 90, "call": 85, "containment": 80, "type_use": 70, "macro_expansion": 70},
    "api": {"containment": 100, "inheritance": 95, "type_use": 90, "call": 55, "reference": 70},
    "performance": {"call": 100, "member_access": 95, "type_use": 90, "reference": 85, "containment": 75, "macro_expansion": 90},
}


def source_text(root: Path, symbol: dict[str, Any]) -> str:
    data = (root / symbol["file"]).read_bytes()
    work_count("source_bytes_read", len(data))
    start, end = int(symbol.get("start", 0)), int(symbol.get("end", 0))
    if not (0 <= start <= end <= len(data)):
        raise CtxppError(f"invalid source range for {symbol.get('qualified_name')}")
    return data[start:end].decode("utf-8", errors="replace")


_COMPLETE_TARGET_INTENTS = {"api", "edit", "understand", "debug", "performance", "test"}


def recover_target_representation(root: Path, target: dict[str, Any], intent: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recover a truthful target range without changing semantic-index behavior."""
    recovered = dict(target)
    degraded = bool(recovered.get("degraded"))
    definition = bool(recovered.get("definition"))
    range_valid = False
    try:
        source = root / str(recovered["file"])
        data = source.read_bytes()
        start, end = int(recovered.get("start", -1)), int(recovered.get("end", -1))
        range_valid = 0 <= start < end <= len(data)
        if recovered.get("source_hash") and recovered.get("source_hash") != sha256_bytes(data):
            range_valid = False
    except (KeyError, OSError, TypeError, ValueError):
        data = b""

    if not degraded:
        kind = "semantic-definition" if definition else "semantic-declaration"
        record_kind = str(recovered.get("kind", ""))
        record_like = "Record" in record_kind or record_kind in ("EnumDecl", "CXXRecordDecl")
        definition_required = record_like and intent in _COMPLETE_TARGET_INTENTS
        complete = range_valid and (definition or not definition_required)
        coverage = {"representation_kind": kind, "degraded": False, "range_valid": range_valid,
                    "is_definition": definition, "body_present": bool(recovered.get("body_present", definition)),
                    "target_complete": complete, "contracts_complete": True,
                    "required_dependencies_complete": True, "elisions_declared": True}
        recovered.update(coverage)
        return recovered, coverage

    # Lexical records from older overlays and text matches may contain only the name.
    # Re-lex this one canonical file and prefer its complete definition for content intents.
    candidates: list[dict[str, Any]] = []
    if data:
        try:
            candidates = [symbol for symbol in lexical_symbols(root, [root / str(recovered["file"])], [])
                          if symbol.get("name") == recovered.get("name")]
        except (OSError, ValueError):
            candidates = []
    if candidates:
        want_definition = intent in _COMPLETE_TARGET_INTENTS
        candidates.sort(key=lambda symbol: (
            not (want_definition and symbol.get("definition") and symbol.get("target_complete")),
            symbol.get("id") != target.get("id"),
            not symbol.get("target_complete"),
            -(int(symbol.get("end", 0)) - int(symbol.get("start", 0))),
            int(symbol.get("start", 0)),
        ))
        recovered = dict(candidates[0])
        data = (root / str(recovered["file"])).read_bytes()
        range_valid = bool(recovered.get("range_valid")) and recovered.get("source_hash") == sha256_bytes(data)
        definition = bool(recovered.get("definition"))

    complete = range_valid and bool(recovered.get("target_complete"))
    if intent in _COMPLETE_TARGET_INTENTS:
        complete = complete and definition and bool(recovered.get("body_present"))
    if complete:
        kind = "canonical-definition" if definition else "canonical-declaration"
    elif range_valid and int(recovered.get("end", 0)) > int(recovered.get("start", 0)):
        kind = "lexical-declaration"
    elif recovered.get("name"):
        kind = "name-only"
    else:
        kind = "unavailable"
    coverage = {"representation_kind": kind, "degraded": True, "range_valid": range_valid,
                "is_definition": definition, "body_present": bool(recovered.get("body_present")),
                "target_complete": complete, "contracts_complete": complete,
                # A lexical declaration has no trustworthy typed dependency graph.  The
                # canonical target is useful, but API sufficiency remains conservative.
                "required_dependencies_complete": intent not in _COMPLETE_TARGET_INTENTS,
                "elisions_declared": complete}
    recovered.update(coverage)
    return recovered, coverage


def choose_fragments(root: Path, target: dict[str, Any], intent: str, budget: int, tokenizer: Tokenizer) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, bool, dict[str, Any]]:
    target, coverage = recover_target_representation(root, target, intent)
    records = load_index(root)
    _, _, symbols, edges = partition_index(records)
    by_id = {s["id"]: s for s in symbols}
    target_id = target["id"]
    candidates: dict[str, tuple[int, str]] = {}
    weights = INTENT_EDGE_WEIGHT[intent]
    for edge in edges:
        work_count("graph_edges_traversed")
        if edge.get("from") == target_id and edge.get("to") in by_id:
            candidates[edge["to"]] = max(candidates.get(edge["to"], (0, "")), (weights.get(edge.get("type"), 10), edge.get("type", "reference")))
        if edge.get("to") == target_id and edge.get("from") in by_id:
            weight = 85 if edge.get("type") == "containment" else (75 if intent in ("debug", "test", "api") else 45)
            candidates[edge["from"]] = max(candidates.get(edge["from"], (0, "")), (weight, "caller" if edge.get("type") != "containment" else "container"))
    if intent in ("edit", "test", "debug"):
        for symbol in symbols:
            if ("test" in symbol.get("file", "").lower() or "test" in symbol.get("name", "").lower()) and target.get("name", "").lower() in (symbol.get("signature", "") + symbol.get("contract", "")).lower():
                candidates[symbol["id"]] = max(candidates.get(symbol["id"], (0, "")), (70, "test"))
    target_text = source_text(root, target)
    target_tokens = tokenizer.count(target_text).count
    selected = [{"symbol": target, "role": "target", "mandatory": True, "text": target_text, "tokens": target_tokens}]
    used = target_tokens
    omitted = []
    ranked = []
    for sid, (utility, role) in candidates.items():
        symbol = by_id[sid]
        if symbol.get("file") == target.get("file") and symbol.get("start") == target.get("start"):
            continue
        if (symbol.get("file") == target.get("file") and target.get("start", 0) <= symbol.get("start", -1)
                and symbol.get("end", -1) <= target.get("end", 0)):
            continue
        try:
            text = source_text(root, symbol)
        except CtxppError:
            continue
        tokens = tokenizer.count(text).count
        mandatory = role in ("container",) or (symbol.get("body_required") and role in ("type_use", "macro_expansion"))
        ranked.append((not mandatory, -(utility / max(tokens, 1)), -utility, symbol.get("qualified_name", ""), symbol.get("id", ""), symbol, role, text, tokens, mandatory))
    for _, _, _, _, _, symbol, role, text, tokens, mandatory in sorted(ranked):
        item = {"symbol": symbol, "role": role, "mandatory": mandatory, "text": text, "tokens": tokens}
        if used + tokens <= budget:
            selected.append(item); used += tokens
        else:
            omitted.append(item)
    mandatory_total = target_tokens + sum(x["tokens"] for x in omitted if x["mandatory"]) + sum(x["tokens"] for x in selected[1:] if x["mandatory"])
    mandatory_omitted = any(x["mandatory"] for x in omitted)
    reason = ""
    if not coverage["range_valid"] or not coverage["target_complete"]:
        reason = "incomplete-target-representation"
    elif mandatory_total > budget:
        reason = "mandatory-context-over-budget"
    elif mandatory_omitted:
        reason = "mandatory-context-over-budget"
    elif not coverage["required_dependencies_complete"]:
        reason = "required-dependency-unresolved"
    elif not coverage["contracts_complete"]:
        reason = "contract-coverage-partial"
    sufficient = not reason
    coverage["sufficiency_reason"] = reason
    return selected, omitted, mandatory_total, sufficient, coverage


def lexical_api_skeleton(text: str, symbol: dict[str, Any], tokenizer: Tokenizer) -> str | None:
    """Elide only large inline method bodies; retain the complete record surface."""
    canonical_tokens = tokenizer.count(text).count
    if canonical_tokens <= 1200 or not symbol.get("range_valid") or not symbol.get("body_present"):
        return None
    data = text.encode()
    tokens = _cpp_tokens(data)
    outer = next((index for index, token in enumerate(tokens) if token.text == "{"), -1)
    if outer < 0:
        return None
    depth = 0
    boundary = outer + 1
    replacements: list[tuple[int, int]] = []
    index = outer
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "preprocessor":
            return None
        if token.text == "{":
            if depth == 1:
                prefix = [item.text for item in tokens[boundary:index] if item.kind != "comment"]
                # Restrict the V1 skeleton to unmistakable member-function bodies.
                if ")" in prefix and not any(word in prefix for word in ("class", "struct", "union", "enum", "=")):
                    nested = 1
                    closing = index + 1
                    while closing < len(tokens) and nested:
                        if tokens[closing].kind == "preprocessor":
                            return None
                        if tokens[closing].text == "{": nested += 1
                        elif tokens[closing].text == "}": nested -= 1
                        closing += 1
                    if nested:
                        return None
                    replacements.append((token.end, tokens[closing - 1].start))
                    boundary = closing
                    index = closing
                    continue
            depth += 1
        elif token.text == "}":
            depth -= 1
            if depth == 0:
                break
        if depth == 1 and token.text in (";", "}"):
            boundary = index + 1
        index += 1
    if not replacements:
        return None
    candidate = bytearray(data)
    for start, end in reversed(replacements):
        candidate[start:end] = b"/*...*/"
    rendered = candidate.decode(errors="replace")
    candidate_tokens = tokenizer.count(rendered).count
    if candidate_tokens * 100 > canonical_tokens * 60:
        return None
    return rendered


@timed("slice_and_view_assembly")
def render_bundle(root: Path, target: dict[str, Any], intent: str, budget: int, tokenizer: Tokenizer, *, compact: bool = False,
                  core: Path | None = None, layout: str = "navigable") -> tuple[str, dict[str, Any], dict[str, Any]]:
    selected, omitted, mandatory_total, sufficient, coverage = choose_fragments(root, target, intent, budget, tokenizer)
    target = selected[0]["symbol"]
    header = f"CTXPP/1 intent={intent} budget={budget} sufficient={1 if sufficient else 0} readonly=1 layout={layout}"
    if coverage.get("degraded") and not sufficient and coverage.get("sufficiency_reason"):
        header += f" reason={coverage['sufficiency_reason']}"
    rendered = []
    records = load_index(root)
    _, _, all_symbols, all_edges = partition_index(records)
    mappings = []
    glossary: dict[str, str] = {}
    compacted_by_id: dict[str, tuple[str, list[dict[str, Any]]] | None] = {}
    if compact and core:
        compact_jobs = []
        for item in selected:
            if intent == "edit" and item["role"] == "target":
                continue
            symbol = item["symbol"]
            compact_jobs.append({"history_key": f"compact:{symbol['id']}",
                                 "input_fingerprint": sha256_bytes((sha256_file(root / symbol["file"]) + sha256_file(core)).encode()),
                                 "size": int(symbol.get("end", 0)) - int(symbol.get("start", 0)),
                                 "memory_floor_mb": 256,
                                 "symbol": symbol})

        @dataclass
        class CompactExecution:
            symbol_id: str
            value: tuple[str, list[dict[str, Any]]] | None
            peak_memory_mb: int = 512

        def execute_compact(job: dict[str, Any]) -> CompactExecution:
            work_count("compact_view_transforms")
            return CompactExecution(job["symbol"]["id"], compact_range(root, core, job["symbol"]))

        for result in ResourceScheduler(root).run(compact_jobs, execute_compact):
            compacted_by_id[result.symbol_id] = result.value
    for item in selected:
        symbol = item["symbol"]
        text = item["text"]
        mode = "verbatim"
        degraded_target = item["role"] == "target" and bool(symbol.get("degraded"))
        if compact and degraded_target and intent == "api":
            skeleton = lexical_api_skeleton(text, symbol, tokenizer)
            if skeleton is not None:
                text = skeleton
                mode = "lexical-structural"
        if compact and not degraded_target and not (intent == "edit" and item["role"] == "target") and core:
            compacted = compacted_by_id.get(symbol["id"])
            if compacted:
                text, token_maps = compacted
                mode = "lexically-compacted"
            else:
                token_maps = []
        else:
            token_maps = []
        rename_maps: list[dict[str, Any]] = []
        if compact and not degraded_target and not (intent == "edit" and item["role"] == "target"):
            text, fragment_glossary, rename_maps = abbreviate_generated_fragment(
                root, symbol, text, token_maps, all_symbols, all_edges, tokenizer
            )
            if fragment_glossary:
                glossary.update(fragment_glossary)
                mode = "renamed" if mode == "verbatim" else mode + "+renamed"
        label = "target" if item["role"] == "target" else item["role"]
        mode_tag = "v" if mode == "verbatim" else "c"
        annotation = f"@{label} {symbol['file']}:{symbol.get('line', 0)} {symbol.get('qualified_name', symbol.get('name', ''))} {mode_tag}"
        rendered.append((annotation, text.rstrip(), symbol, mode, token_maps, rename_maps))

    chunks = [header]
    if glossary:
        chunks.append("//@abbr:" + ",".join(f"{short}={long}" for short, long in sorted(glossary.items())))
    for annotation, text, symbol, mode, token_maps, rename_maps in rendered:
        chunks.append(annotation)
        generated_line_start = sum(x.count("\n") + 1 for x in chunks[:-1]) + 1
        chunks.append(text)
        generated_line_end = generated_line_start + text.count("\n")
        mappings.append({
            "canonical_file": symbol["file"], "canonical_start": symbol.get("start"), "canonical_end": symbol.get("end"),
            "canonical_line": symbol.get("line"), "canonical_end_line": symbol.get("end_line"),
            "generated_line_start": generated_line_start, "generated_line_end": generated_line_end,
            "symbol_id": symbol["id"], "mode": mode, "transform_rules": ["VIEW-LEX-1"] if mode != "verbatim" else [],
            "token_maps": token_maps, "rename_maps": rename_maps,
        })
    omitted_tokens = sum(x["tokens"] for x in omitted)
    chunks.append(f"@omitted n={len(omitted)} tok={omitted_tokens} mandatory={mandatory_total}; explain target map")
    text = "\n".join(chunks) + "\n"
    count = tokenizer.count(text)
    selected_source_before = tokenizer.count("\n".join(item["text"] for item in selected)).count
    selected_source_after = tokenizer.count(("//@abbr:" + ",".join(f"{short}={long}" for short, long in sorted(glossary.items())) + "\n" if glossary else "")
                                            + "\n".join(item[1] for item in rendered)).count
    report = {
        "tokens": count.count, "token_exact": count.exact, "tokenizer": count.identity,
        "bytes": len(text.encode()), "lines": text.count("\n"), "omitted": len(omitted), "omitted_tokens": omitted_tokens,
        "mandatory_tokens": mandatory_total, "sufficient": sufficient, "glossary": glossary,
        "selected_source_tokens_before": selected_source_before, "selected_source_tokens_after": selected_source_after,
        "selected_source_token_delta": selected_source_before - selected_source_after,
    }
    if coverage.get("degraded"):
        report.update({"representation_kind": coverage["representation_kind"],
                       "target_complete": coverage["target_complete"],
                       "sufficiency_reason": coverage.get("sufficiency_reason", "")})
    source_map = {"format": "CTXPP-MAP/1", "readonly": True, "target": target["id"], "mappings": mappings, "report": report}
    return text, source_map, report


def abbreviate_generated_fragment(root: Path, container: dict[str, Any], text: str, token_maps: list[dict[str, Any]],
                                  symbols: list[dict[str, Any]], edges: list[dict[str, Any]], tokenizer: Tokenizer
                                  ) -> tuple[str, dict[str, str], list[dict[str, Any]]]:
    locals_in_range = [s for s in symbols if s.get("kind") in ("VarDecl", "ParmDecl") and not s.get("public")
                       and s.get("file") == container.get("file")
                       and container.get("start", 0) <= s.get("name_start", -1) < container.get("end", 0)]
    opaque = {e.get("to") for e in edges if e.get("type") == "opaque_reference"}
    refs: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        if edge.get("type") in ("reference", "member_access"):
            refs.setdefault(edge.get("to", ""), []).append(edge)
    source_to_generated = {(int(m["source_start"]), int(m["source_end"])): (int(m["generated_start"]), int(m["generated_end"])) for m in token_maps}
    candidates = []
    existing_names = {s.get("name") for s in locals_in_range}
    used_short: set[str] = set()
    for symbol in sorted(locals_in_range, key=lambda s: (s.get("name", ""), s["id"])):
        if symbol["id"] in opaque:
            continue
        old = symbol.get("name", "")
        short = abbreviation(old)
        if not short or short == old or short in existing_names or short in used_short:
            continue
        source_ranges = [(int(symbol.get("name_start", symbol["start"])), int(symbol.get("name_end", symbol["start"] + len(old))))]
        source_ranges += [(int(e["start"]), int(e["end"])) for e in refs.get(symbol["id"], [])
                          if e.get("file") == container.get("file") and container.get("start", 0) <= e.get("start", -1) < container.get("end", 0)]
        source_ranges = sorted(set(source_ranges))
        generated_ranges = []
        for start, end in source_ranges:
            if token_maps:
                mapped = source_to_generated.get((start, end))
            else:
                mapped = (start - int(container["start"]), end - int(container["start"]))
            if not mapped or text[mapped[0]:mapped[1]] != old:
                generated_ranges = []
                break
            generated_ranges.append((mapped[0], mapped[1], start, end))
        if not generated_ranges:
            continue
        before = " ".join(old for _ in generated_ranges)
        after = " ".join(short for _ in generated_ranges) + f" {short}={old}"
        saving = tokenizer.count(before).count - tokenizer.count(after).count
        if saving > 0:
            candidates.append((symbol, short, sorted(set(generated_ranges)), saving))
            used_short.add(short)
    edits = []
    glossary = {}
    for symbol, short, ranges, saving in candidates:
        glossary[short] = symbol["name"]
        for start, end, source_start, source_end in ranges:
            edits.append((start, end, source_start, source_end, short, symbol, saving))
    if not edits:
        return text, {}, []
    pieces = []
    cursor = 0
    rename_maps = []
    for start, end, source_start, source_end, short, symbol, saving in sorted(edits, key=lambda x: (x[0], x[1], x[5]["id"])):
        if start < cursor:
            continue
        pieces.append(text[cursor:start])
        generated_start = sum(len(x) for x in pieces)
        pieces.append(short)
        rename_maps.append({"generated_start": generated_start, "generated_end": generated_start + len(short),
                            "canonical_file": symbol["file"], "canonical_start": source_start, "canonical_end": source_end,
                            "symbol_id": symbol["id"], "original": symbol["name"], "abbreviation": short,
                            "rule_id": "VIEW-ABBR-1", "token_saving_with_glossary": saving})
        cursor = end
    pieces.append(text[cursor:])
    updated = "".join(pieces)
    if token_maps:
        adjusted = []
        edit_ranges = [(e[0], e[1], len(e[4]) - (e[1] - e[0])) for e in edits]
        for mapping in token_maps:
            start, end = int(mapping["generated_start"]), int(mapping["generated_end"])
            if any(start == a and end == b for a, b, _ in edit_ranges):
                continue
            if any(not (end <= a or start >= b) for a, b, _ in edit_ranges):
                continue
            shift = sum(delta for a, b, delta in edit_ranges if b <= start)
            copy = dict(mapping)
            copy["generated_start"] = start + shift
            copy["generated_end"] = end + shift
            adjusted.append(copy)
        token_maps[:] = adjusted
    # Persist only stable semantic-ID assignments; the visible glossary remains slice-local.
    path = index_dir(root) / "abbreviations.json"
    lock_path = index_dir(root) / "cache/abbreviations.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            try:
                persisted = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                persisted = {}
            for symbol, short, _, _ in candidates:
                persisted[symbol["id"]] = {"original": symbol["name"], "abbreviation": short, "rule": "VIEW-ABBR-1"}
            atomic_write(path, (stable_json(persisted) + "\n").encode())
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return updated, glossary, rename_maps


def compile_record_for(root: Path, symbol: dict[str, Any]) -> dict[str, Any] | None:
    records = load_index(root)
    _, files, _, _ = partition_index(records)
    file_record = next((f for f in files if f["path"] == symbol.get("file")), None)
    if file_record and file_record.get("commands"):
        return file_record["commands"][0]
    # Headers may not own a compile command. Choose the deterministic first observed TU.
    for f in files:
        if f.get("commands"):
            return f["commands"][0]
    return None


def compact_range(root: Path, core: Path, symbol: dict[str, Any]) -> tuple[str, list[dict[str, Any]]] | None:
    command = compile_record_for(root, symbol)
    if not command:
        return None
    file = root / symbol["file"]
    args = ["compact", "--file", str(file), "--start", str(symbol["start"]), "--end", str(symbol["end"]), "--", *command.get("args", [])]
    proc = run_core(core, args, cwd=Path(command.get("directory", root)))
    if proc.returncode != 0:
        return None
    work_count("asts_constructed", 2)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return result["text"], result.get("maps", [])


@timed("view_persistence")
def persist_view(root: Path, text: str, source_map: dict[str, Any]) -> tuple[Path, Path]:
    digest = sha256_bytes((text + stable_json(source_map)).encode())[:16]
    view_dir = index_dir(root) / "views"
    view_path = view_dir / f"{digest}.ctx"
    map_path = view_dir / f"{digest}.map.json"
    banner = "//@generated:ctxpp readonly; edit canonical source via map\n"
    atomic_write(view_path, (banner + text).encode())
    atomic_write(map_path, (stable_json(source_map) + "\n").encode())
    return view_path, map_path


def _view_request_path(root: Path, target: dict[str, Any], intent: str, budget: int, layout: str,
                       tokenizer_config: str, core: Path | None) -> Path:
    try:
        manifest = json.loads((index_dir(root) / "manifest.json").read_text(encoding="utf-8"))
        index_hash = str(manifest.get("index_hash", ""))
    except (OSError, json.JSONDecodeError):
        index_hash = ""
    core_identity = sha256_file(core) if core and core.is_file() else "unavailable"
    key = sha256_bytes(stable_json({
        "schema": 1, "tool": VERSION, "index": index_hash, "target": target.get("id"),
        "intent": intent, "budget": budget, "layout": layout, "tokenizer": tokenizer_config,
        "core": core_identity,
    }).encode())
    return index_dir(root) / "cache/views" / f"{key}.json"


def load_cached_view(root: Path, target: dict[str, Any], intent: str, budget: int, layout: str,
                     tokenizer_config: str, core: Path | None) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    path = _view_request_path(root, target, intent, budget, layout, tokenizer_config, core)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("format") != "CTXPP-VIEW-CACHE/1":
            return None
        for rel, expected in cached.get("source_hashes", {}).items():
            source = root / rel
            if not source.is_file() or sha256_file(source) != expected:
                work_count("view_cache_misses")
                return None
        work_count("view_cache_hits")
        return str(cached["text"]), dict(cached["source_map"]), dict(cached["report"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        work_count("view_cache_misses")
        return None


def save_cached_view(root: Path, target: dict[str, Any], intent: str, budget: int, layout: str,
                     tokenizer_config: str, core: Path | None, text: str, source_map: dict[str, Any],
                     report: dict[str, Any]) -> None:
    source_hashes: dict[str, str] = {}
    for mapping in source_map.get("mappings", []):
        rel = str(mapping.get("canonical_file", ""))
        source = root / rel
        if rel and source.is_file():
            source_hashes[rel] = sha256_file(source)
    payload = {"format": "CTXPP-VIEW-CACHE/1", "source_hashes": dict(sorted(source_hashes.items())),
               "text": text, "source_map": source_map, "report": report}
    atomic_write(_view_request_path(root, target, intent, budget, layout, tokenizer_config, core),
                 (stable_json(payload) + "\n").encode())
    work_count("view_cache_writes")


def git_baseline(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        proc = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None
    return {"branch": run("branch", "--show-current"), "commit": run("rev-parse", "HEAD"), "status": (run("status", "--short") or "").splitlines()}


ABBREVIATIONS = {
    "candidate": "cand", "candidate_index": "ci", "block": "blk", "block_index": "bi", "feature": "ft",
    "column": "col", "count": "n", "score": "sc", "delta": "d", "packing_plan": "pp", "result": "out",
    "temporary": "tmp", "index": "i", "value": "v", "current": "cur", "previous": "prev",
}


def abbreviation(name: str) -> str:
    if name in ABBREVIATIONS:
        return ABBREVIATIONS[name]
    pieces = query_terms(name)
    if len(pieces) > 1:
        return "".join(x[0] for x in pieces)[:4]
    if "_" in name:
        return "".join(x[0] for x in name.split("_") if x)[:4]
    consonants = "".join(c for c in name if c.lower() not in "aeiou")
    return (consonants[:4] or name[:3]).lower()


def semantic_local_rename_plan(root: Path, cfg: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    records = load_index(root)
    meta, file_records, symbols, edges = partition_index(records)
    if not meta.get("semantic"):
        raise CtxppError("semantic-local-rename requires a Clang semantic index")
    tokenizer = Tokenizer(root, str(cfg.get("tokenizer", "auto")))
    allowed = {Path(p).as_posix() for p in paths}
    protected_symbols = set(cfg.get("protected", {}).get("symbols", []))
    patterns = cfg.get("protected", {}).get("name_patterns", [])
    minimum = int(cfg.get("identifiers", {}).get("minimum_net_token_saving", 4))
    candidates = []
    by_id = {s["id"]: s for s in symbols}
    file_by_path = {f["path"]: f for f in file_records}
    by_parent: dict[str, set[str]] = {}
    for s in symbols:
        by_parent.setdefault(s.get("parent_id", ""), set()).add(s.get("name", ""))
    references: dict[str, list[dict[str, Any]]] = {}
    opaque_references: set[str] = set()
    for edge in edges:
        if edge.get("type") in ("reference", "member_access"):
            references.setdefault(edge.get("to", ""), []).append(edge)
        elif edge.get("type") == "opaque_reference":
            opaque_references.add(edge.get("to", ""))
    for symbol in symbols:
        kind = symbol.get("kind")
        if kind not in ("VarDecl", "ParmDecl"):
            continue
        parent = by_id.get(symbol.get("parent_id", ""), {})
        if parent.get("kind") not in ("FunctionDecl", "CXXMethod", "Constructor", "FunctionTemplate", "LambdaExpr"):
            continue
        if kind == "ParmDecl" and not cfg.get("identifiers", {}).get("rename_parameters", False):
            continue
        if symbol.get("public") or symbol["id"] in protected_symbols or any(fnmatch.fnmatch(symbol.get("qualified_name", ""), p) for p in patterns):
            continue
        if any(fnmatch.fnmatch(symbol.get("file", ""), p) for p in cfg.get("protected", {}).get("paths", [])):
            continue
        if symbol["id"] in opaque_references:
            continue
        command_hashes = set(file_by_path.get(symbol.get("file", ""), {}).get("command_hashes", []))
        observed_hashes = {x.get("configuration_hash") for x in symbol.get("occurrences", []) if x.get("file") == symbol.get("file")}
        if len(command_hashes) > 1 and not command_hashes.issubset(observed_hashes):
            continue
        if allowed and symbol.get("file") not in allowed and not any(symbol.get("file", "").startswith(p.rstrip("/") + "/") for p in allowed):
            continue
        old = symbol.get("name", "")
        new = abbreviation(old)
        if not new or new == old or new in by_parent.get(symbol.get("parent_id", ""), set()):
            continue
        file_bytes = (root / symbol["file"]).read_bytes()
        old_bytes = old.encode()
        if any(marker in file_bytes for marker in (b'"' + old_bytes + b'"', b"'" + old_bytes + b"'")):
            continue
        ranges = [{"file": symbol["file"], "start": symbol.get("name_start", symbol["start"]), "end": symbol.get("name_end", symbol.get("name_start", symbol["start"]) + len(old))}]
        ranges += [{"file": e["file"], "start": e["start"], "end": e["end"]} for e in references.get(symbol["id"], [])]
        ranges = sorted({stable_json(r): r for r in ranges}.values(), key=lambda r: (r["file"], r["start"]))
        before = " ".join(old for _ in ranges)
        glossary = f"{new}={old}"
        after = " ".join(new for _ in ranges) + " " + glossary
        delta = tokenizer.count(before).count - tokenizer.count(after).count
        if delta >= minimum:
            candidates.append((delta, symbol, new, ranges, tokenizer.count(before)))
    candidates.sort(key=lambda x: (-x[0], x[1].get("qualified_name", ""), x[1]["id"]))
    edits = []
    affected: dict[str, bytes] = {}
    for delta, symbol, new, ranges, count in candidates:
        valid = True
        local_edits = []
        for r in ranges:
            path = root / r["file"]
            data = affected.setdefault(r["file"], path.read_bytes())
            if data[r["start"]:r["end"]].decode(errors="replace") != symbol["name"]:
                valid = False; break
            local_edits.append({"file": r["file"], "start": r["start"], "end": r["end"], "replacement": new,
                                "rule_id": "CTXPP-RENAME-LOCAL", "rule_version": 1, "symbol_id": symbol["id"],
                                "proof": "P1", "risk": "low", "token_delta": delta if not local_edits else 0,
                                "group_token_delta": delta, "tokens_before": count.count, "tokens_after": count.count - delta,
                                "required_verification": ["V1", "V2"]})
        if valid:
            edits.extend(local_edits)
    if not edits:
        raise CtxppError("no profitable semantic local rename met the configured full-context token threshold")
    return make_plan(root, cfg, "source-transform", edits, [], rule="semantic-local-rename")


def make_plan(root: Path, cfg: dict[str, Any], kind: str, edits: list[dict[str, Any]], creates: list[dict[str, Any]], *, rule: str) -> dict[str, Any]:
    touched = sorted({e["file"] for e in edits})
    files = {}
    for rel in touched:
        data = (root / rel).read_bytes()
        files[rel] = {"baseline_hash": sha256_bytes(data), "baseline_b64": base64.b64encode(data).decode()}
    for edit in edits:
        data = base64.b64decode(files[edit["file"]]["baseline_b64"])
        baseline_range = data[int(edit["start"]):int(edit["end"])]
        edit["baseline_range_b64"] = base64.b64encode(baseline_range).decode()
        edit["byte_delta"] = len(edit["replacement"].encode()) - len(baseline_range)
        edit["line_delta"] = edit["replacement"].count("\n") - baseline_range.count(b"\n")
    plan_seed = stable_json({"kind": kind, "rule": rule, "edits": edits, "creates": creates, "files": {k: v["baseline_hash"] for k, v in files.items()}})
    plan_id = sha256_bytes(plan_seed.encode())[:16]
    plan = {
        "format": "CTXPP-PLAN/1", "id": plan_id, "kind": kind, "rule": rule, "dry_run": True,
        "root": str(root), "profile": cfg.get("profile"), "source_write_required": True,
        "baseline": git_baseline(root), "files": files, "edits": sorted(edits, key=lambda e: (e["file"], e["start"], e["end"])),
        "creates": sorted(creates, key=lambda e: e["file"]), "verification": cfg.get("verification", {}),
        "projected_token_delta": sum(int(e.get("token_delta", 0)) for e in edits),
        "projected_file_open_context_delta": sum(int(e.get("file_open_context_delta", 0)) for e in edits),
        "proof_levels": sorted({e.get("proof", "P1") for e in edits}),
        "risk": sorted({e.get("risk", "low") for e in edits}),
    }
    path = index_dir(root) / "plans" / f"{plan_id}.json"
    atomic_write(path, (stable_json(plan) + "\n").encode())
    plan["path"] = str(path)
    return plan


def shard_plan(root: Path, cfg: dict[str, Any], host: str) -> dict[str, Any]:
    rel = Path(host).as_posix()
    path = root / rel
    if not path.is_file() or path.suffix not in (".cc", ".cpp", ".cxx", ".cu"):
        raise CtxppError("shard requires one canonical .cc/.cpp/.cxx/.cu file")
    if is_excluded(rel, cfg) or rel.startswith(".ctxpp/"):
        raise CtxppError("refusing excluded or generated source")
    if any(fnmatch.fnmatch(rel, p) for p in cfg.get("protected", {}).get("paths", [])):
        raise CtxppError("refusing protected source path")
    records = load_index(root)
    meta, _, symbols, _ = partition_index(records)
    if not meta.get("semantic"):
        raise CtxppError("sharding requires a Clang semantic index")
    definitions = [s for s in symbols if s.get("file") == rel and s.get("definition") and s.get("kind") in ("FunctionDecl", "CXXMethod", "FunctionTemplate")]
    definitions.sort(key=lambda s: s.get("start", 0))
    if len(definitions) < 2:
        raise CtxppError("no profitable safe shard: need at least two complete top-level definitions")
    target_tokens = int(cfg.get("budgets", {}).get("fragment_target_tokens", 1200))
    minimum = int(cfg.get("budgets", {}).get("fragment_min_tokens", 200))
    chosen = []; total = 0
    for symbol in definitions:
        if total >= target_tokens and chosen:
            break
        text = source_text(root, symbol)
        if any(line.lstrip().startswith("#") for line in text.splitlines()):
            continue
        chosen.append(symbol); total += int(symbol.get("tokens", max(1, len(text) // 4)))
    if not chosen or total < minimum:
        raise CtxppError(f"no profitable safe shard: candidate has {total} tokens below minimum {minimum}")
    start, end = chosen[0]["start"], chosen[-1]["end"]
    data = path.read_bytes()
    line_start = data.rfind(b"\n", 0, start) + 1
    previous_end = max(0, line_start - 1)
    previous_start = data.rfind(b"\n", 0, previous_end) + 1
    if data[previous_start:previous_end].lstrip().startswith(b"//@"):
        start = previous_start
    fragment_data = data[start:end]
    fragment_dir = path.parent.relative_to(root)
    suffix = ".inc.cuh" if path.suffix == ".cu" else ".inc"
    short_role = abbreviation(chosen[0]["name"])
    fragment_rel = (fragment_dir / (short_role + suffix)).as_posix()
    if (root / fragment_rel).exists():
        fragment_rel = (fragment_dir / (short_role + "_" + sha256_bytes(chosen[0]["id"].encode())[:4] + suffix)).as_posix()
    include_rel = Path(fragment_rel).relative_to(path.parent.relative_to(root)).as_posix()
    replacement = f'#include "{include_rel}"\n'
    tokenizer = Tokenizer(root, str(cfg.get("tokenizer", "auto")))
    fragment_tokens = tokenizer.count(fragment_data.decode(errors="replace")).count
    before_annotation = f"@target {rel}:{chosen[0].get('line', 0)} {chosen[0].get('qualified_name', chosen[0]['name'])} v"
    after_annotation = f"@target {fragment_rel}:2 {chosen[0].get('qualified_name', chosen[0]['name'])} v"
    representative_delta = tokenizer.count(before_annotation).count - tokenizer.count(after_annotation).count
    if representative_delta < 0:
        raise CtxppError(f"refusing shard: representative emitted slice would regress by {-representative_delta} tokens")
    edit = {"file": rel, "start": start, "end": end, "replacement": replacement,
            "rule_id": "CTXPP-SHARD-SAME-TU", "rule_version": 1, "proof": "P3", "risk": "medium",
            "token_delta": representative_delta, "representative_slice_token_delta": representative_delta,
            "required_verification": ["V1", "V2", "V3"],
            "symbols": [s["id"] for s in chosen]}
    route_rel = (fragment_dir / (path.stem + ".INDEX.ctx")).as_posix()
    projected_host = _apply_edits(data, [edit])
    route_body = f"{Path(fragment_rel).name}:implementation|symbols:{','.join(s['name'] for s in chosen)}|tokens:{total}\n"
    route = "CTXPP-ROUTE/1\n" + f"host:{rel}|hash:{sha256_bytes(projected_host)}\n" + route_body
    file_tokens = tokenizer.count(data.decode(errors="replace")).count
    route_tokens = tokenizer.count(route).count
    context_after = fragment_tokens + route_tokens + 10
    context_delta = file_tokens - context_after
    if context_delta <= 0:
        raise CtxppError(f"refusing shard: representative context would not improve ({file_tokens} -> {context_after} tokens)")
    edit["file_open_context_delta"] = context_delta
    edit["file_open_context_before_tokens"] = file_tokens
    edit["file_open_context_after_tokens"] = context_after
    creates = [{"file": fragment_rel, "content_b64": base64.b64encode(fragment_data).decode(), "rule_id": "CTXPP-SHARD-SAME-TU"},
               {"file": route_rel, "content_b64": base64.b64encode(route.encode()).decode(), "rule_id": "CTXPP-SHARD-ROUTE"}]
    return make_plan(root, cfg, "shard", [edit], creates, rule="same-tu-contiguous-v1")


def _apply_edits(data: bytes, edits: list[dict[str, Any]]) -> bytes:
    result = data
    for edit in sorted(edits, key=lambda e: e["start"], reverse=True):
        start, end = int(edit["start"]), int(edit["end"])
        if not 0 <= start <= end <= len(result):
            raise CtxppError("plan contains an invalid byte range")
        replacement = base64.b64decode(edit["replacement_b64"]) if edit.get("replacement_b64") else edit["replacement"].encode()
        result = result[:start] + replacement + result[end:]
    return result


def verification_commands(plan: dict[str, Any]) -> list[tuple[str, str]]:
    result = []
    for tier in ("build", "targeted_tests", "full_tests", "abi", "performance"):
        for command in plan.get("verification", {}).get(tier, []):
            result.append((tier, str(command)))
    return result


def apply_plan(root: Path, cfg: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    if not cfg.get("source_write"):
        raise CtxppError("source_write=false; explicit repository opt-in is required")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("format") != "CTXPP-PLAN/1" or Path(plan.get("root", "")).resolve() != root:
        raise CtxppError("plan format or repository root mismatch")
    planned_paths = [*plan.get("files", {}).keys(), *(x.get("file", "") for x in plan.get("creates", []))]
    if any(p.startswith(".ctxpp/") or is_excluded(p, cfg) for p in planned_paths):
        raise CtxppError("refusing a plan that targets generated, excluded, or dependency content")
    for rel, info in plan.get("files", {}).items():
        path = root / rel
        if path.is_file() and path.stat().st_mode & 0o222 == 0:
            raise CtxppError(f"read-only source: {rel}")
        if not path.is_file() or sha256_file(path) != info["baseline_hash"]:
            raise CtxppError(f"stale plan: {rel} changed after planning")
    for create in plan.get("creates", []):
        if (root / create["file"]).exists():
            raise CtxppError(f"stale plan: create target already exists: {create['file']}")
    baseline = {rel: (root / rel).read_bytes() for rel in plan.get("files", {})}
    created: list[Path] = []
    removed: dict[Path, bytes] = {}
    verification = []
    try:
        for rel, data in baseline.items():
            edits = [e for e in plan.get("edits", []) if e["file"] == rel]
            atomic_write(root / rel, _apply_edits(data, edits))
        for create in plan.get("creates", []):
            path = root / create["file"]
            atomic_write(path, base64.b64decode(create["content_b64"]))
            created.append(path)
        for rel in plan.get("remove", []):
            path = root / rel
            if path.is_file():
                removed[path] = path.read_bytes()
                path.unlink()
        semantic = scan_repository(root, cfg, Path(__file__).resolve().parent.parent)
        verification.append({"tier": "V1", "command": "ctxpp scan", "returncode": 0 if semantic["failures"] == 0 else 1,
                             "stdout": stable_json(semantic), "stderr": ""})
        if semantic["failures"]:
            raise CtxppError("semantic reparse failed after applying plan")
        for tier, command in verification_commands(plan):
            proc = run_captured(root, str(command), root, atomic_write)
            verification.append({"tier": tier, "command": command, "returncode": proc.returncode,
                                 "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]})
            if proc.returncode != 0:
                raise CtxppError(f"verification failed ({tier}): {command}")
    except Exception as exc:
        for rel, data in baseline.items():
            atomic_write(root / rel, data)
        for path in reversed(created):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for path, data in removed.items():
            atomic_write(path, data)
        try:
            scan_repository(root, cfg, Path(__file__).resolve().parent.parent)
        except Exception:
            pass
        report = {"format": "CTXPP-APPLY/1", "plan": plan.get("id"), "success": False, "rolled_back": True,
                  "error": str(exc), "verification": verification}
        report_path = index_dir(root) / "reports" / f"{plan.get('id')}.failed.json"
        atomic_write(report_path, (stable_json(report) + "\n").encode())
        return {**report, "report": str(report_path)}
    reverse_files = {}
    reverse_edits = []
    for rel, old in baseline.items():
        current = (root / rel).read_bytes()
        reverse_files[rel] = {"baseline_hash": sha256_bytes(current), "baseline_b64": base64.b64encode(current).decode()}
        reverse_edits.append({"file": rel, "start": 0, "end": len(current), "replacement": "",
                              "replacement_b64": base64.b64encode(old).decode(),
                              "rule_id": "CTXPP-REVERSE", "rule_version": 1, "proof": "P1", "risk": "low", "token_delta": 0})
    reverse_plan = {"format": "CTXPP-PLAN/1", "id": plan["id"] + "-reverse", "kind": "reverse", "rule": "byte-exact-reverse",
                    "dry_run": False, "root": str(root), "files": reverse_files, "edits": reverse_edits,
                    "creates": [], "remove": [str(p.relative_to(root)) for p in created], "verification": plan.get("verification", {})}
    reverse_path = index_dir(root) / "plans" / f"{plan['id']}.reverse.json"
    atomic_write(reverse_path, (stable_json(reverse_plan) + "\n").encode())
    plan["dry_run"] = False
    atomic_write(plan_path, (stable_json(plan) + "\n").encode())
    report = {"format": "CTXPP-APPLY/1", "plan": plan["id"], "success": True, "rolled_back": False,
              "reverse_plan": str(reverse_path), "verification": verification,
              "changed": sorted(baseline), "created": [str(p.relative_to(root)) for p in created]}
    report_path = index_dir(root) / "reports" / f"{plan['id']}.json"
    atomic_write(report_path, (stable_json(report) + "\n").encode())
    return {**report, "report": str(report_path)}


@timed("verification")
def verify_commands(root: Path, cfg: dict[str, Any], tiers: Iterable[str] = ()) -> list[dict[str, Any]]:
    selected = set(tiers)
    results = []
    for tier in ("build", "targeted_tests", "full_tests", "abi", "performance"):
        if selected and tier not in selected:
            continue
        for command in cfg.get("verification", {}).get(tier, []):
            proc = run_captured(root, str(command), root, atomic_write)
            results.append({"tier": tier, "command": command, "returncode": proc.returncode,
                            "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]})
    return results


def lint_contract(text: str, line: int) -> list[str]:
    marker = "//@"
    if marker not in text:
        return []
    payload = text.split(marker, 1)[1].strip()
    fields = []
    problems = []
    for part in payload.split("|"):
        if ":" not in part:
            problems.append(f"line {line}: contract field lacks ':'")
            continue
        field = part.split(":", 1)[0].strip()
        if field not in CONTRACT_FIELDS:
            problems.append(f"line {line}: unknown contract field {field!r}")
        if field in fields:
            problems.append(f"line {line}: duplicate contract field {field!r}")
        fields.append(field)
    order = [CONTRACT_FIELDS.index(f) for f in fields if f in CONTRACT_FIELDS]
    if order != sorted(order):
        problems.append(f"line {line}: contract fields are not in canonical order")
    return problems


def lint_repository(root: Path, cfg: dict[str, Any], paths: Iterable[str] = ()) -> dict[str, Any]:
    tokenizer = Tokenizer(root, str(cfg.get("tokenizer", "auto")))
    problems = []
    hard_max = int(cfg.get("budgets", {}).get("fragment_hard_max_tokens", 3200))
    for path in source_paths(root, cfg, paths):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        count = tokenizer.count(text)
        if count.count > hard_max:
            problems.append({"path": rel, "kind": "oversized-file", "tokens": count.count, "limit": hard_max})
        for number, line in enumerate(text.splitlines(), 1):
            for message in lint_contract(line, number):
                problems.append({"path": rel, "kind": "contract", "message": message})
            if "using namespace " in line and not line.lstrip().startswith("//"):
                problems.append({"path": rel, "kind": "forbidden-dense-construct", "message": f"line {number}: using namespace"})
    status = index_status(root, cfg)
    if status.get("stale"):
        problems.append({"path": ".ctxpp/index.jsonl", "kind": "stale-index", "reasons": status.get("reasons")})
    for route in sorted(root.rglob("INDEX.ctx")):
        try:
            lines = route.read_text(encoding="utf-8").splitlines()
            host_line = next((x for x in lines if x.startswith("host:") and "|hash:" in x), None)
            if host_line:
                host, expected = host_line[5:].split("|hash:", 1)
                if not (root / host).is_file() or sha256_file(root / host) != expected:
                    problems.append({"path": route.relative_to(root).as_posix(), "kind": "stale-route", "host": host})
        except OSError as exc:
            problems.append({"path": str(route), "kind": "route-read", "message": str(exc)})
    abbr_path = index_dir(root) / "abbreviations.json"
    if abbr_path.is_file():
        try:
            values = json.loads(abbr_path.read_text(encoding="utf-8")).values()
            meanings: dict[str, set[str]] = {}
            for value in values:
                meanings.setdefault(value.get("abbreviation", ""), set()).add(value.get("original", ""))
            for short, originals in meanings.items():
                if short and len(originals) > 1:
                    problems.append({"path": ".ctxpp/abbreviations.json", "kind": "abbreviation-collision", "abbreviation": short, "originals": sorted(originals)})
        except (OSError, json.JSONDecodeError) as exc:
            problems.append({"path": ".ctxpp/abbreviations.json", "kind": "abbreviation-invalid", "message": str(exc)})
    return {"format": "CTXPP-LINT/1", "problems": problems, "ok": not problems}


def audit_repository(root: Path, cfg: dict[str, Any], paths: Iterable[str] = ()) -> dict[str, Any]:
    records = load_index(root)
    meta, files, symbols, edges = partition_index(records)
    scoped = {Path(x).as_posix() for x in paths}
    if scoped:
        files = [f for f in files if f["path"] in scoped or any(f["path"].startswith(p.rstrip("/") + "/") for p in scoped)]
        allowed = {f["path"] for f in files}
        symbols = [s for s in symbols if s.get("file") in allowed]
    largest_files = sorted(files, key=lambda f: (-f.get("tokens", 0), f["path"]))[:10]
    largest_symbols = sorted(symbols, key=lambda s: (-s.get("tokens", 0), s.get("qualified_name", "")))[:10]
    repeats = sorted(((s.get("name", ""), sum(1 for x in symbols if x.get("name") == s.get("name"))) for s in symbols), key=lambda x: (-x[1], x[0]))
    return {"format": "CTXPP-AUDIT/1", "backend": meta.get("backend"), "files": len(files), "symbols": len(symbols), "edges": len(edges),
            "tokens": sum(f.get("tokens", 0) for f in files),
            "largest_files": [{"path": f["path"], "tokens": f.get("tokens"), "lines": f.get("lines")} for f in largest_files],
            "largest_symbols": [{"name": s.get("qualified_name"), "file": s.get("file"), "tokens": s.get("tokens")} for s in largest_symbols],
            "repeated_names": [{"name": n, "count": c} for n, c in dict(repeats).items() if c > 1][:10],
            "opaque_or_conflicting": [f["path"] for f in files if f.get("parse_status") != "ok"]}


def explain_generated(root: Path, query: str) -> dict[str, Any]:
    candidate = Path(query)
    plan_path = candidate if candidate.is_absolute() else root / candidate
    if not plan_path.is_file():
        plan_path = index_dir(root) / "plans" / (query if query.endswith(".json") else query + ".json")
    if plan_path.is_file():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if plan.get("format") == "CTXPP-PLAN/1":
            return {"plan": plan.get("id"), "rule": plan.get("rule"), "projected_token_delta": plan.get("projected_token_delta"),
                    "decisions": [{k: e.get(k) for k in ("rule_id", "rule_version", "symbol_id", "proof", "risk", "token_delta", "required_verification")}
                                  for e in plan.get("edits", [])]}
    abbr_path = index_dir(root) / "abbreviations.json"
    if abbr_path.is_file():
        abbreviations = json.loads(abbr_path.read_text(encoding="utf-8"))
        matches = [{"symbol_id": sid, **value} for sid, value in abbreviations.items()
                   if query in (sid, value.get("original"), value.get("abbreviation"))]
        if matches:
            return {"abbreviations": matches}
    if query.startswith("omissions:"):
        target = resolve_symbols(root, query.split(":", 1)[1], 1)
        if not target:
            raise CtxppError("unknown omitted-target ID")
        return {"target": target[0]["id"], "hint": "rerun slice with a larger --budget or --depth"}
    if ":" in query and query.endswith(tuple(str(i) for i in range(10))):
        path_text, _, line_text = query.rpartition(":")
        map_path = Path(path_text)
        if map_path.suffix == ".ctx":
            map_path = map_path.with_suffix(".map.json")
        if not map_path.is_absolute():
            map_path = root / map_path
        if map_path.is_file() and line_text.isdigit():
            line = int(line_text)
            data = json.loads(map_path.read_text(encoding="utf-8"))
            matches = [m for m in data.get("mappings", []) if m["generated_line_start"] <= line <= m["generated_line_end"]]
            return {"generated": query, "canonical": matches}
    symbols = resolve_symbols(root, query, 8)
    return {"query": query, "symbols": [{k: s.get(k) for k in ("id", "name", "qualified_name", "file", "line", "kind")} for s in symbols]}
