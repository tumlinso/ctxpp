"""Canonical CTXPP-INDEX/2 storage. No compiler or workflow dependencies."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterable
import uuid
import zlib

from ctxpp_paths import state_dir

SCHEMA = 2
EDGE_FIELDS = ('type', 'from', 'to', 'file', 'translation_unit', 'kind', 'configuration_hash', 'via',
               'start', 'end', 'line', 'column', 'end_line', 'end_column')
EDGE_COLUMNS = ('type_id', 'from_id', 'to_id', 'file_id', 'tu_id', 'kind_id', 'config_id', 'via_id',
                'start', 'end', 'line', 'column', 'end_line', 'end_column')


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _pack(value: Any) -> bytes:
    return zlib.compress(_json(value).encode(), 6)


def unpack(value: bytes | None) -> Any:
    return json.loads(zlib.decompress(value)) if value is not None else {}


def database_path(root: Path, state_path: Path | None = None) -> Path:
    return (state_path if state_path is not None else state_dir(root)) / 'index.sqlite'


class IndexedStore:
    """One read transaction pins the generation while publication replaces the file."""
    def __init__(self, database: Path):
        self.connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute('BEGIN')
            version = self.connection.execute('PRAGMA user_version').fetchone()[0]
            if version != SCHEMA:
                raise ValueError(f'unsupported CTXPP SQLite schema {version}')
            self.artifact_identity = self.metadata('generation')
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def metadata(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def meta(self) -> dict[str, Any]:
        return self.metadata('index_meta', {})

    def manifest(self) -> dict[str, Any]:
        return self.metadata('manifest', {})

    def freshness(self) -> dict[str, Any]:
        return self.metadata('freshness', {})

    def readiness(self) -> dict[str, Any]:
        return self.metadata('readiness', {})

    def range_symbols(self, file: str, start: int, end: int, limit: int = 512):
        return [self._symbol(row) for row in self.connection.execute(
            'SELECT * FROM symbols WHERE file=? AND start_byte>=? AND end_byte<=? ORDER BY symbol_order LIMIT ?',
            (file, start, end, limit))]

    def _symbol(self, row) -> dict[str, Any]:
        from ctxpp_runtime import count
        count("query_rows")
        count("symbol_records_decoded")
        result = unpack(row['data'])
        if row['has_occurrences']:
            result['occurrences'] = [unpack(item[0]) for item in self.connection.execute(
                'SELECT data FROM occurrences WHERE symbol_row_id=? ORDER BY occurrence_order', (row['symbol_order'],))]
        return result

    def exact(self, target: str, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute('SELECT * FROM symbols WHERE id=? OR qualified_name=? OR name=? '
            'ORDER BY (qualified_name<>?),definition DESC,file,symbol_order LIMIT ?',
            (target, target, target, target, max(0, limit)))
        return [self._symbol(row) for row in rows]

    def location(self, file: str, line: int, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute('SELECT * FROM symbols WHERE file=? AND line<=? AND end_line>=? '
            'ORDER BY (end_byte-start_byte),qualified_name LIMIT ?', (file, line, line, max(0, limit)))
        return [self._symbol(row) for row in rows]

    def candidates(self, terms: list[str], limit: int = 512) -> list[dict[str, Any]]:
        if not terms:
            return []
        # Bound decoded records even for broad terms; exact lookups run first.
        terms = terms[:32]
        clauses = ' OR '.join('instr(search_text,?)>0' for _ in terms)
        relevance = '+'.join('(instr(search_text,?)>0)' for _ in terms)
        rows = self.connection.execute(f'SELECT * FROM symbols WHERE {clauses} '
            f'ORDER BY ({relevance}) DESC,symbol_order LIMIT ?', (*terms, *terms, max(0, limit)))
        return [self._symbol(row) for row in rows]

    def symbol_ids(self, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        result = {}
        values = sorted(set(ids))
        for offset in range(0, len(values), 400):
            batch = values[offset:offset + 400]
            marks = ','.join('?' for _ in batch)
            for row in self.connection.execute(f'SELECT * FROM symbols WHERE id IN ({marks})', batch):
                result[row['id']] = self._symbol(row)
        return result

    def _edge(self, row, strings: dict[int, str] | None = None) -> dict[str, Any]:
        from ctxpp_runtime import count
        count("query_rows")
        count("edge_records_decoded")
        result = {'record': 'edge', **unpack(row['data'])}
        for number, (key, column) in enumerate(zip(EDGE_FIELDS, EDGE_COLUMNS)):
            if row['fields'] & (1 << number):
                value = row[column]
                if number < 8 and value is not None:
                    if strings is None:
                        value = self.connection.execute('SELECT value FROM strings WHERE id=?', (value,)).fetchone()[0]
                    else:
                        value = strings[value]
                result[key] = value
        return result

    def edges_for(self, symbol_id: str, limit: int = 1024) -> list[dict[str, Any]]:
        row = self.connection.execute('SELECT id FROM strings WHERE value=?', (symbol_id,)).fetchone()
        if not row:
            return []
        rows = self.connection.execute('SELECT * FROM edges WHERE from_id=? OR to_id=? ORDER BY edge_order LIMIT ?',
                                       (row[0], row[0], max(0, limit)))
        return [self._edge(row) for row in rows]

    def edges_for_files(self, files: Iterable[str]) -> list[dict[str, Any]]:
        result = []
        values = sorted(set(files))
        for offset in range(0, len(values), 400):
            batch = values[offset:offset + 400]
            marks = ','.join('?' for _ in batch)
            rows = self.connection.execute(f'SELECT * FROM edges WHERE file_id IN '
                f'(SELECT id FROM strings WHERE value IN ({marks})) ORDER BY edge_order', batch)
            result.extend(self._edge(row) for row in rows)
        return result

    def test_symbols(self, limit: int = 256) -> list[dict[str, Any]]:
        return [self._symbol(row) for row in self.connection.execute(
            'SELECT * FROM symbols WHERE is_test=1 ORDER BY symbol_order LIMIT ?', (max(0, limit),))]

    def related_tests(self, name: str, limit: int = 257) -> list[dict[str, Any]]:
        return [self._symbol(row) for row in self.connection.execute(
            'SELECT * FROM symbols WHERE is_test=1 AND instr(search_text,?)>0 ORDER BY symbol_order LIMIT ?',
            (name.lower(), max(0, limit)))]

    def file(self, path: str) -> dict[str, Any] | None:
        row = self.connection.execute('SELECT data FROM files WHERE path=?', (path,)).fetchone()
        return unpack(row[0]) if row else None

    def files(self) -> list[dict[str, Any]]:
        return [unpack(row[0]) for row in self.connection.execute('SELECT data FROM files ORDER BY file_order')]

    def counts(self) -> tuple[int, int, int]:
        return tuple(self.connection.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                     for name in ('files', 'symbols', 'edges'))

    def has_incremental_provenance(self) -> bool:
        return bool(self.metadata("incremental_provenance", False))

    def symbol_records(self, ids: Iterable[str]) -> list[dict[str, Any]]:
        values = sorted(set(ids))
        result = []
        for offset in range(0, len(values), 400):
            batch = values[offset:offset + 400]
            marks = ','.join('?' for _ in batch)
            result.extend(self._symbol(row) for row in self.connection.execute(
                f'SELECT * FROM symbols WHERE id IN ({marks}) ORDER BY symbol_order', batch))
        return result

    def symbol_records_for_tus(self, tus: Iterable[str]) -> list[dict[str, Any]]:
        values = sorted(set(tus))
        result = []
        for offset in range(0, len(values), 400):
            batch = values[offset:offset + 400]
            marks = ','.join('?' for _ in batch)
            result.extend(self._symbol(row) for row in self.connection.execute(
                f'SELECT DISTINCT s.* FROM symbols s JOIN occurrences o ON o.symbol_row_id=s.symbol_order '
                f'WHERE o.translation_unit IN ({marks}) ORDER BY s.symbol_order', batch))
        return result

    def contribution_symbols(self, ids: Iterable[str], exclude_tus: Iterable[str] = ()) -> list[dict[str, Any]]:
        values, excluded, result = sorted(set(ids)), set(exclude_tus), []
        for offset in range(0, len(values), 400):
            batch = values[offset:offset + 400]
            marks = ','.join('?' for _ in batch)
            for row in self.connection.execute(
                f'SELECT translation_unit,configuration_hash,data FROM provenance_symbols '
                f'WHERE symbol_id IN ({marks}) ORDER BY translation_unit,cache_key,record_order', batch):
                if row[0] not in excluded:
                    result.append({**unpack(row[2]), "translation_unit": row[0], "configuration_hash": row[1]})
        return result

    def all_symbols_edges(self):
        return ([self._symbol(row) for row in self.connection.execute('SELECT * FROM symbols ORDER BY symbol_order')],
                [self._edge(row) for row in self.connection.execute('SELECT * FROM edges ORDER BY edge_order')])

    def iter_records(self):
        yield self.meta()
        for row in self.connection.execute('SELECT data FROM files ORDER BY file_order'):
            yield unpack(row[0])
        for row in self.connection.execute('SELECT * FROM symbols ORDER BY symbol_order'):
            yield self._symbol(row)
        # A bounded dictionary is far cheaper than repeating every USR/path in every edge.
        strings = dict(self.connection.execute('SELECT id,value FROM strings'))
        for row in self.connection.execute('SELECT * FROM edges ORDER BY edge_order'):
            yield self._edge(row, strings)
        for row in self.connection.execute('SELECT data FROM residual_records ORDER BY record_order'):
            yield unpack(row[0])


def open_store(root: Path, state_path: Path | None = None) -> IndexedStore | None:
    path = database_path(root, state_path)
    if not path.is_file():
        return None
    store = IndexedStore(path)
    try:
        if Path(str(store.metadata('source_root', ''))).resolve() != root.resolve():
            raise ValueError('index belongs to a different source repository')
        return store
    except BaseException:
        store.close()
        raise


_DDL = '''
PRAGMA user_version=2;
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE strings(id INTEGER PRIMARY KEY,value TEXT NOT NULL UNIQUE);
CREATE TABLE files(path TEXT PRIMARY KEY,file_order INTEGER NOT NULL,data BLOB NOT NULL);
CREATE TABLE symbols(id TEXT NOT NULL,name TEXT,qualified_name TEXT,kind TEXT,file TEXT,line INTEGER,end_line INTEGER,
 start_byte INTEGER,end_byte INTEGER,definition INTEGER,is_test INTEGER,search_text TEXT,symbol_order INTEGER PRIMARY KEY,
 has_occurrences INTEGER NOT NULL,data BLOB NOT NULL);
CREATE TABLE occurrences(symbol_row_id INTEGER NOT NULL,occurrence_order INTEGER NOT NULL,translation_unit TEXT,data BLOB NOT NULL,
 PRIMARY KEY(symbol_row_id,occurrence_order));
CREATE TABLE edges(edge_order INTEGER PRIMARY KEY,from_id INTEGER,to_id INTEGER,file_id INTEGER,tu_id INTEGER,type_id INTEGER,
 kind_id INTEGER,config_id INTEGER,via_id INTEGER,start INTEGER,end INTEGER,line INTEGER,column INTEGER,end_line INTEGER,
 end_column INTEGER,fields INTEGER NOT NULL,data BLOB);
CREATE TABLE tu_contributions(cache_key TEXT PRIMARY KEY,state_key TEXT NOT NULL,input_fingerprint TEXT NOT NULL,
 dependencies BLOB NOT NULL,cache_sha256 TEXT NOT NULL,payload BLOB NOT NULL);
CREATE TABLE provenance_symbols(cache_key TEXT NOT NULL,record_order INTEGER NOT NULL,symbol_id TEXT NOT NULL,
 translation_unit TEXT NOT NULL,configuration_hash TEXT,data BLOB NOT NULL,PRIMARY KEY(cache_key,record_order));
CREATE INDEX provenance_symbols_id ON provenance_symbols(symbol_id);
CREATE INDEX provenance_symbols_tu ON provenance_symbols(translation_unit);
CREATE TABLE residual_records(record_order INTEGER PRIMARY KEY,data BLOB NOT NULL);
CREATE INDEX symbols_id ON symbols(id); CREATE INDEX symbols_name ON symbols(name); CREATE INDEX symbols_qname ON symbols(qualified_name);
CREATE INDEX symbols_file_line ON symbols(file,line,end_line);
CREATE INDEX edges_from ON edges(from_id); CREATE INDEX edges_to ON edges(to_id);
CREATE INDEX edges_tu ON edges(tu_id); CREATE INDEX edges_file ON edges(file_id); CREATE INDEX occurrences_tu ON occurrences(translation_unit);
'''


def _fsync_directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(_json(payload) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _configuration_hash(connection, cache_key: str, state_key: str, job: dict[str, Any], meta: dict[str, Any]):
    if job.get("command_hash"):
        return job["command_hash"]
    tu, _, number = state_key.partition("\0")
    row = connection.execute('SELECT data FROM files WHERE path=?', (tu,)).fetchone()
    facts = unpack(row[0]).get("commands", []) if row else []
    cache_stem = cache_key.removesuffix('.z').removesuffix('.jsonl')
    for fact in facts:
        key = {"tu": tu, "configuration": int(number or 0), "command": fact.get("hash"),
               "core": meta.get("core_version"), "core_hash": meta.get("determinants", {}).get("core_hash", ""),
               "tool": meta.get("tool_version")}
        if hashlib.sha256(_json(key).encode()).hexdigest() == cache_stem:
            return fact.get("hash")
    return None


def _index_contribution_symbols(connection, cache_key: str, state_key: str, configuration_hash, payload: bytes):
    tu = state_key.split("\0", 1)[0]
    for order, line in enumerate(payload.splitlines()):
        item = json.loads(line)
        if not isinstance(item, dict) or not isinstance(item.get('record'), str):
            raise ValueError(f'invalid TU cache record: {cache_key}:{order + 1}')
        if item.get('record') == 'symbol' and isinstance(item.get('id'), str):
            connection.execute('INSERT INTO provenance_symbols VALUES(?,?,?,?,?,?)',
                (cache_key, order, item['id'], tu, configuration_hash, _pack(item)))


def _validated_contributions(connection, directory: Path, tu_state: dict[str, Any] | None,
                             root: Path, *, validate_sources: bool = False, only_tus: set[str] | None = None) -> bool:
    meta = json.loads(connection.execute("SELECT value FROM metadata WHERE key='index_meta'").fetchone()[0])
    complete = bool((tu_state or {}).get("jobs"))
    for key, job in (tu_state or {}).get('jobs', {}).items():
        if only_tus is not None and key.split('\0', 1)[0] not in only_tus:
            continue
        cache_name = job.get('cache')
        if not isinstance(cache_name, str) or Path(cache_name).name != cache_name:
            raise ValueError(f'invalid TU cache path for {key!r}')
        dependencies = job.get('dependencies', {})
        if not dependencies or not job.get('input_fingerprint'):
            raise ValueError(f'TU cache lacks validated provenance: {key!r}')
        if validate_sources:
            for rel, expected in dependencies.items():
                source = root / rel
                if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                    raise ValueError(f'TU cache dependency changed: {rel}')
        cache = directory / 'cache/tu' / cache_name
        # No provenance is synthesized for missing historical cache payloads.
        if not cache.is_file():
            complete = False
            continue
        encoded = cache.read_bytes()
        payload = zlib.decompress(encoded) if cache_name.endswith('.jsonl.z') else encoded
        configuration_hash = _configuration_hash(connection, cache_name, key, job, meta)
        complete = complete and configuration_hash is not None
        _index_contribution_symbols(connection, cache_name, key, configuration_hash, payload)
        connection.execute('INSERT INTO tu_contributions VALUES(?,?,?,?,?,?)',
            (cache_name, key, job['input_fingerprint'], _pack(dependencies),
             hashlib.sha256(payload).hexdigest(), zlib.compress(payload, 6)))

    return complete


def _insert_symbol(connection, record: dict[str, Any], order: int):
    data = dict(record)
    occurrences = record.get('occurrences')
    has_occurrences = isinstance(occurrences, list)
    if has_occurrences:
        data.pop('occurrences')
    connection.execute('INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
        record['id'], record.get('name', ''), record.get('qualified_name', ''), record.get('kind', ''),
        record.get('file', ''), record.get('line', 0), record.get('end_line', record.get('line', 0)),
        record.get('start', 0), record.get('end', 0), int(bool(record.get('definition'))),
        int('test' in str(record.get('file', '')).lower() or 'test' in str(record.get('name', '')).lower()),
        ' '.join(str(record.get(k, '')) for k in ('qualified_name', 'signature', 'contract', 'file', 'lexical_terms')).lower(),
        order, int(has_occurrences), _pack(data)))
    for occurrence_order, item in enumerate(occurrences if has_occurrences else []):
        connection.execute('INSERT INTO occurrences VALUES(?,?,?,?)',
            (order, occurrence_order, item.get('translation_unit'), _pack(item)))


def _insert_edge(connection, record: dict[str, Any], order: int, intern):
    residual = {k: v for k, v in record.items() if k not in EDGE_FIELDS and k != 'record'}
    values = [intern(record.get(key)) if n < 8 else record.get(key) for n, key in enumerate(EDGE_FIELDS)]
    fields = sum(1 << n for n, key in enumerate(EDGE_FIELDS) if key in record)
    # Public column order follows source/target first; reconstruction uses explicit mapping.
    mapping = dict(zip(EDGE_COLUMNS, values))
    connection.execute('INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
        order, mapping['from_id'], mapping['to_id'], mapping['file_id'], mapping['tu_id'],
        mapping['type_id'], mapping['kind_id'], mapping['config_id'], mapping['via_id'],
        *values[8:], fields, _pack(residual) if residual else None))


def publish(root: Path, records: Iterable[dict[str, Any]], manifest: dict[str, Any], freshness: dict[str, Any],
            tu_state: dict[str, Any] | None = None, readiness: dict[str, Any] | None = None,
            *, state_path: Path | None = None, validate_sources: bool = False) -> str:
    """Stream a complete generation and atomically replace the canonical database.

    Embedded metadata is authoritative. Sidecars are compatibility projections and are
    checked against generation by consumers that need publication consistency.
    """
    directory = state_path if state_path is not None else state_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.index.', suffix='.sqlite', dir=directory)
    os.close(fd)
    temporary = Path(name)
    generation = uuid.uuid4().hex
    connection = sqlite3.connect(temporary)
    try:
        connection.execute('PRAGMA journal_mode=OFF')
        connection.execute('PRAGMA synchronous=OFF')
        connection.executescript(_DDL)
        strings: dict[str, int] = {}
        def intern(value):
            if value is None:
                return None
            if value not in strings:
                cursor = connection.execute('INSERT INTO strings(value) VALUES(?)', (value,))
                strings[value] = cursor.lastrowid
            return strings[value]
        counts = {'file': 0, 'symbol': 0, 'edge': 0, 'other': 0}
        meta = None
        for record in records:
            kind = record.get('record')
            if kind == 'meta':
                if meta is not None:
                    raise ValueError('multiple index meta records')
                meta = record
            elif kind == 'file':
                connection.execute('INSERT INTO files VALUES(?,?,?)', (record['path'], counts['file'], _pack(record)))
                counts['file'] += 1
            elif kind == 'symbol':
                _insert_symbol(connection, record, counts['symbol'])
                counts['symbol'] += 1
            elif kind == 'edge':
                _insert_edge(connection, record, counts['edge'], intern)
                counts['edge'] += 1
            else:
                connection.execute('INSERT INTO residual_records VALUES(?,?)', (counts['other'], _pack(record)))
                counts['other'] += 1
        if meta is None:
            raise ValueError('index meta record missing')
        if Path(str(meta.get('root', root))).resolve() != root.resolve():
            raise ValueError('index belongs to a different source repository')
        manifest = {**manifest, 'generation': generation, 'artifact_id': generation}
        freshness = {**freshness, 'generation': generation}
        metadata = {'schema': SCHEMA, 'generation': generation, 'source_root': str(root.resolve()),
                    'index_meta': meta, 'manifest': manifest, 'freshness': freshness,
                    'tu_state': tu_state or {}, 'readiness': readiness or {}}
        connection.executemany('INSERT INTO metadata VALUES(?,?)', ((k, _json(v)) for k, v in metadata.items()))
        complete = _validated_contributions(connection, directory, tu_state, root, validate_sources=validate_sources)
        connection.execute('INSERT INTO metadata VALUES(?,?)', ('incremental_provenance', _json(complete)))
        connection.commit()
        connection.close()
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, database_path(root, directory))
        _fsync_directory(directory)
        # Descriptor is small and names the exact DB generation. An old descriptor
        # crossing publication fails closed rather than mixing generations.
        descriptor = export_descriptor(root, directory)
        _atomic_json(directory / 'export.json', descriptor)
        _atomic_json(directory / 'manifest.json', manifest)
        _atomic_json(directory / 'cache/freshness.json', freshness)
        if tu_state is not None:
            _atomic_json(directory / 'cache/tu-state.json', tu_state)
        if readiness is not None:
            _atomic_json(directory / 'cache/readiness.json', readiness)
        return generation
    finally:
        connection.close()
        temporary.unlink(missing_ok=True)


def export_descriptor(root: Path, state_path: Path | None = None) -> dict[str, Any]:
    directory = (state_path if state_path is not None else state_dir(root)).resolve()
    store = open_store(root, directory)
    if store is None:
        raise FileNotFoundError(database_path(root, directory))
    try:
        if Path(store.metadata('source_root')).resolve() != root.resolve():
            raise ValueError('index belongs to a different source repository')
        return {'format': 'CTXPP-EXPORT/2', 'root': str(root.resolve()), 'source_root': str(root.resolve()),
                'state_dir': str(directory), 'database': 'index.sqlite', 'schema': 'CTXPP-INDEX/2',
                'generation': store.artifact_identity, 'artifact_id': store.artifact_identity,
                'index_meta': store.meta(), 'index_integrity': True,
                'freshness_scope': 'as-recorded-at-index-time'}
    finally:
        store.close()


def migrate(root: Path, state_path: Path | None = None) -> dict[str, Any]:
    """Stream-import v1 records and independently validated TU cache provenance."""
    directory = state_path if state_path is not None else state_dir(root)
    source = directory / 'index.jsonl'
    manifest = json.loads((directory / 'manifest.json').read_text())
    freshness = json.loads((directory / 'cache/freshness.json').read_text())
    digest = hashlib.sha256()
    with source.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != manifest.get('index_hash'):
        raise ValueError('legacy index and manifest integrity mismatch')
    state_file = directory / 'cache/tu-state.json'
    tu_state = json.loads(state_file.read_text()) if state_file.is_file() else None
    readiness_file = directory / 'cache/readiness.json'
    readiness = json.loads(readiness_file.read_text()) if readiness_file.is_file() else None
    def records():
        with source.open(encoding='utf-8') as stream:
            for number, line in enumerate(stream, 1):
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f'invalid legacy record at line {number}') from exc
    generation = publish(root.resolve(), records(), manifest, freshness, tu_state, readiness,
                         state_path=directory, validate_sources=True)
    return {'format': 'CTXPP-MIGRATION/2', 'generation': generation, 'database': str(database_path(root, directory)),
            'legacy_preserved': True}


def export_legacy(root: Path, output, state_path: Path | None = None) -> None:
    store = open_store(root, state_path)
    if store is None:
        raise FileNotFoundError(database_path(root, state_path))
    try:
        for record in store.iter_records():
            output.write(_json(record) + '\n')
    finally:
        store.close()


def export_legacy_json(root: Path, output, state_path: Path | None = None) -> None:
    """Stream the CTXPP-EXPORT/1 envelope from one pinned generation.

    Each reconstructed record contributes its canonical JSONL bytes to the new
    manifest hash. The manifest follows the array, so neither the graph nor a
    duplicate JSONL serialization is retained in memory or written to state.
    """
    directory = (state_path if state_path is not None else state_dir(root)).resolve()
    store = open_store(root, directory)
    if store is None:
        raise FileNotFoundError(database_path(root, directory))
    try:
        header = {'format': 'CTXPP-EXPORT/1', 'root': str(root.resolve()),
                  'state_dir': str(directory), 'index_meta': store.meta(),
                  'freshness': store.freshness(), 'freshness_scope': 'as-recorded-at-index-time'}
        output.write(_json(header)[:-1] + ',"records":[')
        digest = hashlib.sha256()
        first = True
        for record in store.iter_records():
            encoded = _json(record)
            digest.update((encoded + '\n').encode('utf-8'))
            if not first:
                output.write(',')
            output.write(encoded)
            first = False
        manifest = {**store.manifest(), 'format': 'CTXPP-MANIFEST/1', 'index_hash': digest.hexdigest()}
        output.write('],"manifest":' + _json(manifest) + ',"index_integrity":true}\n')
    finally:
        store.close()


def _check_source_root(connection, root: Path):
    row = connection.execute("SELECT value FROM metadata WHERE key='source_root'").fetchone()
    if not row or Path(str(json.loads(row[0]))).resolve() != root.resolve():
        raise ValueError('index belongs to a different source repository')


def prepare_incremental_provenance(root: Path) -> dict[str, Any]:
    """Index captured, validated TU symbol payloads without parsing any source.

    This is an explicit upgrade for databases produced before the optional
    provenance-symbol index. It does not change the public graph or generation.
    """
    connection = sqlite3.connect(database_path(root), timeout=30)
    try:
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('BEGIN IMMEDIATE')
        _check_source_root(connection, root)
        connection.execute('''CREATE TABLE IF NOT EXISTS provenance_symbols(
            cache_key TEXT NOT NULL,record_order INTEGER NOT NULL,symbol_id TEXT NOT NULL,
            translation_unit TEXT NOT NULL,configuration_hash TEXT,data BLOB NOT NULL,
            PRIMARY KEY(cache_key,record_order))''')
        connection.execute('CREATE INDEX IF NOT EXISTS provenance_symbols_id ON provenance_symbols(symbol_id)')
        connection.execute('CREATE INDEX IF NOT EXISTS provenance_symbols_tu ON provenance_symbols(translation_unit)')
        connection.execute('CREATE INDEX IF NOT EXISTS edges_file ON edges(file_id)')
        prior_complete = connection.execute("SELECT value FROM metadata WHERE key='incremental_provenance'").fetchone()
        if prior_complete and json.loads(prior_complete[0]):
            rows = connection.execute('SELECT count(*) FROM provenance_symbols').fetchone()[0]
            tus = connection.execute('SELECT count(*) FROM tu_contributions').fetchone()[0]
            connection.commit()
            return {'complete': True, 'translation_units': tus, 'symbol_contributions': rows, 'reused': True}
        connection.execute('DELETE FROM provenance_symbols')
        meta = json.loads(connection.execute("SELECT value FROM metadata WHERE key='index_meta'").fetchone()[0])
        tu_state = json.loads(connection.execute("SELECT value FROM metadata WHERE key='tu_state'").fetchone()[0])
        jobs = tu_state.get('jobs', {})
        complete = bool(jobs)
        seen = 0
        # One compressed TU payload at a time, not the public graph.
        for cache_key, state_key, payload in connection.execute(
                'SELECT cache_key,state_key,payload FROM tu_contributions ORDER BY state_key'):
            configuration = _configuration_hash(connection, cache_key, state_key, jobs.get(state_key, {}), meta)
            complete = complete and configuration is not None
            _index_contribution_symbols(connection, cache_key, state_key, configuration, zlib.decompress(payload))
            seen += 1
        complete = complete and seen == len(jobs)
        connection.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('incremental_provenance', _json(complete)))
        rows = connection.execute('SELECT count(*) FROM provenance_symbols').fetchone()[0]
        connection.commit()
        return {'complete': complete, 'translation_units': seen, 'symbol_contributions': rows}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def apply_delta(root: Path, *, affected_tus: set[str], impacted_symbol_ids: set[str],
                symbols: list[dict[str, Any]], edges: list[dict[str, Any]], files: list[dict[str, Any]],
                meta: dict[str, Any], manifest: dict[str, Any], freshness: dict[str, Any],
                tu_state: dict[str, Any], readiness: dict[str, Any],
                expected_generation: str | None = None, removed_files: set[str] | None = None,
                replace_edge_files: set[str] | None = None, _database: Path | None = None, _project: bool = True) -> str:
    """Commit affected rows and metadata using a durable rollback-journal transaction.

    Unlike initial full publication this mutates the existing DB, so journaling
    and synchronous durability are mandatory. Caller holds publication_lock and
    closes its read transaction first. Readers observe one committed generation.
    """
    from ctxpp_runtime import count
    directory = state_dir(root)
    connection = sqlite3.connect(_database or database_path(root), timeout=0.25 if _database is None else 30)
    contention = False
    try:
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('BEGIN IMMEDIATE')
        _check_source_root(connection, root)
        current = json.loads(connection.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0])
        if expected_generation is not None and current != expected_generation:
            raise ValueError('semantic generation changed during incremental scan; retry')
        generation = uuid.uuid4().hex
        # Edge/occurrence indexes make these operations proportional to affected
        # TU records. Stable semantic IDs intentionally are not unique row IDs.
        deleted_edges = 0
        for tu in sorted(affected_tus):
            row = connection.execute('SELECT id FROM strings WHERE value=?', (tu,)).fetchone()
            if row:
                cursor = connection.execute('DELETE FROM edges WHERE tu_id=?', (row[0],))
                deleted_edges += cursor.rowcount
            cache_keys = [row[0] for row in connection.execute(
                'SELECT cache_key FROM tu_contributions WHERE state_key>=? AND state_key<?', (tu + '\0', tu + '\1'))]
            for cache_key in cache_keys:
                connection.execute('DELETE FROM provenance_symbols WHERE cache_key=?', (cache_key,))
                connection.execute('DELETE FROM tu_contributions WHERE cache_key=?', (cache_key,))
        for file in sorted(replace_edge_files or ()):
            row = connection.execute('SELECT id FROM strings WHERE value=?', (file,)).fetchone()
            if row:
                deleted_edges += connection.execute('DELETE FROM edges WHERE file_id=?', (row[0],)).rowcount
        values = sorted(impacted_symbol_ids)
        deleted_symbols = 0
        for offset in range(0, len(values), 400):
            batch = values[offset:offset + 400]
            marks = ','.join('?' for _ in batch)
            connection.execute(f'DELETE FROM occurrences WHERE symbol_row_id IN '
                               f'(SELECT symbol_order FROM symbols WHERE id IN ({marks}))', batch)
            deleted_symbols += connection.execute(f'DELETE FROM symbols WHERE id IN ({marks})', batch).rowcount
        symbol_order = connection.execute('SELECT coalesce(max(symbol_order),-1)+1 FROM symbols').fetchone()[0]
        for record in symbols:
            if record['id'] not in impacted_symbol_ids:
                raise ValueError(f'delta includes unclaimed symbol identity: {record["id"]}')
            _insert_symbol(connection, record, symbol_order)
            symbol_order += 1
        interned = {}
        def intern(value):
            if value is None:
                return None
            if value not in interned:
                row = connection.execute('SELECT id FROM strings WHERE value=?', (value,)).fetchone()
                interned[value] = row[0] if row else connection.execute('INSERT INTO strings(value) VALUES(?)', (value,)).lastrowid
            return interned[value]
        edge_order = connection.execute('SELECT coalesce(max(edge_order),-1)+1 FROM edges').fetchone()[0]
        for record in edges:
            if record.get('translation_unit') not in affected_tus and record.get('file') not in (replace_edge_files or set()):
                raise ValueError('delta edge lacks affected TU provenance')
            _insert_edge(connection, record, edge_order, intern)
            edge_order += 1
        for path in sorted(removed_files or ()):
            connection.execute('DELETE FROM files WHERE path=?', (path,))
        file_order = connection.execute('SELECT coalesce(max(file_order),-1)+1 FROM files').fetchone()[0]
        for record in files:
            row = connection.execute('SELECT file_order FROM files WHERE path=?', (record['path'],)).fetchone()
            order = row[0] if row else file_order
            file_order += not bool(row)
            connection.execute('INSERT OR REPLACE INTO files VALUES(?,?,?)', (record['path'], order, _pack(record)))
        manifest = {**manifest, 'generation': generation, 'artifact_id': generation}
        freshness = {**freshness, 'generation': generation}
        metadata = {'generation': generation, 'index_meta': meta, 'manifest': manifest, 'freshness': freshness,
                    'tu_state': tu_state, 'readiness': readiness}
        connection.executemany('INSERT OR REPLACE INTO metadata VALUES(?,?)', ((key, _json(value)) for key, value in metadata.items()))
        complete = _validated_contributions(connection, directory, tu_state, root, only_tus=affected_tus)
        if not complete and any(key.split('\0', 1)[0] in affected_tus for key in tu_state.get('jobs', {})):
            raise ValueError('incremental contribution provenance is incomplete')
        connection.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('incremental_provenance', _json(complete)))
        connection.commit()
        count('incremental_store_updates')
        count('semantic_symbol_rows_replaced', deleted_symbols)
        count('semantic_edge_rows_replaced', deleted_edges)
        count('semantic_records_written', len(symbols) + len(edges) + len(files))
    except sqlite3.OperationalError as exc:
        connection.rollback()
        if _database is not None or 'locked' not in str(exc).lower():
            raise
        contention = True
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    if contention:
        # Long read snapshots must not stall a writer. Preserve their inode and
        # apply the same bounded SQL delta to a private copy. Prefer reflink;
        # portable fallback copies bytes without reconstructing any graph rows.
        import shutil
        import fcntl
        fd, temporary_name = tempfile.mkstemp(prefix='.delta.', suffix='.sqlite', dir=directory)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            try:
                with database_path(root).open('rb') as source, temporary.open('wb') as destination:
                    fcntl.ioctl(destination.fileno(), 0x40049409, source.fileno())  # Linux FICLONE
            except OSError:
                shutil.copyfile(database_path(root), temporary)
                count('incremental_contention_copy_bytes', temporary.stat().st_size)
            generation = apply_delta(root, affected_tus=affected_tus, impacted_symbol_ids=impacted_symbol_ids,
                symbols=symbols, edges=edges, files=files, meta=meta, manifest=manifest, freshness=freshness,
                tu_state=tu_state, readiness=readiness, expected_generation=expected_generation,
                removed_files=removed_files, replace_edge_files=replace_edge_files, _database=temporary, _project=False)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, database_path(root))
            _fsync_directory(directory)
            manifest = {**manifest, 'generation': generation, 'artifact_id': generation}
            freshness = {**freshness, 'generation': generation}
            count('incremental_reader_contention_fallbacks')
        finally:
            temporary.unlink(missing_ok=True)
    if not _project:
        return generation
    _atomic_json(directory / 'export.json', export_descriptor(root))
    _atomic_json(directory / 'manifest.json', manifest)
    _atomic_json(directory / 'cache/freshness.json', freshness)
    _atomic_json(directory / 'cache/tu-state.json', tu_state)
    _atomic_json(directory / 'cache/readiness.json', readiness)
    return generation


def compress_legacy_caches(root: Path) -> dict[str, Any]:
    """Losslessly compress captured TU caches and retire only verified raw copies.

    The caller holds publication_lock. New files are verified before the metadata
    transaction; old raw files are removed only after its durable commit and
    compatibility projections have succeeded. A failed operation preserves raw
    files, and a retry safely reuses matching compressed copies.
    """
    directory = state_dir(root)
    store = open_store(root)
    if store is None:
        raise FileNotFoundError(database_path(root))
    try:
        expected_generation = store.artifact_identity
        state = store.metadata('tu_state', {})
        manifest, freshness = store.manifest(), store.freshness()
        jobs = {key: dict(value) for key, value in state.get('jobs', {}).items()}
        captured = {row[0]: row[1] for row in store.connection.execute('SELECT cache_key,cache_sha256 FROM tu_contributions')}
    finally:
        store.close()
    replacements = []
    # A prior run may have committed metadata before a projection failure.
    # Recover only sidecopies proven equivalent to an embedded contribution.
    retire_only = []
    for job in jobs.values():
        name = job.get('cache', '')
        if not isinstance(name, str) or not name.endswith('.jsonl.z') or Path(name).name != name:
            continue
        source = directory / 'cache/tu' / name.removesuffix('.z')
        destination = directory / 'cache/tu' / name
        expected = captured.get(name)
        if (source.is_file() and destination.is_file() and expected
                and hashlib.sha256(source.read_bytes()).hexdigest() == expected
                and hashlib.sha256(zlib.decompress(destination.read_bytes())).hexdigest() == expected):
            retire_only.append((source, destination, expected))
    raw_bytes = compressed_bytes = 0
    for state_key, job in sorted(jobs.items()):
        name = job.get('cache', '')
        if not isinstance(name, str) or not name.endswith('.jsonl'):
            continue
        if Path(name).name != name or name not in captured:
            raise ValueError(f'legacy cache lacks captured provenance: {name}')
        source = directory / 'cache/tu' / name
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != captured[name]:
            raise ValueError(f'legacy cache changed after publication: {name}')
        destination = source.with_name(source.name + '.z')
        compressed = zlib.compress(payload, 6)
        if destination.exists():
            if zlib.decompress(destination.read_bytes()) != payload:
                raise ValueError(f'compressed cache already contains different data: {destination.name}')
        else:
            fd, temporary = tempfile.mkstemp(prefix='.' + destination.name, dir=destination.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(compressed)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        if hashlib.sha256(zlib.decompress(destination.read_bytes())).hexdigest() != digest:
            raise ValueError(f'compressed cache verification failed: {destination.name}')
        job['cache'] = destination.name
        replacements.append((source, destination, digest))
        raw_bytes += len(payload)
        compressed_bytes += destination.stat().st_size
    if not replacements:
        removed = 0
        if retire_only:
            _atomic_json(directory / 'export.json', export_descriptor(root))
            _atomic_json(directory / 'manifest.json', manifest)
            _atomic_json(directory / 'cache/freshness.json', freshness)
            _atomic_json(directory / 'cache/tu-state.json', state)
            for source, destination, expected in retire_only:
                if source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == expected:
                    source.unlink()
                    removed += 1
            _fsync_directory(directory / 'cache/tu')
        return {'format': 'CTXPP-CACHE-COMPRESSION/1', 'caches': 0, 'raw_bytes': 0,
                'raw_copies_removed': removed, 'compressed_bytes': 0, 'generation': expected_generation}
    _fsync_directory(directory / 'cache/tu')
    state = {**state, 'jobs': jobs}
    generation = uuid.uuid4().hex
    manifest = {**manifest, 'generation': generation, 'artifact_id': generation}
    freshness = {**freshness, 'generation': generation}
    connection = sqlite3.connect(database_path(root), timeout=30)
    try:
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('BEGIN IMMEDIATE')
        current = json.loads(connection.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0])
        if current != expected_generation:
            raise ValueError('semantic generation changed during cache compression; retry')
        has_provenance = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provenance_symbols'").fetchone()
        for source, destination, digest in replacements:
            if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                raise ValueError(f'legacy cache changed during compression: {source.name}')
            connection.execute('UPDATE tu_contributions SET cache_key=? WHERE cache_key=?', (destination.name, source.name))
            if has_provenance:
                connection.execute('UPDATE provenance_symbols SET cache_key=? WHERE cache_key=?', (destination.name, source.name))
        values = {'generation': generation, 'manifest': manifest, 'freshness': freshness, 'tu_state': state}
        connection.executemany('INSERT OR REPLACE INTO metadata VALUES(?,?)', ((key, _json(value)) for key, value in values.items()))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    _atomic_json(directory / 'export.json', export_descriptor(root))
    _atomic_json(directory / 'manifest.json', manifest)
    _atomic_json(directory / 'cache/freshness.json', freshness)
    _atomic_json(directory / 'cache/tu-state.json', state)
    removed = 0
    for source, destination, digest in [*replacements, *retire_only]:
        # Preserve an unexpected concurrent replacement rather than removing it.
        if source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == digest:
            source.unlink()
            removed += 1
    _fsync_directory(directory / 'cache/tu')
    return {'format': 'CTXPP-CACHE-COMPRESSION/1', 'caches': len(replacements),
            'raw_copies_removed': removed, 'raw_bytes': raw_bytes, 'compressed_bytes': compressed_bytes,
            'generation': generation}
