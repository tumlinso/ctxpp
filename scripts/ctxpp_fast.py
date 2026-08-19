from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path
from typing import Any

from ctxpp_rank import rank as _rank, terms as _rank_terms


def _bounded_failures(root: Path, failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(failures) <= 12 and all("category" in failure for failure in failures):
        return failures
    encoded = "\n".join(json.dumps(item, sort_keys=True, separators=(",", ":")) for item in failures)
    digest = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    log = root / ".ctxpp/logs" / f"status-{digest}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    if not log.is_file():
        temporary = log.with_suffix(".tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        os.replace(temporary, log)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for failure in failures:
        message = str(failure.get("error", "")).splitlines()[0][:512]
        lower = message.lower()
        category = "command_translation" if any(word in lower for word in ("unknown argument", "unsupported", "unrecognized")) else "source_parse"
        key = (category, message)
        group = groups.setdefault(key, {"file": failure.get("file"), "configuration": failure.get("configuration"),
                                        "error": message, "category": category, "count": 0,
                                        "affected_file_count": 0, "details_log": log.relative_to(root).as_posix(), "_files": set()})
        group["count"] += 1
        if failure.get("file"): group["_files"].add(str(failure["file"]))
    result = []
    for group in sorted(groups.values(), key=lambda value: (value["category"], value["error"]))[:12]:
        group["affected_file_count"] = len(group.pop("_files")); result.append(group)
    return result


def _git_dirty(root: Path) -> set[str] | None:
    import subprocess
    proc = subprocess.run(["git", "-c", "status.relativePaths=true", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                          cwd=root, capture_output=True, check=False)
    if proc.returncode != 0:
        return None
    parts = proc.stdout.split(b"\0")
    result: set[str] = set()
    index = 0
    while index < len(parts):
        entry = parts[index]
        if not entry:
            index += 1; continue
        status = entry[:2]
        result.add(entry[3:].decode("utf-8", errors="surrogateescape"))
        if b"R" in status or b"C" in status:
            index += 1
            if index < len(parts) and parts[index]:
                result.add(parts[index].decode("utf-8", errors="surrogateescape"))
        index += 1
    return result


def _root(start: Path) -> Path:
    current = start.resolve()
    for path in (current, *current.parents):
        if (path / ".ctxpp.toml").is_file():
            return path
    return current


def _terms(query: str) -> list[str]:
    return _rank_terms(query)


def _profile(counters: dict[str, int]) -> None:
    import tempfile
    destination = os.environ.get("CTXPP_PROFILE_PATH")
    if not destination:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"format": "CTXPP-PROFILE/1", "counters": dict(sorted(counters.items())), "timings_ns": {}},
                                sort_keys=True, separators=(",", ":")) + "\n")
    os.replace(temporary, path)


def _parse(argv: list[str]) -> tuple[Path, str, str, int] | None:
    root = None
    position = 0
    while position < len(argv) and argv[position].startswith("-"):
        if argv[position] == "--root" and position + 1 < len(argv):
            root = Path(argv[position + 1]); position += 2
        elif argv[position] == "--json":
            position += 1
        else:
            return None
    if position >= len(argv) or argv[position] not in ("status", "where", "route"):
        return None
    command = argv[position]
    if command == "status":
        if position + 1 != len(argv):
            return None
        return _root(root or Path.cwd()), command, "", 8
    if position + 1 >= len(argv):
        return None
    query = argv[position + 1]
    position += 2
    limit = 8
    if position < len(argv):
        if len(argv) != position + 2 or argv[position] != "--limit":
            return None
        try:
            limit = int(argv[position + 1])
        except ValueError:
            return None
        if limit < 0:
            return None
    return _root(root or Path.cwd()), command, query, limit


