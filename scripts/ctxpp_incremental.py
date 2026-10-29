"""Localized semantic TU and uncompiled-file refresh over the v2 index."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import ctxpp_store
from ctxpp_recipe import (infer as infer_recipe, observed_records, persist_successful,
                          preflight as recipe_preflight, successful_records,
                          translate as translate_recipe)
from ctxpp_runtime import ResourceScheduler, run_process_measured, count


def scan_lexical_files(root: Path, cfg: dict[str, Any], scoped: tuple[str, ...]) -> dict[str, Any] | None:
    """Replace explicitly scoped, uncompiled source files without reading TU payloads.

    Complete dependency provenance and unchanged recipe/configuration identity
    establish that none of these files belongs to the semantic graph. Headers,
    directory scopes, usable recipes and ambiguous historical rows fall back to
    the normal scan rather than silently publishing lexical semantic changes.
    """
    import ctxpp_lib as lib

    root = root.resolve()
    selected: set[str] = set()
    for raw in scoped:
        path = Path(raw)
        path = (path if path.is_absolute() else root / path).resolve()
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            return None
        if path.suffix not in (".cc", ".cpp", ".cxx", ".cu") or path.is_dir() or lib.is_excluded(rel, cfg):
            return None
        selected.add(rel)
    if not selected:
        return None
    store = ctxpp_store.open_store(root)
    if store is None:
        return None
    try:
        meta = store.meta()
        if (not store.has_incremental_provenance() or meta.get("format") != "CTXPP-INDEX/1"
                or meta.get("root") != str(root)
                or meta.get("config_hash") != lib.sha256_bytes(lib.stable_json(cfg).encode())):
            return None
        state = store.metadata("tu_state", {})
        jobs = state.get("jobs", {})
        if state.get("format") != "CTXPP-TU-CACHE/1" or not jobs:
            return None
        for key, job in jobs.items():
            dependencies = job.get("dependencies", {})
            if (not dependencies or not job.get("input_fingerprint")
                    or key.split("\0", 1)[0] in selected or selected.intersection(dependencies)):
                return None
        config_path = root / ".ctxpp.toml"
        config_hash = lib.sha256_file(config_path) if config_path.is_file() else None
        compdb_path = lib.find_compdb(root, cfg)
        determinants = meta.get("determinants", {})
        compdb_hash = lib.sha256_file(compdb_path) if compdb_path and compdb_path.is_file() else None
        if determinants.get("compilation_database_hash") != compdb_hash:
            return None
        recipes = [*lib.read_compdb(compdb_path, root), *observed_records(root), *successful_records(root)]
        by_file = lib.compdb_by_file(recipes)
        compiler_hashes = sorted({lib.sha256_bytes(lib.stable_json(command).encode())
                                  for commands in by_file.values() for command in commands})
        if determinants.get("compiler_hashes") != compiler_hashes:
            return None
        for rel in selected:
            path = root / rel
            if path in by_file:
                return None
            if path.is_file():
                inferred, _, _ = infer_recipe(path, recipes)
                if inferred is not None:
                    usable, diagnostic = recipe_preflight(translate_recipe(inferred, path), path)
                    # Missing optional driver leaves this unowned file lexical.
                    # Recipe/translation errors must retain normal diagnostics.
                    if usable or diagnostic != "Clang driver unavailable":
                        return None
        prior_files = {rel: store.file(rel) for rel in selected}
        if any(record and (record.get("translation_units") or record.get("commands")
                          or record.get("command_hashes")) for record in prior_files.values()):
            return None
        # Query the file index directly. File.symbols is metadata, not authority
        # for deciding which representations need replacing (IDs may repeat).
        prior_rows = []
        for rel in sorted(selected):
            prior_rows.extend(store._symbol(row) for row in store.connection.execute(
                'SELECT * FROM symbols WHERE file=? ORDER BY symbol_order', (rel,)))
            if store.connection.execute('SELECT 1 FROM edges WHERE file_id='
                    '(SELECT id FROM strings WHERE value=?) LIMIT 1', (rel,)).fetchone():
                return None
        if any(not row.get("degraded") or any(occurrence.get("translation_unit")
                for occurrence in row.get("occurrences", [])) for row in prior_rows):
            return None
        impacted_ids = {str(row["id"]) for row in prior_rows}
        for symbol_id in impacted_ids:
            if store.connection.execute('SELECT 1 FROM edges WHERE from_id='
                    '(SELECT id FROM strings WHERE value=?) OR to_id='
                    '(SELECT id FROM strings WHERE value=?) LIMIT 1', (symbol_id, symbol_id)).fetchone():
                return None
        freshness = store.freshness()
        manifest = store.manifest()
        readiness = store.readiness()
        generation = store.artifact_identity
        counts_before = store.counts()
        digests = {rel: lib.sha256_file(root / rel) if (root / rel).is_file() else None for rel in selected}
        changed = {rel for rel in selected if (prior_files[rel] or {}).get("hash") != digests[rel]
                   or (prior_files[rel] is not None and digests[rel] is None)}
        result = {"backend": meta.get("backend", "degraded-text-routing"),
                  "files": counts_before[0], "symbols": counts_before[1], "edges": counts_before[2],
                  "failures": len(meta.get("failures", [])), "cache_hits": 0,
                  "index": str(ctxpp_store.database_path(root)),
                  "incremental": {"translation_units": 0, "parsed": 0, "lexical_files": len(changed),
                                  "symbols_replaced": 0, "store_rows_decoded": len(prior_rows)}}
        if not changed:
            count("lexical_file_scan_noops")
            return result
        old_rows = [row for row in prior_rows if row.get("file") in changed]
        new_rows = lib.lexical_symbols(root, [root / rel for rel in sorted(changed) if digests[rel] is not None], [])
        impacted_ids = {str(row["id"]) for row in [*old_rows, *new_rows]}
        # apply_delta deletes by semantic ID; retain every other representation
        # even in a historical artifact with colliding IDs in different files.
        replacements = store.symbol_records(impacted_ids)
        preserved = [row for row in replacements if row.get("file") not in changed]
        desired = [*preserved, *new_rows]
        store.close()
        store = None
        tokenizer = lib.Tokenizer(root, str(cfg.get("tokenizer", "auto")), defer_writes=True)
        files = []
        manifest_files = dict(manifest.get("files", {}))
        freshness_files = dict(freshness.get("files", {}))
        removed = set()
        for rel in sorted(changed):
            if digests[rel] is None:
                removed.add(rel)
                manifest_files.pop(rel, None)
                freshness_files.pop(rel, None)
                continue
            path = root / rel
            data = path.read_bytes()
            if lib.sha256_bytes(data) != digests[rel]:
                raise lib.CtxppError(f"source changed during lexical scan: {rel}; retry")
            tokens = tokenizer.count(data.decode("utf-8", errors="replace"))
            stat = path.stat()
            files.append({"record": "file", "path": rel, "hash": digests[rel],
                "command_hashes": [], "standards": ["unknown"], "commands": [],
                "tokens": tokens.count, "token_exact": tokens.exact, "tokenizer": tokens.identity,
                "bytes": len(data), "lines": lib.line_count(data), "classification": lib.classify_file(rel),
                "translation_units": [], "parse_status": "unobserved", "diagnostics": [],
                "symbols": sorted(str(row["id"]) for row in new_rows if row.get("file") == rel),
                "includes": [], "route": None})
            manifest_files[rel] = digests[rel]
            freshness_files[rel] = {"hash": digests[rel], "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        coverage = dict(meta.get("coverage", {}))
        coverage["lexical_only"] = max(0, int(coverage.get("lexical_only", 0)) - len(old_rows) + len(new_rows))
        next_meta = {**meta, "coverage": coverage,
                     "incomplete": not meta.get("semantic") or bool(meta.get("failures")) or bool(coverage["lexical_only"])}
        next_readiness = {**readiness, "rewrite_ready": not readiness.get("semantic_stale_tus")
                          and not meta.get("failures") and not coverage["lexical_only"] and bool(meta.get("semantic"))}
        with lib.publication_lock(root):
            for rel, expected in digests.items():
                actual = lib.sha256_file(root / rel) if (root / rel).is_file() else None
                if actual != expected:
                    raise lib.CtxppError(f"source changed during lexical scan: {rel}; retry")
            if (lib.sha256_file(config_path) if config_path.is_file() else None) != config_hash:
                raise lib.CtxppError("configuration changed during lexical scan; retry")
            if (lib.sha256_file(compdb_path) if compdb_path and compdb_path.is_file() else None) != compdb_hash:
                raise lib.CtxppError("compilation database changed during lexical scan; retry")
            current_recipes = [*lib.read_compdb(compdb_path, root), *observed_records(root), *successful_records(root)]
            if lib.stable_json(current_recipes) != lib.stable_json(recipes):
                raise lib.CtxppError("compilation recipes changed during lexical scan; retry")
            ctxpp_store.apply_delta(root, affected_tus=set(), impacted_symbol_ids=impacted_ids,
                symbols=desired, edges=[], files=files, meta=next_meta,
                manifest={**manifest, "files": dict(sorted(manifest_files.items()))},
                freshness={**freshness, "files": dict(sorted(freshness_files.items())), "config_file_hash": config_hash},
                tu_state=state, readiness=next_readiness, removed_files=removed, expected_generation=generation)
            lib.refresh_lexical_overlay(root, [root / rel for rel in sorted(changed)], new_rows)
        tokenizer.flush()
        count("lexical_file_delta_scans")
        count("files_lexically_refreshed", len(changed))
        result.update(files=counts_before[0] - sum(prior_files[rel] is not None for rel in removed)
                      + sum(prior_files[rel] is None for rel in changed - removed),
                      symbols=counts_before[1] - len(old_rows) + len(new_rows))
        result["incremental"].update(symbols_replaced=len(impacted_ids), store_rows_decoded=len(prior_rows) + len(replacements))
        return result
    finally:
        if store is not None:
            store.close()


def scan_incremental(root: Path, cfg: dict[str, Any], skill_root: Path,
                     refresh_tus: set[str]) -> dict[str, Any] | None:
    """Refresh selected translation units without decoding the complete graph.

    Return ``None`` only when the existing generation lacks the provenance
    required to preserve untouched symbol contributions safely.
    """
    import ctxpp_lib as lib
    ParseExecution = lib.ParseExecution

    root = root.resolve()
    count("incremental_scan_attempts")
    requested = {Path(str(item)).as_posix() for item in refresh_tus}
    store = ctxpp_store.open_store(root)
    if store is None:
        return None
    try:
        if not store.has_incremental_provenance():
            count("incremental_provenance_unavailable")
            store.close()
            return None
        meta = store.meta()
        if (meta.get("format") != "CTXPP-INDEX/1" or meta.get("root") != str(root)
                or meta.get("config_hash") != lib.sha256_bytes(lib.stable_json(cfg).encode())):
            count("incremental_identity_fallbacks")
            raise lib.CtxppError("localized scan identity changed; run a complete ctxpp scan")
        prior_state = store.metadata("tu_state", {})
        if prior_state.get("format") != "CTXPP-TU-CACHE/1":
            count("incremental_state_fallbacks")
            raise lib.CtxppError("localized scan TU state is incompatible; run a complete ctxpp scan")
        prior_jobs = prior_state.get("jobs", {})
        prior_files = {str(item.get("path", "")): item for item in store.files()}
        prior_manifest = store.manifest()
        prior_freshness = store.freshness()
        prior_readiness = store.readiness()
        generation = store.artifact_identity
        old_rows_for_tus = store.symbol_records_for_tus(requested)
        counts_before = store.counts()
        old_meta = meta
    except BaseException:
        store.close()
        raise

    compdb_path = lib.find_compdb(root, cfg)
    compdb = lib.read_compdb(compdb_path, root)
    observed = observed_records(root)
    saved = successful_records(root)
    by_file = lib.compdb_by_file([*compdb, *observed, *saved])
    known_tus = {key.split("\0", 1)[0] for key in prior_jobs}
    core = lib.find_core(skill_root)
    if core is None:
        store.close()
        raise lib.CtxppError(
            "native parser unavailable for localized scan; existing semantic index is unchanged. "
            "Run ctxpp init or scripts/build_tool.sh to restore it, then retry."
        )
    core_hash = lib.sha256_file(core)
    determinants = old_meta.get("determinants", {})
    if determinants.get("core_hash") and determinants["core_hash"] != core_hash:
        store.close()
        raise lib.CtxppError("semantic core changed; run a complete ctxpp scan")
    core_version = str(old_meta.get("core_version", "unavailable"))
    if core_version == "unavailable":
        store.close()
        raise lib.CtxppError("semantic core version unavailable for localized scan")

    cache_dir = lib.index_dir(root) / "cache/tu"
    hash_cache: dict[str, str | None] = {}

    def current_hash(rel: str) -> str | None:
        if rel not in hash_cache:
            path = root / rel
            hash_cache[rel] = lib.sha256_file(path) if path.is_file() else None
        return hash_cache[rel]

    recipe_files: dict[Path, list[tuple[dict[str, Any], str, float]]] = {}
    for file, commands in by_file.items():
        try:
            rel = file.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        if rel in requested:
            recipe_files[file] = [(record, str(record.get("origin", "compile_database")), 1.0)
                                  for record in commands]
    available = set()
    for file in recipe_files:
        try:
            available.add(file.resolve().relative_to(root).as_posix())
        except ValueError:
            pass
    inferred_tus = (requested - available) - known_tus
    if inferred_tus:
        templates = [*compdb, *observed, *saved]
        for rel in sorted(inferred_tus):
            file = root / rel
            if not file.is_file() or lib.is_excluded(rel, cfg):
                continue
            record, origin, confidence = infer_recipe(file, templates)
            if record:
                recipe_files[file] = [(record, origin, confidence)]

    jobs: list[dict[str, Any]] = []
    command_facts: dict[str, list[dict[str, Any]]] = {}
    for file, recipes in sorted(recipe_files.items(), key=lambda item: str(item[0])):
        rel = file.resolve().relative_to(root).as_posix()
        if rel not in requested or lib.is_excluded(rel, cfg) or file.suffix not in lib.SOURCE_EXTENSIONS:
            continue
        for number, (record, origin, confidence) in enumerate(recipes):
            translated = translate_recipe(record, file)
            args = translated["clang_argv"]
            command_hash = lib.sha256_bytes(lib.stable_json({"directory": record.get("directory"), "args": args}).encode())
            command_facts.setdefault(rel, []).append({"hash": command_hash, "standard": lib.language_standard(args),
                "args": args, "directory": record.get("directory", str(root)), "origin": origin,
                "confidence": confidence, "source_rewrite_usable": origin in ("compile_database", "observed_standalone")})
            state_key = f"{rel}\0{number}"
            prior = prior_jobs.get(state_key, {})
            source_hash = current_hash(rel)
            input_fingerprint = lib.sha256_bytes(lib.stable_json({"file": source_hash, "command": command_hash,
                "core": core_version, "core_hash": core_hash, "tool": lib.VERSION}).encode())
            cache_key = lib.sha256_bytes(lib.stable_json({"tu": rel, "configuration": number, "command": command_hash,
                "core": core_version, "core_hash": core_hash, "tool": lib.VERSION}).encode())
            write_cache = cache_dir / f"{cache_key}-{input_fingerprint[:16]}.jsonl.z"
            read_cache = write_cache
            prior_name = prior.get("cache")
            if isinstance(prior_name, str) and Path(prior_name).name == prior_name and (cache_dir / prior_name).is_file():
                read_cache = cache_dir / prior_name
            elif not read_cache.is_file() and (cache_dir / f"{cache_key}.jsonl").is_file():
                read_cache = cache_dir / f"{cache_key}.jsonl"
            jobs.append({"file": file, "rel": rel, "number": number, "record": record, "args": args,
                "translated": translated, "origin": origin, "confidence": confidence,
                "command_hash": command_hash, "input_fingerprint": input_fingerprint, "source_hash": source_hash,
                "cache_key": cache_key, "state_key": state_key, "prior": prior,
                "cache": read_cache, "cache_write": write_cache,
                "history_key": lib.sha256_bytes(f"parse\0{rel}\0{command_hash}".encode()),
                "size": file.stat().st_size, "cuda": file.suffix in (".cu", ".cuh"),
                "memory_floor_mb": 1024 if file.suffix in (".cu", ".cuh") else 256,
                "needs_preflight": Path(str(translated.get("compiler_identity", ""))).name == "nvcc"
                    or origin.startswith("inferred")})

    job_tus = {job["rel"] for job in jobs}
    deleted_tus = requested - job_tus
    if deleted_tus - known_tus:
        store.close()
        raise lib.CtxppError("localized scan has no complete TU recipe; run a complete ctxpp scan")
    if not jobs and not deleted_tus:
        store.close()
        raise lib.CtxppError("localized scan has no selected translation units")
    dependencies_current = lambda job: bool(job["prior"].get("dependencies")) and all(
        current_hash(rel) == digest for rel, digest in job["prior"].get("dependencies", {}).items())
    pending, outputs = [], {}
    cache_hits = 0
    for job in jobs:
        reusable = (job["prior"].get("input_fingerprint") == job["input_fingerprint"]
                    and dependencies_current(job) and job["cache"].is_file())
        if reusable:
            outputs[job["state_key"]] = lib._read_tu_cache(job["cache"])
            cache_hits += 1
        else:
            usable, error = recipe_preflight(job["translated"], job["file"]) if job["needs_preflight"] else (True, "")
            if not usable:
                store.close()
                raise lib.CtxppError(error or f"translation unit preflight failed: {job['rel']}")
            pending.append(job)

    def execute(job):
        proc = run_process_measured([str(core), "scan", "--root", str(root), "--file", str(job["file"]), "--", *job["args"]],
                                    cwd=Path(job["record"].get("directory", root)))
        return ParseExecution(job, proc.completed, proc.peak_memory_mb)

    configured_ceiling = cfg.get("tool", {}).get("max_workers")
    scheduler = ResourceScheduler(root, int(configured_ceiling) if isinstance(configured_ceiling, int)
                                  and configured_ceiling > 0 else None)
    completed = scheduler.run(pending, execute)
    next_jobs = {key: dict(value) for key, value in prior_jobs.items()
                 if key.split("\0", 1)[0] not in deleted_tus}
    parsed_tus = set()
    changed_paths: set[str] = set()
    raw_core: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for execution in completed:
        job, proc = execution.job, execution.completed
        if proc.returncode:
            store.close()
            raise lib.CtxppError(f"semantic scan failed for {job['rel']}: {proc.stderr.strip()}")
        if current_hash(job["rel"]) != job["source_hash"]:
            store.close()
            raise lib.CtxppError(f"source changed during semantic scan: {job['rel']}; retry")
        dependencies = {job["rel"]: str(job["source_hash"])}
        for line in proc.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            candidate = str(item.get("file", ""))
            if candidate and (root / candidate).is_file() and not lib.is_excluded(candidate, cfg):
                dependencies[candidate] = str(current_hash(candidate))
            include = str(item.get("to", "")) if item.get("record") == "include" else ""
            if include and (root / include).is_file() and not lib.is_excluded(include, cfg):
                dependencies[include] = str(current_hash(include))
        if any(current_hash(rel) != digest for rel, digest in dependencies.items()):
            store.close()
            raise lib.CtxppError(f"dependency changed during semantic scan: {job['rel']}; retry")
        payload_bytes = proc.stdout.encode()
        job["cache_write"] = lib._tu_cache_path(cache_dir, job["cache_key"], payload_bytes)
        lib._write_tu_cache(job["cache_write"], payload_bytes)
        persist_successful(root, job["record"], job["file"], job["origin"], lib.atomic_write)
        next_jobs[job["state_key"]] = {"cache": job["cache_write"].name,
            "input_fingerprint": job["input_fingerprint"], "command_hash": job["command_hash"],
            "dependencies": dict(sorted(dependencies.items()))}
        outputs[job["state_key"]] = proc.stdout
        parsed_tus.add(job["rel"])
        for rel, digest in dependencies.items():
            if prior_files.get(rel, {}).get("hash") != digest:
                changed_paths.add(rel)
        count("tus_parsed")
        count("asts_constructed")
        count("semantic_records_updated", len(proc.stdout.splitlines()))

    state_payload = {"format": "CTXPP-TU-CACHE/1", "jobs": dict(sorted(next_jobs.items()))}
    for job in jobs:
        if job["state_key"] not in outputs:
            store.close()
            raise lib.CtxppError(f"translation unit has no validated cache payload: {job['rel']}")
        for line in outputs[job["state_key"]].splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                store.close()
                raise lib.CtxppError(f"invalid core JSON for {job['rel']}: {exc}") from exc
            item["translation_unit"] = job["rel"]
            item["configuration_hash"] = job["command_hash"]
            raw_core.append(item)
            if item.get("record") == "observation":
                observations.append(item)
    raw_symbols = [item for item in raw_core if item.get("record") == "symbol"]
    raw_edges = [item for item in raw_core if item.get("record") == "edge"]
    affected_tus = set(requested)

    old_file_tus = {rel for rel, item in prior_files.items()
                    if affected_tus.intersection(str(tu) for tu in item.get("translation_units", []))}
    new_tus_by_file: dict[str, set[str]] = {}
    new_includes_by_file: dict[str, set[str]] = {}
    for item in raw_core:
        rel = str(item.get("file", ""))
        if rel:
            new_tus_by_file.setdefault(rel, set()).add(str(item.get("translation_unit", "")))
        if item.get("record") == "include" and item.get("to"):
            source_rel = str(item.get("from", ""))
            target_rel = str(item["to"])
            if source_rel:
                new_includes_by_file.setdefault(source_rel, set()).add(target_rel)
                new_tus_by_file.setdefault(source_rel, set()).add(str(item.get("translation_unit", "")))
            new_tus_by_file.setdefault(target_rel, set()).add(str(item.get("translation_unit", "")))

    metadata_files = old_file_tus | set(new_tus_by_file) | affected_tus
    # Only source files whose lexical declarations may have changed need their
    # lexical rows refreshed. TU membership changes across included headers are
    # file metadata updates and must not fan out into decoding every symbol in
    # those headers.
    lexical_files = set(affected_tus) | {rel for rel in changed_paths if (root / rel).is_file()}
    old_file_ids = {str(sid) for rel in lexical_files for sid in prior_files.get(rel, {}).get("symbols", [])}
    impacted_ids = old_file_ids | {str(row["id"]) for row in old_rows_for_tus} | {str(row["id"]) for row in raw_symbols}
    prior_rows_for_ids = store.symbol_records(impacted_ids)
    survivor_contributions = store.contribution_symbols(impacted_ids, affected_tus)

    config_order = {}
    for state_key, job in next_jobs.items():
        tu, separator, number = state_key.partition("\0")
        if separator and job.get("command_hash"):
            try:
                config_order[(tu, str(job["command_hash"]))] = int(number)
            except ValueError:
                continue
    merge_input = [*survivor_contributions, *raw_core]
    merge_input.sort(key=lambda item: (str(item.get("translation_unit", "")),
        config_order.get((str(item.get("translation_unit", "")), str(item.get("configuration_hash", ""))), 0)))
    merged_semantic, merged_edges, _ = lib._merge_core_records(merge_input)
    preserved_degraded = [row for row in prior_rows_for_ids
                          if row.get("degraded") and str(row.get("file", "")) not in lexical_files]
    lexical = lib.lexical_symbols(root, [root / rel for rel in sorted(lexical_files) if (root / rel).is_file()],
                                  merged_semantic)
    desired_symbols = [*merged_semantic, *preserved_degraded, *lexical]
    desired_symbols.sort(key=lambda row: (row.get("qualified_name", ""), row.get("file", ""),
                                           row.get("start", 0), row.get("id", "")))
    impacted_ids.update(str(row["id"]) for row in desired_symbols)
    metadata_files.update(str(row["file"]) for row in desired_symbols if row.get("file"))
    callable_kinds = {"FunctionDecl", "CXXMethod", "CXXConstructor", "CXXDestructor",
                       "Constructor", "Destructor", "FunctionTemplate", "ConversionFunction"}
    replace_edge_files = {str(row.get("file", "")) for row in [*prior_rows_for_ids, *desired_symbols]
                          if row.get("kind") in callable_kinds and row.get("file")}
    replace_edge_files.update(str(edge.get("file", "")) for edge in raw_edges if edge.get("file"))
    old_file_edges = store.edges_for_files(replace_edge_files) if replace_edge_files else []
    edge_symbol_ids = {str(edge.get(key, "")) for edge in [*old_file_edges, *merged_edges]
                       for key in ("from", "to") if edge.get(key)}
    file_symbol_ids = {str(sid) for rel in replace_edge_files
                       for sid in prior_files.get(rel, {}).get("symbols", [])}
    indexed_edge_symbols = store.symbol_records(edge_symbol_ids | file_symbol_ids | impacted_ids)
    store.close()
    # Rebuild callable attribution only in files whose callable graph changed.
    # Existing derived rows are regenerated from their base edges to avoid
    # carrying stale test/nonlocal/template classifications forward.
    derived_edge_types = {"test_relationship", "template_dependency", "nonlocal_read", "nonlocal_write"}
    base_file_edges = [edge for edge in old_file_edges
                       if edge.get("translation_unit") not in affected_tus
                       and edge.get("type") not in derived_edge_types]
    base_new_edges = [edge for edge in merged_edges if edge.get("type") not in derived_edge_types]
    file_base_edges = [*base_file_edges, *(edge for edge in base_new_edges
                                          if edge.get("file") in replace_edge_files)]
    other_new_edges = [edge for edge in base_new_edges if edge.get("file") not in replace_edge_files]
    current_edge_symbols = [row for row in indexed_edge_symbols if str(row.get("id", "")) not in impacted_ids]
    current_edge_symbols.extend(row for row in desired_symbols
                                if str(row.get("id", "")) in (edge_symbol_ids | file_symbol_ids | impacted_ids))
    if file_base_edges:
        _, file_base_edges, _ = lib._merge_core_records([*(dict(row) for row in current_edge_symbols), *file_base_edges])
    rebuilt_file_edges = lib.enrich_semantic_edges(root, current_edge_symbols, file_base_edges)
    rebuilt_other_edges = lib.enrich_semantic_edges(root, current_edge_symbols, other_new_edges)
    enriched_edges = list({lib.stable_json(edge): edge
                           for edge in [*rebuilt_file_edges, *rebuilt_other_edges]}.values())

    tokenizer = lib.Tokenizer(root, str(cfg.get("tokenizer", "auto")), defer_writes=True)
    file_records = []
    freshness = dict(prior_freshness)
    manifest = dict(prior_manifest)
    manifest_files = dict(prior_manifest.get("files", {}))
    facts_by_tu: dict[str, list[dict[str, Any]]] = {}
    for file, commands in by_file.items():
        try:
            rel = file.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        for record in commands:
            translated = translate_recipe(record, file)
            args = translated["clang_argv"]
            command_hash = lib.sha256_bytes(lib.stable_json({"directory": record.get("directory"), "args": args}).encode())
            facts_by_tu.setdefault(rel, []).append({"hash": command_hash, "standard": lib.language_standard(args),
                "args": args, "directory": record.get("directory", str(root)),
                "origin": str(record.get("origin", "compile_database")), "confidence": 1.0,
                "source_rewrite_usable": str(record.get("origin", "compile_database")) in ("compile_database", "observed_standalone")})
    symbol_ids_by_file: dict[str, list[str]] = {}
    for row in desired_symbols:
        if row.get("file"):
            symbol_ids_by_file.setdefault(str(row["file"]), []).append(str(row["id"]))
    observations_by_file: dict[str, list[str]] = {}
    for obs in observations:
        if obs.get("file"):
            observations_by_file.setdefault(str(obs["file"]), []).extend(obs.get("diagnostics", []))
    for rel in sorted(metadata_files):
        path = root / rel
        if not path.is_file() or lib.is_excluded(rel, cfg):
            continue
        prior = prior_files.get(rel, {})
        old_tus = {str(tu) for tu in prior.get("translation_units", [])}
        new_tus = (old_tus - affected_tus) | new_tus_by_file.get(rel, set())
        if rel in affected_tus:
            new_tus.add(rel)
        facts = sorted({lib.stable_json(fact): fact for tu in sorted(new_tus)
                        for fact in facts_by_tu.get(tu, [])}.values(), key=lib.stable_json)
        current_digest = current_hash(rel)
        if current_digest is None:
            current_digest = lib.sha256_file(path)
        data_changed = current_digest != prior.get("hash")
        if data_changed or not prior:
            data = path.read_bytes()
            digest = lib.sha256_bytes(data)
            text = data.decode("utf-8", errors="replace")
            token_count = tokenizer.count(text)
            token_fields = {"tokens": token_count.count, "token_exact": token_count.exact, "tokenizer": token_count.identity,
                            "bytes": len(data), "lines": lib.line_count(data), "hash": digest}
            changed_paths.add(rel)
        else:
            token_fields = {key: prior[key] for key in ("tokens", "token_exact", "tokenizer", "bytes", "lines", "hash") if key in prior}
            digest = str(prior["hash"])
        stale_diagnostics = [] if old_tus & affected_tus else list(prior.get("diagnostics", []))
        stale_diagnostics.extend(observations_by_file.get(rel, []))
        includes = (new_includes_by_file.get(rel, set()) if rel in affected_tus
                    else set(prior.get("includes", [])) | new_includes_by_file.get(rel, set()))
        symbols_here = [str(sid) for sid in prior.get("symbols", []) if str(sid) not in impacted_ids]
        symbols_here.extend(symbol_ids_by_file.get(rel, []))
        status = "unobserved" if not facts else ("error" if any("error:" in d.lower() for d in stale_diagnostics) else "ok")
        replacement = {"record": "file", **token_fields, "path": rel,
            "command_hashes": sorted({fact["hash"] for fact in facts}),
            "standards": sorted({fact["standard"] for fact in facts}) or ["unknown"],
            "commands": facts, "classification": prior.get("classification", lib.classify_file(rel)),
            "translation_units": sorted(new_tus), "parse_status": status, "diagnostics": stale_diagnostics,
            "symbols": sorted(symbols_here), "includes": sorted(includes), "route": None}
        file_records.append(replacement)
        manifest_files[rel] = digest
        freshness.setdefault("files", {})[rel] = {"hash": digest, "size": path.stat().st_size,
                                                     "mtime_ns": path.stat().st_mtime_ns}
    tokenizer.flush()

    semantic_before = {str(row["id"]) for row in prior_rows_for_ids if not row.get("degraded")}
    semantic_after = {str(row["id"]) for row in desired_symbols if not row.get("degraded")}
    lexical_before = sum(bool(row.get("degraded")) for row in prior_rows_for_ids)
    lexical_after = sum(bool(row.get("degraded")) for row in desired_symbols)
    coverage = dict(old_meta.get("coverage", {}))
    coverage["semantic"] = max(0, int(coverage.get("semantic", 0)) - len(semantic_before) + len(semantic_after))
    coverage["lexical_only"] = max(0, int(coverage.get("lexical_only", 0)) - lexical_before + lexical_after)
    backend = old_meta.get("backend", "degraded-text-routing")
    if coverage["semantic"] == 0:
        backend = "degraded-text-routing"
    next_meta = {**old_meta, "backend": backend, "semantic": backend != "degraded-text-routing",
                 "coverage": coverage, "incomplete": backend == "degraded-text-routing" or bool(old_meta.get("failures")) or bool(coverage["lexical_only"])}
    next_meta["compilation_database"] = str(compdb_path) if compdb_path else None
    next_meta["determinants"] = {**determinants,
        "compilation_database_hash": lib.sha256_file(compdb_path) if compdb_path and compdb_path.is_file() else None,
        "compiler_hashes": sorted({lib.sha256_bytes(lib.stable_json(command).encode())
                                    for commands in by_file.values() for command in commands})}
    freshness_files = dict(freshness.get("files", {}))
    removed_files = {rel for rel in metadata_files if not (root / rel).is_file()}
    for rel in removed_files:
        manifest_files.pop(rel, None)
        freshness_files.pop(rel, None)
    next_manifest = {**manifest, "files": dict(sorted(manifest_files.items()))}
    next_freshness = {**freshness, "files": dict(sorted(freshness_files.items()))}
    stale_tus = set(str(tu) for tu in prior_readiness.get("semantic_stale_tus", [])) - affected_tus
    for key, job in next_jobs.items():
        tu = key.split("\0", 1)[0]
        if tu in affected_tus:
            continue
        if any(rel in changed_paths and current_hash(rel) != digest for rel, digest in job.get("dependencies", {}).items()):
            stale_tus.add(tu)
    readiness = {**prior_readiness, "format": "CTXPP-READINESS/1", "semantic_stale_tus": sorted(stale_tus),
                 "route_ready": True, "read_ready": not stale_tus,
                 "rewrite_ready": not stale_tus and not next_meta.get("failures") and not coverage["lexical_only"]
                    and backend != "degraded-text-routing"}
    from ctxpp_lib import publication_lock
    with publication_lock(root):
        for job in jobs:
            for rel, digest in next_jobs[job["state_key"]].get("dependencies", {}).items():
                source = root / rel
                if not source.is_file() or lib.sha256_file(source) != digest:
                    raise lib.CtxppError(f"source changed during semantic scan: {job['rel']}; retry")
        ctxpp_store.apply_delta(root, affected_tus=affected_tus, impacted_symbol_ids=impacted_ids,
            symbols=desired_symbols, edges=enriched_edges, files=file_records, meta=next_meta,
            manifest=next_manifest, freshness=next_freshness, tu_state=state_payload,
            readiness=readiness, removed_files=removed_files, replace_edge_files=replace_edge_files,
            expected_generation=generation)
    lib.refresh_lexical_overlay(root, [root / rel for rel in sorted(lexical_files)], desired_symbols)

    # Match the established scan result keys while reporting the bounded work.
    updated = ctxpp_store.open_store(root)
    try:
        file_count, symbol_count, edge_count = updated.counts()
    finally:
        updated.close()
    result = {"backend": backend, "files": file_count, "symbols": symbol_count,
              "edges": max(0, edge_count), "failures": len(next_meta.get("failures", [])),
              "cache_hits": cache_hits, "index": str(lib.index_dir(root) / "index.sqlite"),
              "incremental": {"translation_units": len(affected_tus), "parsed": len(parsed_tus),
                              "symbols_replaced": len(impacted_ids), "store_rows_decoded": len(prior_rows_for_ids)}}
    return result
