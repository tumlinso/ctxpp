from __future__ import annotations

import hashlib
import json
import re
import shlex
import sys
import subprocess
from pathlib import Path
import sqlite3
import tomllib
from ctxpp_paths import state_dir
from ctxpp_runtime import open_query_store
from typing import Any

from ctxpp_lib import (CtxppError, Tokenizer, load_index, partition_index,
                       _cpp_tokens, bounded_graph, recover_target_representation, sha256_bytes, sha256_file,
                       source_text, stable_json)


CONTEXT_KEYS = ("types", "dependencies", "callers", "callees", "tests")
DATA_SHAPE_KINDS = frozenset({
    "StructDecl", "ClassDecl", "UnionDecl", "EnumDecl", "ClassTemplate",
    "RecordDecl", "CXXRecordDecl",
})


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)


def source_identity(root: Path, relevant_paths: list[str]) -> dict[str, Any]:
    head_result = _git(root, "rev-parse", "HEAD")
    head = head_result.stdout.decode().strip() if head_result.returncode == 0 else None
    status_result = _git(root, "-c", "status.relativePaths=true", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *sorted(set(relevant_paths)))
    dirty_paths: list[str] = []
    if status_result.returncode == 0:
        entries = status_result.stdout.split(b"\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            if not entry:
                index += 1
                continue
            status = entry[:2]
            dirty_paths.append(entry[3:].decode("utf-8", errors="surrogateescape"))
            if b"R" in status or b"C" in status:
                index += 1
                if index < len(entries) and entries[index]:
                    dirty_paths.append(entries[index].decode("utf-8", errors="surrogateescape"))
            index += 1
    file_hashes = {
        rel: sha256_file(root / rel)
        for rel in sorted(set(relevant_paths))
        if (root / rel).is_file()
    }
    fingerprint = sha256_bytes(stable_json({
        "git_head": head,
        "dirty_paths": sorted(set(dirty_paths)),
        "relevant_files": file_hashes,
    }).encode())
    return {
        "schema_version": 1,
        "repo_root": str(root),
        "git_head": head,
        "dirty_paths": sorted(set(dirty_paths)),
        "fingerprint": fingerprint,
    }


INTENT_ALIASES = {
    "investigate": "understand",
    "debug": "debug",
    "edit": "edit",
    "test": "test",
    "review": "understand",
    "performance": "performance",
}

INTENT_ORDER = {
    "investigate": ("callers", "tests", "dependencies", "callees", "types"),
    "debug": ("callers", "callees", "dependencies", "tests", "types"),
    "edit": ("types", "dependencies", "callers", "tests", "callees"),
    "test": ("tests", "callers", "dependencies", "types", "callees"),
    "review": ("callers", "dependencies", "tests", "callees", "types"),
    "performance": ("callees", "callers", "types", "dependencies", "tests"),
}


def _line_for_offset(path: Path, offset: int) -> int:
    try:
        return path.read_bytes()[:max(offset, 0)].count(b"\n") + 1
    except OSError:
        return 0


def _location(root: Path, symbol: dict[str, Any]) -> dict[str, Any] | None:
    rel = str(symbol.get("file", ""))
    path = root / rel
    if not rel or not path.is_file():
        return None
    start = int(symbol.get("start", 0))
    end = int(symbol.get("end", start))
    return {
        "path": rel,
        "line": int(symbol.get("line") or _line_for_offset(path, start)),
        "end_line": int(symbol.get("end_line") or _line_for_offset(path, end)),
        "byte_start": start,
        "byte_end": end,
        "content_sha256": sha256_file(path),
    }


def _contract_items(root: Path, target: dict[str, Any]) -> list[dict[str, Any]]:
    rel = str(target.get("file", ""))
    path = root / rel
    if not path.is_file():
        return []
    start = int(target.get("start", 0))
    prefix = path.read_bytes()[:start].decode("utf-8", errors="replace").splitlines()
    comment_lines: list[tuple[int, str]] = []
    found = False
    for number, line in reversed(list(enumerate(prefix, 1))[-8:]):
        stripped = line.strip()
        if stripped.startswith("//@"):
            comment_lines.append((number, stripped[3:]))
            found = True
        elif found or stripped:
            break
    result: list[dict[str, Any]] = []
    for line, content in reversed(comment_lines):
        for field in content.split("|"):
            key, separator, text = field.partition(":")
            if separator and key and text:
                result.append({"kind": key, "text": text, "path": rel, "line": line})
    if not result and target.get("contract"):
        for field in str(target["contract"]).split("|"):
            key, separator, text = field.partition(":")
            if separator and key and text:
                result.append({"kind": key, "text": text, "path": rel, "line": int(target.get("line", 0))})
    return result


def _related_item(root: Path, symbol: dict[str, Any] | None, edge: dict[str, Any], *, direction: str,
                 indexed_files: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    identifier = str((symbol or {}).get("id") or (edge.get("from") if direction == "incoming" else edge.get("to")))
    fallback_file = str(edge.get("file", ""))
    location = _location(root, symbol or {})
    if location is None and fallback_file and (root / fallback_file).is_file():
        start, end = int(edge.get("start", 0)), int(edge.get("end", edge.get("start", 0)))
        location = {
            "path": fallback_file,
            "line": _line_for_offset(root / fallback_file, start),
            "end_line": _line_for_offset(root / fallback_file, end),
            "byte_start": start,
            "byte_end": end,
            "content_sha256": sha256_file(root / fallback_file),
        }
    # Anchor the location to the hash checked against the index above. Do not
    # re-stamp an old byte range with a later current-file hash if the file
    # changes between freshness validation and location construction.
    if location is not None:
        indexed = (indexed_files or {}).get(str(location.get("path", "")))
        location["content_sha256"] = str((indexed or {}).get("hash", ""))
    edge_semantic = not any(edge.get(key) for key in ("degraded", "lexical"))
    if edge.get("semantic") is False or edge.get("degraded") or edge.get("lexical"):
        edge_semantic = False
    return {
        "id": identifier,
        "name": str((symbol or {}).get("qualified_name") or (symbol or {}).get("name") or identifier),
        "kind": str((symbol or {}).get("kind") or ("test" if edge.get("type") == "test_relationship" else "unresolved")),
        "relation": str(edge.get("type", "reference")),
        "direction": direction,
        "location": location,
        "semantic": bool(symbol and not symbol.get("degraded") and edge_semantic),
    }


def _collect_context(root: Path, target: dict[str, Any], symbols: list[dict[str, Any]], edges: list[dict[str, Any]],
                     indexed_files: dict[str, dict[str, Any]] | None = None, *,
                     stale_files: set[str] | None = None, stale_tus: set[str] | None = None,
                     provenance_incomplete: bool = False) -> tuple[dict[str, list[dict[str, Any]]], int, int]:
    """Collect only relationships whose indexed source files still match.

    An old byte offset must never be paired with a newly computed whole-file
    hash.  That would make a stale relationship look freshly verified after a
    source edit (especially when the edited caller no longer mentions target).
    """
    by_id = {symbol["id"]: symbol for symbol in symbols}
    indexed_files = indexed_files or {}
    stale_files = stale_files or set()
    stale_tus = stale_tus or set()
    current_matches: dict[str, bool] = {}

    def path_is_current(rel: str) -> bool:
        if not rel:
            return False
        if rel not in current_matches:
            indexed = indexed_files.get(rel)
            path = root / rel
            current_matches[rel] = bool(
                indexed and indexed.get("hash") and path.is_file()
                and sha256_file(path) == indexed.get("hash")
            )
        return current_matches[rel]

    target_id = target["id"]
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in CONTEXT_KEYS}
    stale_relationships = 0
    unknown_relationships = 0
    outgoing = {
        "type_use": "types", "inheritance": "types",
        "member_access": "dependencies", "macro_expansion": "dependencies",
        "nonlocal_read": "dependencies", "nonlocal_write": "dependencies",
        "call": "callees",
    }
    incoming = {"call": "callers", "test_relationship": "tests"}
    for edge in edges:
        category = None
        direction = "outgoing"
        other = None
        if edge.get("from") == target_id and edge.get("type") in outgoing:
            category = outgoing[str(edge["type"])]
            other = by_id.get(edge.get("to"))
        elif edge.get("to") == target_id and edge.get("type") in incoming:
            category = incoming[str(edge["type"])]
            direction = "incoming"
            other = by_id.get(edge.get("from"))
        if category:
            # The edge's source file and the related symbol's defining file
            # both contribute to the relationship's provenance.  Requiring
            # both prevents a current caller declaration from validating an
            # edge whose callsite came from an older version of that file.
            source_paths = {str(edge.get("file", ""))}
            if other:
                source_paths.add(str(other.get("file", "")))
            translation_unit = str(edge.get("translation_unit", ""))
            if provenance_incomplete and not translation_unit:
                unknown_relationships += 1
                continue
            if (source_paths & stale_files or translation_unit in stale_tus
                    or not source_paths or not all(path_is_current(rel) for rel in source_paths)):
                stale_relationships += 1
                continue
            buckets[category].append(_related_item(
                root, other, edge, direction=direction, indexed_files=indexed_files,
            ))
    for category in CONTEXT_KEYS:
        unique = {
            stable_json({key: item.get(key) for key in ("id", "relation", "direction", "location")}): item
            for item in buckets[category]
        }
        buckets[category] = sorted(unique.values(), key=lambda item: (item["name"], item["relation"], item["id"]))
    return buckets, stale_relationships, unknown_relationships


def _bound_context(context: dict[str, list[dict[str, Any]]], max_items: int) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    selected = {key: [] for key in CONTEXT_KEYS}
    offsets = {key: 0 for key in CONTEXT_KEYS}
    remaining = max_items
    while remaining:
        progressed = False
        for key in CONTEXT_KEYS:
            if offsets[key] < len(context[key]) and remaining:
                selected[key].append(context[key][offsets[key]])
                offsets[key] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    omitted = {key: len(context[key]) - len(selected[key]) for key in CONTEXT_KEYS}
    return selected, omitted


def _retrieval_target(root: Path, target: dict[str, Any]) -> str:
    """Prefer a readable route only when it resolves to the same representation."""
    identity = str(target["id"])
    store = open_query_store(root)
    if store:
        try:
            name = str(target.get("qualified_name") or target.get("name") or "")
            exact = store.exact(name, 2) if name else []
            if len(exact) == 1 and exact[0].get("id") == identity:
                return name
            file, line = str(target.get("file", "")), int(target.get("line", 0))
            located = store.location(file, line, 1)
            if located and located[0].get("id") == identity:
                return f"{file}:{line}"
        finally:
            store.close()
    return identity


def _handle(kind: str, target: str, argv: list[str]) -> dict[str, Any]:
    digest = hashlib.sha256(stable_json([kind, target, argv]).encode()).hexdigest()[:16]
    return {"handle": f"ctxpp:{kind}:{digest}", "kind": kind, "target": target, "argv": argv}


def build_context_packet(root: Path, requested_target: str, target: dict[str, Any], *, intent: str,
                         budget: int, max_items: int, tokenizer: Tokenizer) -> dict[str, Any]:
    if budget <= 0:
        raise CtxppError("packet budget must be positive")
    if not 1 <= max_items <= 64:
        raise CtxppError("packet max-items must be between 1 and 64")
    recovered, coverage = recover_target_representation(root, target, intent)
    location = _location(root, recovered)
    if location is None or not coverage.get("range_valid"):
        raise CtxppError(f"target has no verified canonical range: {requested_target}")
    content = source_text(root, recovered)
    meta, files, symbols, edges, graph_truncated = bounded_graph(root, recovered)
    indexed_file = next((item for item in files if item.get("path") == recovered.get("file")), {})
    source_hash_matches = indexed_file.get("hash") == location["content_sha256"]
    indexed_files = {str(item.get("path", "")): item for item in files if item.get("path")}
    collected, stale_relationships, unknown_relationships = _collect_context(
        root, recovered, symbols, edges, indexed_files,
        stale_files={str(path) for path in meta.get("_stale_relationship_files", [])},
        stale_tus={str(tu) for tu in meta.get("_stale_relationship_tus", [])},
        provenance_incomplete=bool(meta.get("_relationship_provenance_incomplete")),
    )
    # bounded_graph can report relationships it had to suppress while finding
    # the bounded incident neighborhood (for example stale incoming edges).
    stale_relationships += max(0, int(meta.get("_stale_relationships", 0) or 0))
    unknown_relationships += max(0, int(meta.get("_unknown_relationships", 0) or 0))
    semantic_relationships = bool(
        meta.get("semantic") and not recovered.get("degraded") and source_hash_matches
        and not stale_relationships and not unknown_relationships
    )
    context, omitted = _bound_context(collected, max_items)
    if stale_relationships:
        omitted["stale_relationships"] = stale_relationships
    if unknown_relationships:
        omitted["unknown_relationships"] = unknown_relationships
    invariants = _contract_items(root, recovered)
    target_payload = {
        "id": str(recovered["id"]),
        "name": str(recovered.get("qualified_name") or recovered.get("name") or recovered["id"]),
        "kind": str(recovered.get("kind", "unknown")),
        "signature": str(recovered.get("signature", "")),
        "type": str(recovered.get("type", "")),
        "definition": bool(recovered.get("definition")),
        "canonical": True,
        "location": location,
        "text_sha256": sha256_bytes(content.encode()),
        "content": content,
    }
    identity = source_identity(root, [target_payload["location"]["path"]])
    packet: dict[str, Any] = {
        "format": "CTXPP-CONTEXT-PACKET/1",
        "schema_version": 1,
        "readonly": True,
        "request": {"target": requested_target, "intent": intent, "budget_tokens": budget, "max_items": max_items},
        "source_identity": identity,
        "target": target_payload,
        "context": context,
        "invariants": invariants,
        "trust": {
            "canonical_source": "authoritative",
            "target_range": "hash-verified" if source_hash_matches else "unverified",
            "relationships": "semantic" if semantic_relationships else "lexical-or-partial",
            "index_backend": str(meta.get("backend", "unknown")),
            "index_semantic": bool(meta.get("semantic")),
            "index_incomplete": bool(meta.get("incomplete")),
            "index_sha256": None,
            "index_generation": None,
        },
        "coverage": {
            "sufficient": False,
            "target_complete": bool(coverage.get("target_complete")),
            "representation_kind": str(coverage.get("representation_kind", "unknown")),
            "omitted": omitted,
            "budget_exceeded": False,
        },
        "expansions": [],
    }
    if meta.get("_generation"):
        packet["trust"]["index_generation"] = meta["_generation"]
    else:
        packet["trust"]["index_sha256"] = sha256_file(state_dir(root) / "index.jsonl")
    if graph_truncated:
        packet["coverage"]["omitted"]["graph"] = 1
    base_sufficient = bool(coverage.get("target_complete") and semantic_relationships and not graph_truncated)

    expansion_target = _retrieval_target(root, recovered)

    def finalize_shape() -> int:
        packet["expansions"] = [_handle("canonical-source", target_payload["id"], ["expand", expansion_target])]
        if any(packet["coverage"]["omitted"].values()):
            packet["expansions"].append(_handle(
                "more-context", target_payload["id"],
                ["packet", expansion_target, "--intent", intent,
                 "--budget", str(max(budget * 2, budget + 1)),
                 "--max-items", str(min(64, max_items * 2))],
            ))
        measured = {**packet, "estimated_tokens": 0, "packet_hash": "0" * 64}
        return tokenizer.count(stable_json(measured)).count

    estimated = finalize_shape()
    while estimated > budget and any(packet["context"].values()):
        category = next(key for key in reversed(CONTEXT_KEYS) if packet["context"][key])
        packet["context"][category].pop()
        packet["coverage"]["omitted"][category] += 1
        estimated = finalize_shape()
    if estimated > budget and packet["target"]["content"]:
        # Keep the canonical range/hash and expansion handle, not an oversized dump.
        packet["target"]["content"] = ""
        packet["coverage"]["target_complete"] = False
        packet["coverage"]["omitted"]["target"] = 1
        base_sufficient = False
        estimated = finalize_shape()
    packet["coverage"]["budget_exceeded"] = estimated > budget
    packet["coverage"]["sufficient"] = (
        base_sufficient
        and not packet["coverage"]["budget_exceeded"]
        and not any(packet["coverage"]["omitted"].values())
    )
    packet["estimated_tokens"] = estimated
    packet["packet_hash"] = sha256_bytes(stable_json(packet).encode())
    return packet


def build_task_packet(root: Path, task_spec: dict[str, Any], targets: list[tuple[str, dict[str, Any]]], *,
                      consumer: str, intent: str, budget: int, max_items: int,
                      tokenizer: Tokenizer) -> dict[str, Any]:
    if intent not in INTENT_ALIASES:
        raise CtxppError(f"unsupported task packet intent: {intent}")
    if not targets:
        raise CtxppError("task packet requires at least one resolved target symbol")
    per_target_budget = max(256, budget // len(targets))
    packets = [
        build_context_packet(
            root, requested, target, intent=INTENT_ALIASES[intent],
            budget=per_target_budget, max_items=max_items, tokenizer=tokenizer,
        )
        for requested, target in targets
    ]
    canonical_targets = [packet["target"] for packet in packets]
    order = INTENT_ORDER[intent]
    support: dict[str, list[dict[str, Any]]] = {key: [] for key in order}
    omitted_optional: dict[str, int] = {key: 0 for key in order}
    remaining = max_items
    for key in order:
        unique: dict[str, dict[str, Any]] = {}
        for packet in packets:
            for item in packet["context"].get(key, []):
                unique[stable_json({field: item.get(field) for field in ("id", "relation", "direction", "location")})] = item
            omitted_optional[key] += int(packet["coverage"]["omitted"].get(key, 0))
        values = sorted(unique.values(), key=lambda item: (item["name"], item["relation"], item["id"]))
        support[key] = values[:remaining]
        omitted_optional[key] += max(0, len(values) - len(support[key]))
        remaining -= len(support[key])
    source = dict(source_identity(root, [item["location"]["path"] for item in canonical_targets]))
    source["algorithm"] = "todo-runtime-source-identity" if sibling_runtime_available() else "ctxpp-source-identity"
    source["algorithm_version"] = 1
    required_paths = sorted({str(path) for key in ("read_paths", "write_paths") for path in task_spec.get(key, [])})
    represented_paths = {item["location"]["path"] for item in canonical_targets}
    missing_required = [path for path in required_paths if path not in represented_paths and (root / path).is_file()]
    fresh = all(packet["trust"]["target_range"] == "hash-verified" for packet in packets)
    semantic = all(packet["trust"]["relationships"] == "semantic" for packet in packets)
    budget_exceeded = any(packet["coverage"]["budget_exceeded"] for packet in packets)
    sufficient_for = [] if missing_required or not fresh else [intent]
    confidence = "high" if fresh and semantic and not missing_required else ("medium" if fresh else "low")
    expansions = [
        _handle("canonical-source", target["id"], ["expand", target["id"]])
        for target in canonical_targets[:8]
    ]
    packet: dict[str, Any] = {
        "format": "CTXPP-CONTEXT-PACKET/2",
        "schema_version": 2,
        "readonly": True,
        "consumer": consumer,
        "task_spec": task_spec,
        "request": {
            "intent": intent,
            "budget_tokens": budget,
            "max_items": max_items,
            "target_count": len(canonical_targets),
        },
        "source_identity": source,
        "canonical_targets": canonical_targets,
        "semantic_support": support,
        "invariants": [item for packet_v1 in packets for item in packet_v1["invariants"]],
        "trust": {
            "sufficient_for": sufficient_for,
            "missing_required": missing_required,
            "omitted_optional": omitted_optional,
            "confidence": confidence,
            "freshness": "hash-verified" if fresh else "partial",
            "source_authority": "canonical-repository-source",
            "relationships": "semantic" if semantic else "lexical-or-partial",
        },
        "expansions": expansions,
        "budget_exceeded": budget_exceeded,
    }
    measured = {**packet, "estimated_tokens": 0, "packet_hash": "0" * 64}
    estimated = tokenizer.count(stable_json(measured)).count
    while estimated > budget and any(packet["semantic_support"].values()):
        key = next(name for name in reversed(order) if packet["semantic_support"][name])
        packet["semantic_support"][key].pop()
        packet["trust"]["omitted_optional"][key] += 1
        measured = {**packet, "estimated_tokens": 0, "packet_hash": "0" * 64}
        estimated = tokenizer.count(stable_json(measured)).count
    packet["budget_exceeded"] = estimated > budget
    if packet["budget_exceeded"]:
        packet["trust"]["sufficient_for"] = []
    packet["estimated_tokens"] = estimated
    packet["packet_hash"] = sha256_bytes(stable_json(packet).encode())
    return packet


def sibling_runtime_available() -> bool:
    return False


AGENT_VIEW_MAX_BYTES = 6000
TARGET_VIEW_MAX_BYTES = 3600
TARGET_VIEW_MAX_LINES = 72
RELATED_VIEW_MAX_BYTES = 1200
RELATED_VIEW_MAX_LINES = 14


def _view_root(packet: dict[str, Any]) -> Path | None:
    try:
        return Path(str(packet["source_identity"]["repo_root"])).resolve()
    except (KeyError, OSError, RuntimeError):
        return None


def _verified_source(root: Path, location: dict[str, Any]) -> tuple[Path, bytes] | None:
    """Return current source only when its indexed whole-file hash still matches."""
    try:
        rel = str(location["path"])
        candidate = (root / rel).resolve(strict=True)
        candidate.relative_to(root.resolve())
        data = candidate.read_bytes()
        if not location.get("content_sha256") or sha256_bytes(data) != location["content_sha256"]:
            return None
        return candidate, data
    except (KeyError, OSError, RuntimeError, ValueError):
        return None


def _numbered_source(text: str, first_line: int, *, highlight: tuple[int, int] | None = None,
                     max_line_chars: int = 480) -> tuple[str, bool]:
    result: list[str] = []
    clipped = False
    for offset, line in enumerate(text.splitlines()):
        number = first_line + offset
        marker = ">" if highlight and highlight[0] <= number <= highlight[1] else " "
        if len(line) > max_line_chars:
            line = line[:max_line_chars] + " … [line clipped]"
            clipped = True
        result.append(f"{marker}{number:>5} | {line}")
    return "\n".join(result), clipped


def _range_text(data: bytes, location: dict[str, Any]) -> tuple[str, int] | None:
    try:
        start, end = int(location["byte_start"]), int(location["byte_end"])
        if start < 0 or end <= start or end > len(data):
            return None
        line = data[:start].count(b"\n") + 1
        return data[start:end].decode("utf-8", errors="replace"), line
    except (KeyError, ValueError, TypeError):
        return None


def _target_block(root: Path, target: dict[str, Any], *, label: str = "target") -> tuple[str | None, str | None]:
    location = target.get("location") or {}
    source = _verified_source(root, location)
    if source is None:
        return None, f"{label}: source file is missing or changed since indexing; no code excerpt shown"
    _, data = source
    selected = _range_text(data, location)
    if selected is None:
        return None, f"{label}: indexed byte range is invalid; no code excerpt shown"
    body, first_line = selected
    if sha256_bytes(body.encode("utf-8")) != target.get("text_sha256"):
        return None, f"{label}: range hash does not match; no code excerpt shown"
    line_count = len(body.splitlines())
    body_bytes = len(body.encode("utf-8"))
    if body_bytes > TARGET_VIEW_MAX_BYTES or line_count > TARGET_VIEW_MAX_LINES:
        return None, (f"{label}: indivisible source range is {line_count} lines / {body_bytes} bytes; "
                      "withheld because it exceeds the compact inspect limit")
    if any(len(line) > 480 for line in body.splitlines()):
        return None, f"{label}: source contains an overlong line; full range withheld from compact inspect"
    numbered, _ = _numbered_source(body, first_line, max_line_chars=TARGET_VIEW_MAX_BYTES)
    return (
        f"{label} (canonical source verified; complete range; {location['path']}:{location['line']}-{location['end_line']})\n"
        f"{numbered}",
        None,
    )


def _related_range_block(root: Path, item: dict[str, Any], *, label: str,
                         max_lines: int = RELATED_VIEW_MAX_LINES,
                         max_bytes: int = RELATED_VIEW_MAX_BYTES) -> tuple[str | None, str | None]:
    """Render an indexed related symbol range after whole-file freshness validation."""
    location = item.get("location") or {}
    source = _verified_source(root, location)
    if source is None:
        return None, f"{label}: source file is missing or changed; excerpt withheld"
    _, data = source
    selected = _range_text(data, location)
    if selected is None:
        return None, f"{label}: indexed byte range is invalid; excerpt withheld"
    body, first_line = selected
    range_hash = sha256_bytes(body.encode("utf-8"))
    expected = item.get("text_sha256")
    if expected and expected != range_hash:
        return None, f"{label}: indexed range hash does not match; excerpt withheld"
    lines = body.splitlines()
    if not lines:
        return None, f"{label}: empty indexed source range"
    shown_lines = lines[:max_lines]
    shown = "\n".join(shown_lines)
    numbered, clipped = _numbered_source(shown, first_line, max_line_chars=320)
    partial = len(lines) > len(shown_lines) or len(body.encode("utf-8")) > max_bytes or clipped
    if len(numbered.encode("utf-8")) > max_bytes:
        return None, f"{label}: indexed source range is too large; open {location.get('path')}:{location.get('line')}"
    range_note = "partial indexed range" if partial else "indexed range verified"
    return (
        f"{label} ({range_note}; {location.get('path')}:{location.get('line')}-{location.get('end_line')})\n"
        f"{numbered}",
        None,
    )


def _test_graph(root: Path, target: dict[str, Any]):
    """Return the bounded v2 incident graph with the same freshness checks as packet building."""
    if not (state_dir(root) / "index.sqlite").is_file():
        return None, "CTXPP-INDEX/2 unavailable; exact indexed callsite expansion skipped"
    location = target.get("location") or {}
    graph_target = {
        "id": target.get("id"),
        "name": target.get("name", ""),
        "qualified_name": target.get("name", ""),
        "kind": target.get("kind", ""),
        "file": location.get("path", ""),
        "start": location.get("byte_start", 0),
        "end": location.get("byte_end", 0),
    }
    try:
        meta, files, symbols, edges, truncated = bounded_graph(root, graph_target, limit=512)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return None, f"bounded indexed test context unavailable ({type(exc).__name__}); exact callsite expansion skipped"
    indexed_files = {str(item.get("path", "")): item for item in files if item.get("path")}
    context, stale_count, unknown_count = _collect_context(
        root, graph_target, symbols, edges, indexed_files,
        stale_files={str(path) for path in meta.get("_stale_relationship_files", [])},
        stale_tus={str(tu) for tu in meta.get("_stale_relationship_tus", [])},
        provenance_incomplete=bool(meta.get("_relationship_provenance_incomplete")),
    )
    return {
        "meta": meta,
        "files": indexed_files,
        "symbols": {str(item.get("id", "")): item for item in symbols if item.get("id")},
        "edges": edges,
        "truncated": truncated,
        "context": context,
        "stale_count": stale_count,
        "unknown_count": unknown_count,
    }, None


def _target_declaration_block(root: Path, target: dict[str, Any],
                              indexed_symbol: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Show an exact declaration range while omitting a function body for test intent."""
    location = target.get("location") or {}
    source = _verified_source(root, location)
    if source is None:
        return None, "target declaration: source file is missing or changed; excerpt withheld"
    _, data = source
    selected = _range_text(data, location)
    if selected is None:
        return None, "target declaration: indexed byte range is invalid; excerpt withheld"
    body, first_line = selected
    if sha256_bytes(body.encode("utf-8")) != target.get("text_sha256"):
        return None, "target declaration: indexed range hash does not match; excerpt withheld"
    start, end = int(location["byte_start"]), int(location["byte_end"])
    kind = str(target.get("kind", ""))
    is_definition = bool(target.get("definition", (indexed_symbol or {}).get("definition", False)))
    is_data_shape = kind in DATA_SHAPE_KINDS
    declaration_end: int | None = None
    body_omitted = False
    body_start = (indexed_symbol or {}).get("body_start")
    try:
        body_start_value = int(body_start) if body_start is not None else None
    except (TypeError, ValueError):
        body_start_value = None
    if is_data_shape or not is_definition:
        declaration_end = end
    elif body_start_value is not None and start < body_start_value <= end:
        declaration_end = body_start_value
        body_omitted = True
    else:
        signature = str((indexed_symbol or {}).get("signature") or target.get("signature") or "")
        signature_tokens = [token.text for token in _cpp_tokens(signature.encode("utf-8"))
                           if token.kind not in ("comment", "preprocessor")]
        source_tokens = [token for token in _cpp_tokens(data[start:end])
                         if token.kind not in ("comment", "preprocessor")]
        if signature_tokens and len(source_tokens) >= len(signature_tokens) and all(
            token.text == expected for token, expected in zip(source_tokens, signature_tokens)
        ):
            following = source_tokens[len(signature_tokens):]
            if not following or following[0].text in ("{", ";"):
                declaration_end = start + source_tokens[len(signature_tokens) - 1].end
                body_omitted = bool(following and following[0].text == "{")
    if declaration_end is None or not start < declaration_end <= end:
        return None, f"target declaration boundary unavailable; open {location.get('path')}:{location.get('line')}"
    snippet = data[start:declaration_end].decode("utf-8", errors="replace").rstrip()
    if not snippet:
        return None, "target declaration is empty; excerpt withheld"
    if len(snippet.encode("utf-8")) > 4096 or len(snippet.splitlines()) > 64:
        return None, (f"target declaration is {len(snippet.splitlines())} lines / {len(snippet.encode('utf-8'))} bytes; "
                      f"open {location.get('path')}:{location.get('line')}")
    last_line = data[:declaration_end].count(b"\n") + 1
    numbered, clipped = _numbered_source(snippet, first_line, max_line_chars=480)
    note = "canonical source verified; body omitted" if body_omitted else "canonical source verified; complete declaration"
    if is_data_shape:
        note = "canonical source verified; complete data shape"
    if clipped:
        note += "; long line clipped"
    return f"target declaration ({note}; {location['path']}:{first_line}-{last_line})\n{numbered}", None


def _test_callsite_clusters(edges: list[dict[str, Any]], test_items: list[dict[str, Any]],
                            meta: dict[str, Any], target_id: str) -> list[dict[str, Any]]:
    by_test = {str(item.get("id", "")): item for item in test_items}
    stale_files = set(str(path) for path in meta.get("_stale_relationship_files", []))
    stale_tus = set(str(path) for path in meta.get("_stale_relationship_tus", []))
    provenance_incomplete = bool(meta.get("_relationship_provenance_incomplete"))
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for edge in edges:
        source_id = str(edge.get("from", ""))
        path = str(edge.get("file", ""))
        tu = str(edge.get("translation_unit", ""))
        if (edge.get("type") != "test_relationship" or str(edge.get("to", "")) != target_id
                or not source_id or source_id not in by_test or path in stale_files or tu in stale_tus
                or (provenance_incomplete and not tu)):
            continue
        try:
            start, end = int(edge["start"]), int(edge["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not path or start < 0 or end <= start:
            continue
        grouped.setdefault((path, source_id), []).append(edge)

    clusters: list[dict[str, Any]] = []
    for (path, source_id), source_edges in sorted(grouped.items()):
        ordered = sorted(source_edges, key=lambda edge: (int(edge["start"]), -int(edge["end"])))
        active: list[dict[str, Any]] = []
        active_end = -1
        for edge in ordered:
            start, end = int(edge["start"]), int(edge["end"])
            if active and start > active_end:
                chosen = max(active, key=lambda item: int(item["end"]) - int(item["start"]))
                clusters.append({"path": path, "source_id": source_id, "edge": chosen,
                                 "item": by_test[source_id]})
                active = []
                active_end = -1
            active.append(edge)
            active_end = max(active_end, end)
        if active:
            chosen = max(active, key=lambda item: int(item["end"]) - int(item["start"]))
            clusters.append({"path": path, "source_id": source_id, "edge": chosen,
                             "item": by_test[source_id]})
    deduplicated: dict[tuple[str, int, int], dict[str, Any]] = {}
    for cluster in clusters:
        edge = cluster["edge"]
        identity = (cluster["path"], int(edge["start"]), int(edge["end"]))
        deduplicated.setdefault(identity, cluster)
    return sorted(deduplicated.values(), key=lambda item: (item["path"], int(item["edge"]["start"])))


def _test_callsite_block(root: Path, cluster: dict[str, Any], indexed_files: dict[str, dict[str, Any]],
                         relationship_status: str) -> tuple[str | None, str | None]:
    edge, item = cluster["edge"], cluster["item"]
    path = str(cluster["path"])
    file_record = indexed_files.get(path, {})
    location = {
        "path": path,
        "byte_start": int(edge["start"]),
        "byte_end": int(edge["end"]),
        "content_sha256": str(file_record.get("hash", "")),
    }
    source = _verified_source(root, location)
    if source is None:
        return None, f"test callsite: {path} source is missing or changed; excerpt withheld"
    _, data = source
    selected = _range_text(data, location)
    if selected is None:
        return None, f"test callsite: invalid indexed range; open {path}:{_line_for_offset(root / path, location['byte_start'])}"
    _, first_line = selected
    last_line = data[:location["byte_end"]].count(b"\n") + 1
    all_lines = data.decode("utf-8", errors="replace").splitlines()
    if not all_lines or not 1 <= first_line <= len(all_lines):
        return None, f"test callsite: out-of-file indexed range; open {path}:{first_line}"
    span_lines = max(1, last_line - first_line + 1)
    max_window_lines = min(12, RELATED_VIEW_MAX_LINES)
    if span_lines >= max_window_lines:
        start_line, end_line = first_line, min(len(all_lines), first_line + max_window_lines - 1)
    else:
        context_room = max_window_lines - span_lines
        before = min(2, context_room // 3)
        after = min(4, context_room - before)
        start_line = max(1, first_line - before)
        end_line = min(len(all_lines), last_line + after)
    numbered, clipped = _numbered_source(
        "\n".join(all_lines[start_line - 1:end_line]), start_line,
        highlight=(first_line, min(last_line, end_line)), max_line_chars=320,
    )
    if len(numbered.encode("utf-8")) > RELATED_VIEW_MAX_BYTES:
        return None, f"test callsite window is too large; open {path}:{first_line}"
    relation = ("semantic indexed callsite" if relationship_status == "semantic"
                else "candidate callsite (relationship not verified)")
    title = f"tests — {relation}; bounded local window: {item.get('name', item.get('id', 'test'))}"
    note = f"source: {path}:{start_line}-{end_line}; indexed callsite: {first_line}-{last_line}"
    if clipped:
        note += "; long line clipped"
    return f"{title}\n{note}\n{numbered}", None


def _configured_test_build_hints(root: Path, test_paths: set[str]) -> list[str]:
    if not test_paths:
        return []
    config_path = root / ".ctxpp.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        configured = config.get("tool", {}).get("compilation_database", "compile_commands.json")
        database = Path(str(configured))
        if not database.is_absolute():
            database = root / database
        database = database.resolve(strict=True)
        database.relative_to(root.resolve())
        if not database.is_file() or database.stat().st_size > 8 * 1024 * 1024:
            return []
        entries = json.loads(database.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return []
    build_dir = database.parent
    try:
        build_arg = build_dir.relative_to(root.resolve()).as_posix() or "."
    except ValueError:
        build_arg = build_dir.as_posix()
    target_pattern = re.compile(r"(?:^|[/\\])CMakeFiles[/\\]([^/\\]+)\.dir(?:[/\\]|$)")
    hints: list[str] = []
    seen: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            source = Path(str(entry.get("file", "")))
            if not source.is_absolute():
                source = Path(str(entry.get("directory", root))) / source
            relative_source = source.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        if relative_source not in test_paths:
            continue
        outputs = [str(entry.get("output", ""))]
        arguments = entry.get("arguments")
        if isinstance(arguments, list):
            command = [str(value) for value in arguments]
        else:
            try:
                command = shlex.split(str(entry.get("command", "")))
            except ValueError:
                command = []
        for index, argument in enumerate(command[:-1]):
            if argument == "-o":
                outputs.append(command[index + 1])
        outputs.extend(argument[2:] for argument in command if argument.startswith("-o") and len(argument) > 2)
        for output in outputs:
            match = target_pattern.search(output)
            if not match:
                continue
            target_name = match.group(1)
            if target_name in seen:
                continue
            seen.add(target_name)
            command_text = shlex.join(["cmake", "--build", build_arg, "--target", target_name])
            hints.append(
                f"build target hint from {relative_source}: {command_text} "
                "(compile-database mapping only; build/test status unknown)"
            )
            if len(hints) >= 3:
                return hints
    return hints


def _test_intent_blocks(root: Path, target: dict[str, Any], packet: dict[str, Any],
                        relationship_trust: str) -> tuple[list[tuple[str, str]], list[str]]:
    blocks: list[tuple[str, str]] = []
    skipped: list[str] = []
    graph, graph_error = _test_graph(root, target)
    if graph is None:
        skipped.append(graph_error or "bounded indexed test context unavailable")
        context = packet.get("context", {})
        groups = _contract_groups(packet.get("invariants", []))
        locations = {str(target.get("location", {}).get("path", "")): target.get("location", {})}
        for group in groups:
            block, reason = _contract_block(root, group, locations)
            if block:
                blocks.append(("contract", block))
            elif reason:
                skipped.append(reason)
        declaration, reason = _target_declaration_block(root, target, target)
        if declaration:
            blocks.append(("target declaration", declaration))
        elif reason:
            skipped.append(reason)
        for item in context.get("types", [])[:4]:
            block, reason = _related_range_block(
                root, item, label=f"types: {item.get('name', item.get('id', 'item'))}",
                max_lines=48, max_bytes=3072,
            )
            if block:
                blocks.append(("type", block))
            elif reason:
                skipped.append(reason)
        for item in context.get("tests", [])[:4]:
            status = "semantic" if relationship_trust == "semantic" and item.get("semantic") else "candidate"
            block, reason = _nearby_source_block(root, item, category="tests", relationship_status=status)
            if block:
                blocks.append(("test source", block))
            elif reason:
                skipped.append(reason)
        skipped.append("fallback test windows are symbol-anchored, not exact indexed callsite ranges")
        test_paths = {
            str((item.get("location") or {}).get("path", ""))
            for item in context.get("tests", [])
            if (item.get("location") or {}).get("path")
        }
        for hint in _configured_test_build_hints(root, test_paths):
            blocks.append(("build target hint", hint))
        return blocks, skipped

    meta = graph["meta"]
    test_items = graph["context"]["tests"]
    types = graph["context"]["types"]
    target_symbol = graph["symbols"].get(str(target.get("id", "")))
    declaration, reason = _target_declaration_block(root, target, target_symbol)
    for group in _contract_groups(packet.get("invariants", [])):
        location_map = {str(target.get("location", {}).get("path", "")): target.get("location", {})}
        block, contract_error = _contract_block(root, group, location_map)
        if block:
            blocks.append(("contract", block))
        elif contract_error:
            skipped.append(contract_error)
    if declaration:
        blocks.append(("target declaration", declaration))
    elif reason:
        skipped.append(reason)

    test_clusters = _test_callsite_clusters(
        graph["edges"], test_items, meta, str(target.get("id", "")),
    )
    max_items = max(1, int(packet.get("request", {}).get("max_items", 12)))
    all_test_cluster_count = len(test_clusters)
    type_items = sorted(types, key=lambda item: (
        0 if str(item.get("kind", "")) in DATA_SHAPE_KINDS - {"EnumDecl"} else 1,
        str(item.get("name", "")), str(item.get("id", "")),
    ))[:max_items]
    test_clusters = test_clusters[:max_items]
    selected: list[tuple[str, Any]] = []
    type_index = test_index = 0
    while len(selected) < max_items and (type_index < len(type_items) or test_index < len(test_clusters)):
        if type_index < len(type_items):
            selected.append(("type", type_items[type_index]))
            type_index += 1
        if len(selected) < max_items and test_index < len(test_clusters):
            selected.append(("test", test_clusters[test_index]))
            test_index += 1

    chosen_test_paths: set[str] = set()
    for category, item in selected:
        if category == "type":
            block, block_error = _related_range_block(
                root, item, label=f"types: {item.get('name', item.get('id', 'item'))}",
                max_lines=48, max_bytes=3072,
            )
            if block:
                blocks.append((f"type:{item.get('id', item.get('name', 'type'))}", block))
            elif block_error:
                skipped.append(block_error)
            continue
        cluster = item
        test_item = cluster["item"]
        status = ("semantic" if relationship_trust == "semantic" and test_item.get("semantic")
                  else "candidate")
        block, block_error = _test_callsite_block(root, cluster, graph["files"], status)
        if block:
            blocks.append((f"test:{cluster['path']}:{cluster['edge']['start']}", block))
            chosen_test_paths.add(str(cluster["path"]))
        elif block_error:
            skipped.append(block_error)
    if len(types) > len(type_items):
        skipped.append(f"additional indexed data shapes not shown: {len(types) - len(type_items)}")
    if all_test_cluster_count > max_items:
        skipped.append(f"additional indexed test callsites not shown: {all_test_cluster_count - max_items}")
    if graph["truncated"]:
        skipped.append("incident graph reached the 512-edge bound; some types or tests may be omitted")
    if graph["stale_count"] or graph["unknown_count"]:
        skipped.append("stale or provenance-incomplete indexed relationships were withheld")
    for hint in _configured_test_build_hints(root, chosen_test_paths):
        blocks.append(("build target hint", hint))
    return blocks, skipped


def _nearby_source_block(root: Path, item: dict[str, Any], *, category: str,
                         relationship_status: str) -> tuple[str | None, str | None]:
    location = item.get("location")
    if not isinstance(location, dict):
        return None, f"{category}: {item.get('name', item.get('id', 'item'))} has no indexed source location"
    source = _verified_source(root, location)
    if source is None:
        return None, f"{category}: {item.get('name', item.get('id', 'item'))} source is missing or changed; excerpt withheld"
    _, data = source
    try:
        all_lines = data.decode("utf-8", errors="replace").splitlines()
        anchor_start = int(location["line"])
        anchor_end = max(anchor_start, int(location.get("end_line", anchor_start)))
    except (KeyError, TypeError, ValueError):
        return None, f"{category}: invalid source range for {item.get('name', item.get('id', 'item'))}"
    if not all_lines or anchor_start < 1 or anchor_start > len(all_lines):
        return None, f"{category}: out-of-file source range for {item.get('name', item.get('id', 'item'))}"
    start_line = max(1, anchor_start - 6)
    end_line = min(len(all_lines), anchor_end + 6)
    if end_line - start_line + 1 > RELATED_VIEW_MAX_LINES:
        end_line = start_line + RELATED_VIEW_MAX_LINES - 1
    window = "\n".join(all_lines[start_line - 1:end_line])
    numbered, clipped = _numbered_source(
        window, start_line, highlight=(anchor_start, anchor_end), max_line_chars=320,
    )
    if len(numbered.encode("utf-8")) > RELATED_VIEW_MAX_BYTES:
        return None, f"{category}: nearby source window is too large; open {location['path']}:{anchor_start}"
    kind = "semantic indexed relationship" if relationship_status == "semantic" else "candidate reference (not a verified relationship)"
    partial_note = f"partial nearby window; not the full {category[:-1] if category.endswith('s') else category}"
    if clipped:
        partial_note += "; long line clipped"
    title = f"{category} — {kind}; {partial_note}: {item.get('name', item.get('id', 'item'))}"
    return f"{title}\nsource: {location['path']}:{start_line}-{end_line}; indexed location: {anchor_start}-{anchor_end}\n{numbered}", None


def _contract_block(root: Path, invariants: list[dict[str, Any]],
                    target_locations: dict[str, dict[str, Any]]) -> tuple[str | None, str | None]:
    invariant = invariants[0]
    path = str(invariant.get("path", ""))
    try:
        line = int(invariant["line"])
    except (KeyError, TypeError, ValueError):
        return None, "contract: invalid source location"
    location = target_locations.get(path)
    if location is None:
        return None, f"contract at {path}:{line} has no hash-verified target file"
    source = _verified_source(root, location)
    if source is None:
        return None, f"contract at {path}:{line} source changed; excerpt withheld"
    _, data = source
    lines = data.decode("utf-8", errors="replace").splitlines()
    if line < 1 or line > len(lines):
        return None, f"contract at {path}:{line} is outside the current file"
    numbered, clipped = _numbered_source(lines[line - 1], line, max_line_chars=320)
    note = "contract source"
    if clipped:
        note += " (line clipped)"
    summary = "; ".join(f"{item.get('kind', 'note')}:{item.get('text', '')}" for item in invariants)
    return f"contracts at {path}:{line}: {summary}\n{note}: {path}:{line}\n{numbered}", None


def _contract_groups(invariants: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: dict[tuple[str, int | str], list[dict[str, Any]]] = {}
    for invariant in invariants:
        path = str(invariant.get("path", ""))
        try:
            line: int | str = int(invariant["line"])
        except (KeyError, TypeError, ValueError):
            line = "invalid"
        grouped.setdefault((path, line), []).append(invariant)
    return list(grouped.values())


def _context_blocks(root: Path, target: dict[str, Any], context: dict[str, list[dict[str, Any]]],
                    invariants: list[dict[str, Any]], intent: str,
                    relationship_trust: str) -> tuple[list[tuple[str, str]], list[str]]:
    blocks: list[tuple[str, str]] = []
    skipped: list[str] = []
    locations = {str(target.get("location", {}).get("path", "")): target.get("location", {})}
    for group in _contract_groups(invariants):
        text, reason = _contract_block(root, group, locations)
        if text:
            blocks.append(("contract", text))
        elif reason:
            skipped.append(reason)

    order_by_intent = {
        "edit": ("types", "dependencies", "callers", "tests", "callees"),
        "test": ("tests", "callers", "dependencies", "types", "callees"),
        "debug": ("callers", "callees", "dependencies", "tests", "types"),
        "investigate": ("callers", "tests", "dependencies", "callees", "types"),
        "understand": ("callers", "dependencies", "tests", "callees", "types"),
        "review": ("callers", "dependencies", "tests", "callees", "types"),
        "performance": ("callees", "callers", "types", "dependencies", "tests"),
    }
    limits = {"types": 3, "dependencies": 1, "callers": 1, "tests": 2, "callees": 1}
    def source_identity(item: dict[str, Any]) -> tuple[str, int, int] | None:
        location = item.get("location")
        if not isinstance(location, dict):
            return None
        try:
            return (str(location["path"]), int(location["byte_start"]), int(location["byte_end"]))
        except (KeyError, TypeError, ValueError):
            return None

    test_locations = {source_identity(item) for item in context.get("tests", [])}
    seen_locations: set[tuple[str, int, int]] = set()
    for category in order_by_intent.get(intent, order_by_intent["edit"]):
        for item in context.get(category, [])[:limits[category]]:
            identity = source_identity(item)
            # A callsite can be represented both as a caller and as a test edge.
            # Prefer its test label and show its source window once.
            if identity is not None and (identity in seen_locations or
                                         (category == "callers" and identity in test_locations)):
                continue
            if identity is not None:
                seen_locations.add(identity)
            item_trust = "semantic" if relationship_trust == "semantic" and item.get("semantic") else "candidate"
            if category in ("callers", "tests"):
                text, reason = _nearby_source_block(root, item, category=category, relationship_status=item_trust)
            else:
                text, reason = _related_range_block(root, item, label=f"{category}: {item.get('name', item.get('id', 'item'))}")
            if text:
                blocks.append((category, text))
            elif reason:
                skipped.append(reason)
    return blocks, skipped


def _bounded_human_view(header: str, blocks: list[tuple[str, str]], *, budget: int,
                         tokenizer: Tokenizer | None, route: str, omitted: list[str]) -> str:
    def exact_token_count(text: str) -> int:
        return tokenizer.count(text).count if tokenizer is not None else Tokenizer._estimate(text)

    selected: list[str] = []
    selected_names: list[str] = []
    excluded = [name for name, _ in blocks]
    def estimated_fit(text: str) -> bool:
        return len(text.encode("utf-8")) <= AGENT_VIEW_MAX_BYTES and Tokenizer._estimate(text) <= budget

    def assemble() -> str:
        pieces = [header, *selected]
        if excluded or omitted:
            details = list(dict.fromkeys([*excluded, *omitted]))[:8]
            pieces.append("not shown (inspect limit or source freshness): " + "; ".join(details))
        pieces.append(route)
        return "\n\n".join(piece for piece in pieces if piece.strip()) + "\n"

    result = assemble()
    for name, block in blocks:
        try:
            excluded.remove(name)
        except ValueError:
            pass
        selected.append(block)
        candidate = assemble()
        if estimated_fit(candidate):
            result = candidate
            selected_names.append(name)
        else:
            selected.pop()
            excluded.append(name)
    if tokenizer is not None and exact_token_count(result) > budget:
        while selected:
            selected.pop()
            excluded.append(selected_names.pop())
            result = assemble()
            if exact_token_count(result) <= budget:
                break
    if len(result.encode("utf-8")) > AGENT_VIEW_MAX_BYTES or exact_token_count(result) > budget:
        header = header.split("\n", 2)[0] + "\ninsufficient context within this inspect budget; raise --budget to request more."
        selected = []
        excluded = []
        omitted = []
        route = route if len(route.encode("utf-8")) < 280 else "next: rerun ctxpp inspect with a larger --budget"
        result = assemble()
        while (len(result.encode("utf-8")) > AGENT_VIEW_MAX_BYTES or exact_token_count(result) > budget) and route:
            route = ""
            result = assemble()
    if not result.endswith("\n"):
        result += "\n"
    return result


def _packet_route(packet: dict[str, Any], target: str, intent: str) -> str:
    request = packet.get("request", {})
    try:
        budget = max(1, int(request.get("budget_tokens", 2400)))
        max_items = max(1, int(request.get("max_items", 12)))
    except (TypeError, ValueError):
        budget, max_items = 2400, 12
    argv = ["packet", target, "--intent", intent, "--budget", str(min(1_000_000, budget * 2)),
            "--max-items", str(min(64, max_items * 2))]
    return "next bounded context: ctxpp " + shlex.join(argv)


def render_ambiguous_inspect(query: str, candidates: list[dict[str, Any]], *,
                            intent: str, budget: int) -> str:
    lines = [f"ambiguous inspect target {query!r}; choose one indexed candidate:"]
    for candidate in candidates:
        identity = str(candidate.get("id", ""))
        name = str(candidate.get("name") or identity)
        location = f"{candidate.get('file', '?')}:{candidate.get('line', '?')}"
        command = ["inspect", identity, "--intent", intent, "--budget", str(max(1, budget))]
        lines.append(f"- {name} — {location}")
        lines.append("  inspect: ctxpp " + shlex.join(command))
    lines.append("Each command resolves one exact candidate and keeps the requested context budget.")
    return "\n".join(lines) + "\n"


def render_inspect(packet: dict[str, Any], tokenizer: Tokenizer | None = None) -> str:
    target = packet["target"]
    location = target["location"]
    root = _view_root(packet)
    current = _verified_source(root, location) if root is not None else None
    selected = _range_text(current[1], location) if current else None
    trust = packet.get("trust", {})
    current_hash = bool(trust.get("target_range") == "hash-verified" and selected
                        and sha256_bytes(selected[0].encode("utf-8")) == target.get("text_sha256"))
    relationship_trust = str(trust.get("relationships", "lexical-or-partial"))
    request = packet.get("request", {})
    intent = str(request.get("intent", "edit"))
    budget = max(1, int(request.get("budget_tokens", 2400)))
    target_line = f"{target['name']} [{target['kind']}] — {location['path']}:{location['line']}-{location['end_line']}"
    header_lines = [target_line]
    if not current_hash:
        header_lines.append("source=canonical but stale or unverified; code excerpts withheld")
    if intent == "test":
        machine_coverage = "complete" if packet.get("coverage", {}).get("sufficient") else "partial"
        header_lines.append(
            f"intent=test relationships={relationship_trust} budget={budget} "
            f"tokens packet~={packet.get('estimated_tokens', 'unknown')} "
            f"machine-packet-coverage={machine_coverage} test-view=bounded-indexed"
        )
    else:
        header_lines.append(
            f"intent={intent} relationships={relationship_trust} "
            f"budget={budget} tokens packet~={packet.get('estimated_tokens', 'unknown')} "
            f"sufficient={int(bool(packet.get('coverage', {}).get('sufficient')))}"
        )
    omitted_counts = packet.get("coverage", {}).get("omitted", {})
    if omitted_counts and intent != "test":
        summary = ",".join(f"{key}={value}" for key, value in omitted_counts.items() if value)
        if summary:
            header_lines.append("indexed omissions: " + summary)
    blocks: list[tuple[str, str]] = []
    skipped: list[str] = []
    if root is None:
        skipped.append("repository root unavailable")
    elif current_hash:
        if intent == "test":
            test_blocks, test_skipped = _test_intent_blocks(root, target, packet, relationship_trust)
            blocks.extend(test_blocks)
            skipped.extend(test_skipped)
        elif not target.get("content") or not packet.get("coverage", {}).get("target_complete", True):
            skipped.append("target body was withheld by the packet budget; inspect is insufficient for a safe edit")
        else:
            block, reason = _target_block(root, target)
            if block:
                blocks.append(("target", block))
            elif reason:
                skipped.append(reason)
        if intent != "test":
            context_blocks, context_skipped = _context_blocks(
                root, target, packet.get("context", {}), packet.get("invariants", []),
                intent, relationship_trust,
            )
            blocks.extend(context_blocks)
            skipped.extend(context_skipped)
    else:
        skipped.append("rerun inspect so the current source range and relationships can be verified")
    route = _packet_route(packet, str(request.get("target", target["name"])),
                          intent)
    return _bounded_human_view("\n".join(header_lines), blocks, budget=budget, tokenizer=tokenizer,
                               route=route, omitted=skipped)


def render_task_inspect(packet: dict[str, Any], tokenizer: Tokenizer | None = None) -> str:
    targets = packet.get("canonical_targets", [])
    trust = packet.get("trust", {})
    ranges_current = trust.get("freshness") == "hash-verified"
    request = packet.get("request", {})
    budget = max(1, int(request.get("budget_tokens", 2400)))
    header_lines = [
        f"task intent={request.get('intent', 'edit')} consumer={packet.get('consumer', 'unknown')} targets={len(targets)}",
        f"trust confidence={trust.get('confidence', 'unknown')} freshness={trust.get('freshness', 'unknown')} "
        f"relationships={trust.get('relationships', 'unknown')} sufficient_for={','.join(trust.get('sufficient_for', [])) or 'none'}",
    ]
    if trust.get("missing_required"):
        header_lines.append("missing required paths: " + ", ".join(trust["missing_required"]))
    target_locations = {
        str(target.get("location", {}).get("path", "")): target.get("location", {})
        for target in targets
    }
    root = _view_root(packet)
    blocks: list[tuple[str, str]] = []
    skipped: list[str] = []
    if root is None:
        skipped.append("repository root unavailable")
    elif not ranges_current:
        skipped.append("indexed target ranges are stale or unverified; code excerpts withheld")
    for target in targets:
        location = target.get("location", {})
        header_lines.append(f"target {target.get('name', target.get('id', 'unknown'))}: {location.get('path')}:{location.get('line')}-{location.get('end_line')}")
        if root is None or not ranges_current:
            continue
        if not target.get("content"):
            skipped.append(f"target {target.get('name', target.get('id', 'unknown'))}: body withheld by its packet budget")
        else:
            block, reason = _target_block(root, target, label="canonical target")
            if block:
                blocks.append(("target", block))
            elif reason:
                skipped.append(reason)
    for group in _contract_groups(packet.get("invariants", [])):
        if root is None or not ranges_current:
            break
        block, reason = _contract_block(root, group, target_locations)
        if block:
            blocks.append(("contract", block))
        elif reason:
            skipped.append(reason)
    support = packet.get("semantic_support", {})
    for category in ("types", "dependencies", "callers", "tests", "callees"):
        for item in support.get(category, [])[:1 if category != "tests" else 2]:
            if root is None or not ranges_current:
                continue
            relationship_status = "semantic" if trust.get("relationships") == "semantic" and item.get("semantic") else "candidate"
            if category in ("callers", "tests"):
                block, reason = _nearby_source_block(root, item, category=category, relationship_status=relationship_status)
            else:
                block, reason = _related_range_block(root, item, label=f"{category}: {item.get('name', item.get('id', 'item'))}")
            if block:
                blocks.append((category, block))
            elif reason:
                skipped.append(reason)
    target_name = str(targets[0].get("name", targets[0].get("id", "target"))) if targets else "target"
    route = _packet_route(packet, target_name, str(request.get("intent", "edit")))
    return _bounded_human_view("\n".join(header_lines), blocks, budget=budget, tokenizer=tokenizer,
                               route=route, omitted=skipped)