def _freshness(root: Path, counters: dict[str, int], *, check_config: bool = False) -> tuple[set[str], set[str]] | None:
    import hashlib
    try:
        payload = json.loads((root / ".ctxpp/cache/freshness.json").read_text(encoding="utf-8"))
        freshness = payload["files"]
        config = root / ".ctxpp.toml"
        if check_config and ((hashlib.sha256(config.read_bytes()).hexdigest() if config.is_file() else None) != payload.get("config_file_hash")):
            return None
        dirty = set()
        git_dirty = _git_dirty(root)
        for rel, prior in freshness.items():
            source = root / rel
            try:
                stat = source.stat()
            except OSError:
                dirty.add(rel)
                continue
            counters["files_statted"] = counters.get("files_statted", 0) + 1
            metadata_same = stat.st_size == prior.get("size") and stat.st_mtime_ns == prior.get("mtime_ns")
            trusted_clean = metadata_same and git_dirty is not None and rel not in git_dirty
            if not trusted_clean:
                if hashlib.sha256(source.read_bytes()).hexdigest() != prior.get("hash"):
                    dirty.add(rel)
        if git_dirty is not None:
            source_suffixes = {".h", ".hh", ".hpp", ".hxx", ".inc", ".ipp", ".cc", ".cpp", ".cxx", ".cu", ".cuh"}
            dirty.update(rel for rel in git_dirty if rel not in freshness and Path(rel).suffix in source_suffixes)
        readiness = json.loads((root / ".ctxpp/cache/readiness.json").read_text(encoding="utf-8"))
        return dirty, set(readiness.get("semantic_stale_tus", []))
    except (OSError, KeyError, json.JSONDecodeError):
        return None


def _identifiers(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    result = set()
    word = []
    i = 0
    while i < len(text):
        if text.startswith("//", i):
            end = text.find("\n", i + 2); i = len(text) if end < 0 else end + 1; continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2); i = len(text) if end < 0 else end + 2; continue
        if text[i] in ('"', "'"):
            quote = text[i]; i += 1
            while i < len(text):
                if text[i] == "\\": i += 2
                elif text[i] == quote: i += 1; break
                else: i += 1
            continue
        if text[i] == "_" or text[i].isalpha():
            start = i; i += 1
            while i < len(text) and (text[i] == "_" or text[i].isalnum()): i += 1
            identifier = text[start:i]
            result.add(identifier.lower()); result.update(_terms(identifier)); continue
        i += 1
    return result


def _query_safe(root: Path, connection: sqlite3.Connection, counters: dict[str, int], query: str,
                matches: list[dict[str, Any]]) -> bool:
    if query.isidentifier() and not any(query in (symbol.get("name"), symbol.get("qualified_name"), symbol.get("id")) for symbol in matches):
        return False
    degraded_files = {str(symbol.get("file", "")) for symbol in matches if symbol.get("degraded")}
    if degraded_files:
        for name in ("observed-commands.json", "parse-recipes.json"):
            try: records = json.loads((root / ".ctxpp/cache" / name).read_text(encoding="utf-8")).get("records", [])
            except (OSError, json.JSONDecodeError, TypeError): continue
            for record in records:
                source = Path(str(record.get("file", "")))
                source = source if source.is_absolute() else Path(record.get("directory", root)) / source
                try: rel = source.resolve().relative_to(root).as_posix()
                except (OSError, ValueError): continue
                if rel in degraded_files:
                    return False
    state = _freshness(root, counters)
    if state is None:
        return False
    dirty, stale_tus = state
    selected_files = {str(symbol.get("file", "")) for symbol in matches}
    if dirty & selected_files:
        return False
    selected_tus = set()
    for rel in selected_files:
        row = connection.execute("SELECT data FROM files WHERE path=?", (rel,)).fetchone()
        if row:
            selected_tus.update(str(tu) for tu in json.loads(row[0]).get("translation_units", []))
    if selected_tus & stale_tus:
        return False
    try:
        tu_state = json.loads((root / ".ctxpp/cache/tu-state.json").read_text(encoding="utf-8")).get("jobs", {})
    except (OSError, json.JSONDecodeError):
        tu_state = {}
    for key, record in tu_state.items():
        tu = key.split("\0", 1)[0]
        if tu in selected_tus and any(rel in dirty for rel in record.get("dependencies", {})):
            return False
    terms = [term for term in _terms(query) if len(term) > 1]
    return not any(terms and all(term in _identifiers(root / rel) for term in terms) for rel in dirty)


def _decode(rows) -> list[dict[str, Any]]:
    return [json.loads(row[0]) for row in rows]


