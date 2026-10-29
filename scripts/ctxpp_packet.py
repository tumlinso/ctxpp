from __future__ import annotations

import hashlib
import shlex
import sys
import subprocess
from pathlib import Path
from ctxpp_paths import state_dir
from ctxpp_runtime import open_query_store
from typing import Any

from ctxpp_lib import (CtxppError, Tokenizer, load_index, partition_index,
                       bounded_graph, recover_target_representation, sha256_bytes, sha256_file,
                       source_text, stable_json)


CONTEXT_KEYS = ("types", "dependencies", "callers", "callees", "tests")


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


def _related_range_block(root: Path, item: dict[str, Any], *, label: str) -> tuple[str | None, str | None]:
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
    shown_lines = lines[:RELATED_VIEW_MAX_LINES]
    shown = "\n".join(shown_lines)
    numbered, clipped = _numbered_source(shown, first_line, max_line_chars=320)
    partial = len(lines) > len(shown_lines) or len(body.encode("utf-8")) > RELATED_VIEW_MAX_BYTES or clipped
    if len(numbered.encode("utf-8")) > RELATED_VIEW_MAX_BYTES:
        return None, f"{label}: indexed source range is too large; open {location.get('path')}:{location.get('line')}"
    range_note = "partial indexed range" if partial else "indexed range verified"
    return (
        f"{label} ({range_note}; {location.get('path')}:{location.get('line')}-{location.get('end_line')})\n"
        f"{numbered}",
        None,
    )


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
    budget = max(1, int(request.get("budget_tokens", 2400)))
    target_line = f"{target['name']} [{target['kind']}] — {location['path']}:{location['line']}-{location['end_line']}"
    header_lines = [target_line]
    if not current_hash:
        header_lines.append("source=canonical but stale or unverified; code excerpts withheld")
    header_lines.append(
        f"intent={request.get('intent', 'edit')} relationships={relationship_trust} "
        f"budget={budget} tokens packet~={packet.get('estimated_tokens', 'unknown')} "
        f"sufficient={int(bool(packet.get('coverage', {}).get('sufficient')))}"
    )
    omitted_counts = packet.get("coverage", {}).get("omitted", {})
    if omitted_counts:
        summary = ",".join(f"{key}={value}" for key, value in omitted_counts.items() if value)
        if summary:
            header_lines.append("indexed omissions: " + summary)
    blocks: list[tuple[str, str]] = []
    skipped: list[str] = []
    if root is None:
        skipped.append("repository root unavailable")
    elif current_hash:
        if not target.get("content") or not packet.get("coverage", {}).get("target_complete", True):
            skipped.append("target body was withheld by the packet budget; inspect is insufficient for a safe edit")
        else:
            block, reason = _target_block(root, target)
            if block:
                blocks.append(("target", block))
            elif reason:
                skipped.append(reason)
        context_blocks, context_skipped = _context_blocks(
            root, target, packet.get("context", {}), packet.get("invariants", []),
            str(request.get("intent", "edit")), relationship_trust,
        )
        blocks.extend(context_blocks)
        skipped.extend(context_skipped)
    else:
        skipped.append("rerun inspect so the current source range and relationships can be verified")
    route = _packet_route(packet, str(request.get("target", target["name"])),
                          str(request.get("intent", "edit")))
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
