from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from ctxpp_lib import (CtxppError, Tokenizer, load_index, partition_index,
                       recover_target_representation, sha256_bytes, sha256_file,
                       source_text, stable_json)


CONTEXT_KEYS = ("types", "dependencies", "callers", "callees", "tests")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)


def source_identity(root: Path, relevant_paths: list[str]) -> dict[str, Any]:
    head_result = _git(root, "rev-parse", "HEAD")
    head = head_result.stdout.decode().strip() if head_result.returncode == 0 else None
    status_result = _git(root, "-c", "status.relativePaths=true", "status", "--porcelain=v1", "-z", "--untracked-files=all")
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


def _related_item(root: Path, symbol: dict[str, Any] | None, edge: dict[str, Any], *, direction: str) -> dict[str, Any]:
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
    return {
        "id": identifier,
        "name": str((symbol or {}).get("qualified_name") or (symbol or {}).get("name") or identifier),
        "kind": str((symbol or {}).get("kind") or ("test" if edge.get("type") == "test_relationship" else "unresolved")),
        "relation": str(edge.get("type", "reference")),
        "direction": direction,
        "location": location,
        "semantic": bool(symbol and not symbol.get("degraded")),
    }


def _collect_context(root: Path, target: dict[str, Any], symbols: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_id = {symbol["id"]: symbol for symbol in symbols}
    target_id = target["id"]
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in CONTEXT_KEYS}
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
            buckets[category].append(_related_item(root, other, edge, direction=direction))
    for category in CONTEXT_KEYS:
        unique = {
            stable_json({key: item.get(key) for key in ("id", "relation", "direction", "location")}): item
            for item in buckets[category]
        }
        buckets[category] = sorted(unique.values(), key=lambda item: (item["name"], item["relation"], item["id"]))
    return buckets


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
    records = load_index(root)
    meta, files, symbols, edges = partition_index(records)
    indexed_file = next((item for item in files if item.get("path") == recovered.get("file")), {})
    source_hash_matches = indexed_file.get("hash") == location["content_sha256"]
    semantic_relationships = bool(meta.get("semantic") and not recovered.get("degraded") and source_hash_matches)
    context, omitted = _bound_context(_collect_context(root, recovered, symbols, edges), max_items)
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
            "index_sha256": sha256_file(root / ".ctxpp/index.jsonl"),
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
    base_sufficient = bool(coverage.get("target_complete") and semantic_relationships)

    def finalize_shape() -> int:
        packet["expansions"] = [_handle("canonical-source", target_payload["id"], ["expand", target_payload["id"]])]
        if any(packet["coverage"]["omitted"].values()):
            packet["expansions"].append(_handle(
                "more-context", target_payload["id"],
                ["packet", target_payload["id"], "--intent", intent,
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
    packet["coverage"]["budget_exceeded"] = estimated > budget
    packet["coverage"]["sufficient"] = (
        base_sufficient
        and not packet["coverage"]["budget_exceeded"]
        and not any(packet["coverage"]["omitted"].values())
    )
    packet["estimated_tokens"] = estimated
    packet["packet_hash"] = sha256_bytes(stable_json(packet).encode())
    return packet


def render_inspect(packet: dict[str, Any]) -> str:
    target = packet["target"]
    location = target["location"]
    lines = [
        f"{target['name']} [{target['kind']}]",
        f"edit {location['path']}:{location['line']}-{location['end_line']} bytes={location['byte_start']}:{location['byte_end']}",
        f"trust target={packet['trust']['target_range']} relationships={packet['trust']['relationships']} source=canonical",
    ]
    for key in CONTEXT_KEYS:
        values = packet["context"][key]
        if values:
            lines.append(f"{key}: " + ", ".join(item["name"] for item in values))
    if packet["invariants"]:
        lines.append("invariants: " + "; ".join(f"{item['kind']}:{item['text']}" for item in packet["invariants"]))
    omitted = sum(packet["coverage"]["omitted"].values())
    lines.append(f"packet sufficient={1 if packet['coverage']['sufficient'] else 0} tokens~={packet['estimated_tokens']} omitted={omitted}")
    for expansion in packet["expansions"]:
        lines.append("expand: ctxpp " + " ".join(expansion["argv"]))
    return "\n".join(lines) + "\n"