def _overlay(root: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads((root / ".ctxpp/cache/lexical-overlay.json").read_text(encoding="utf-8"))
        return list(payload.get("symbols", [])) if payload.get("format") == "CTXPP-LEXICAL-OVERLAY/1" else []
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _resolve(connection: sqlite3.Connection, target: str, limit: int) -> list[dict[str, Any]]:
    if target.startswith(("usr:", "c:")):
        exact = _decode(connection.execute("SELECT data FROM symbols WHERE id=? LIMIT ?", (target, limit)).fetchall())
        if exact:
            return exact
    if ":" in target:
        rel, _, raw_line = target.rpartition(":")
        if raw_line.isdigit():
            return _decode(connection.execute(
                "SELECT data FROM symbols WHERE file=? AND line<=? AND end_line>=? ORDER BY (end_byte-start_byte),qualified_name LIMIT ?",
                (rel, int(raw_line), int(raw_line), limit)).fetchall())
    exact = _decode(connection.execute(
        "SELECT data FROM symbols WHERE id=? OR qualified_name=? OR name=?", (target, target, target)).fetchall())
    if exact:
        return sorted(exact, key=lambda symbol: (symbol.get("qualified_name") != target, not symbol.get("definition"), symbol.get("file", "")))[:limit]
    terms = _terms(target)
    if not terms:
        return []
    clauses = " OR ".join("instr(search_text,?)>0" for _ in terms)
    symbols = _decode(connection.execute(f"SELECT data FROM symbols WHERE {clauses}", terms).fetchall())
    return _rank(target, symbols, limit)


def _route(connection: sqlite3.Connection, query: str, limit: int) -> list[dict[str, Any]]:
    matches = _resolve(connection, query, limit * 3)
    base_ids = {symbol.get("id") for symbol in matches}
    if base_ids:
        marks = ",".join("?" for _ in base_ids)
        edges = _decode(connection.execute(
            f"SELECT data FROM edges WHERE type='test_relationship' AND to_id IN ({marks})", sorted(base_ids)).fetchall())
        source_ids = sorted({edge.get("from") for edge in edges if edge.get("from")})
        if source_ids:
            source_marks = ",".join("?" for _ in source_ids)
            for symbol in _decode(connection.execute(f"SELECT data FROM symbols WHERE id IN ({source_marks})", source_ids).fetchall()):
                matches.append({**symbol, "_route_test_bonus": 20})
    matches = list({symbol["id"]: symbol for symbol in matches}.values())
    return _rank(query, matches, limit)


def try_fast(argv: list[str]) -> int | None:
    parsed = _parse(argv)
    if not parsed:
        return None
    import sqlite3
    root, command, query, limit = parsed
    database = root / ".ctxpp/cache/query.sqlite"
    manifest_path = root / ".ctxpp/manifest.json"
    try:
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))["index_hash"]
        connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
        actual = connection.execute("SELECT value FROM metadata WHERE key='index_hash'").fetchone()
        meta_row = connection.execute("SELECT value FROM metadata WHERE key='index_meta'").fetchone()
        counters: dict[str, int] = {}
        if not actual or actual[0] != expected or not meta_row:
            connection.close()
            return None
        meta = json.loads(meta_row[0])
        if command == "status":
            state = _freshness(root, counters, check_config=True)
            if state is None or state[0] or state[1]:
                connection.close()
                return None
            file_count, symbol_count, edge_count = (int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                                                    for table in ("files", "symbols", "edges"))
            matches = []
        else:
            matches = _resolve(connection, query, limit) if command == "where" else _route(connection, query, limit)
            overlay = _overlay(root)
            if command == "where":
                exact_overlay = [symbol for symbol in overlay if query in (symbol.get("id"), symbol.get("name"), symbol.get("qualified_name"))]
                if exact_overlay:
                    matches = exact_overlay[:limit]
            elif overlay:
                matches = _rank(query, [*matches, *overlay], limit)
            if not _query_safe(root, connection, counters, query, matches):
                connection.close()
                return None
        connection.close()
    except (OSError, KeyError, json.JSONDecodeError, sqlite3.Error):
        return None
    if command == "status":
        source_write = bool(meta.get("_source_write"))
        output = {"format": "CTXPP-STATUS/1", "root": str(root), "configured": (root / ".ctxpp.toml").is_file(),
                  "present": True, "stale": False, "reasons": [], "backend": meta.get("backend"),
                  "semantic": bool(meta.get("semantic")), "incomplete": bool(meta.get("incomplete")),
                  "failures": _bounded_failures(root, list(meta.get("failures", []))), "files": file_count, "symbols": symbol_count, "edges": edge_count,
                  "profile": meta.get("_profile"), "source_write_configured": source_write,
                  "source_write_safe": source_write and bool(meta.get("semantic")) and not meta.get("failures")}
    else:
        output = {"format": f"CTXPP-{command.upper()}/1", "incomplete": not bool(meta.get("semantic")),
                  "matches": [{key: symbol.get(key) for key in ("id", "qualified_name", "kind", "file", "line", "definition", "degraded")}
                              for symbol in matches]}
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    _profile(counters)
    return 0
