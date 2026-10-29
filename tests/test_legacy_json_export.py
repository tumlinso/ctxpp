from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import ctxpp_store as storage


class LegacyJsonExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'source'
        self.root.mkdir()
        self.state = Path(self.temporary.name) / 'state'
        sources = {'a.cpp': 'int alpha() { return beta(); }\n', 'b.cpp': 'int beta() { return 2; }\n'}
        self.records = [{'record': 'meta', 'format': 'CTXPP-INDEX/1', 'root': str(self.root),
                         'semantic': True, 'backend': 'libclang-runtime', 'incomplete': False}]
        self.files = {}
        for path, text in sources.items():
            (self.root / path).write_text(text)
            digest = hashlib.sha256(text.encode()).hexdigest()
            self.files[path] = digest
            self.records.append({'record': 'file', 'path': path, 'hash': digest, 'parse_status': 'ok'})
        self.records.extend([
            {'record': 'symbol', 'id': 'alpha', 'name': 'alpha', 'qualified_name': 'alpha', 'file': 'a.cpp',
             'definition': True, 'line': 1, 'end_line': 1, 'start': 0, 'end': len(sources['a.cpp']) - 1},
            {'record': 'symbol', 'id': 'beta', 'name': 'beta', 'qualified_name': 'beta', 'file': 'b.cpp',
             'definition': True, 'line': 1, 'end_line': 1, 'start': 0, 'end': len(sources['b.cpp']) - 1,
             'contract': 'Unicode survives: β'},
            {'record': 'edge', 'type': 'call', 'from': 'alpha', 'to': 'beta', 'file': 'a.cpp', 'start': 21, 'end': 25},
        ])
        self.freshness = {'format': 'CTXPP-FRESHNESS/1', 'files': {
            path: {'hash': digest, 'size': (self.root / path).stat().st_size,
                   'mtime_ns': (self.root / path).stat().st_mtime_ns} for path, digest in self.files.items()}}
        storage.publish(self.root, self.records, {'format': 'CTXPP-MANIFEST/2', 'files': self.files},
                        self.freshness, state_path=self.state)

    def snapshot(self):
        return {path.relative_to(self.state).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.state.rglob('*') if path.is_file()}

    def test_streamed_envelope_reconstructs_exact_records_and_legacy_manifest(self):
        before = self.snapshot()
        output = io.StringIO()
        # Only the streaming iterator may read graph records.
        with mock.patch.object(storage.IndexedStore, 'all_symbols_edges', side_effect=AssertionError('full graph')):
            storage.export_legacy_json(self.root, output, self.state)
        envelope = json.loads(output.getvalue())
        self.assertEqual(envelope['format'], 'CTXPP-EXPORT/1')
        self.assertEqual(envelope['root'], str(self.root))
        self.assertEqual(envelope['state_dir'], str(self.state))
        self.assertEqual(envelope['records'], self.records)
        self.assertEqual(envelope['index_meta'], self.records[0])
        self.assertEqual(envelope['manifest']['format'], 'CTXPP-MANIFEST/1')
        self.assertEqual(envelope['manifest']['files'], self.files)
        raw = io.StringIO()
        storage.export_legacy(self.root, raw, self.state)
        self.assertEqual(envelope['manifest']['index_hash'], hashlib.sha256(raw.getvalue().encode()).hexdigest())
        self.assertTrue(envelope['index_integrity'])
        self.assertEqual(envelope['freshness']['files'], self.freshness['files'])
        self.assertEqual(envelope['freshness_scope'], 'as-recorded-at-index-time')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / '.ctxpp').exists())

    def test_emits_each_record_before_requesting_the_next(self):
        output = io.StringIO()
        records = self.records
        def records_in_order(store):
            for number, record in enumerate(records):
                if number:
                    self.assertIn(storage._json(records[number - 1]), output.getvalue())
                yield record
        with mock.patch.object(storage.IndexedStore, 'iter_records', records_in_order):
            storage.export_legacy_json(self.root, output, self.state)
        self.assertEqual(json.loads(output.getvalue())['records'], records)
        self.assertGreater(output.getvalue().index('"manifest":'), output.getvalue().index('"records":'))

    def test_cli_read_only_flags_are_exclusive_and_default_stays_v2(self):
        before = self.snapshot()
        command = [str(SCRIPTS / 'ctxpp'), '--root', str(self.root), '--state-dir', str(self.state), '--read-only', 'export']
        exported = subprocess.run([*command, '--legacy-json'], capture_output=True, text=True)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertEqual(json.loads(exported.stdout)['records'], self.records)
        default = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(json.loads(default.stdout)['format'], 'CTXPP-EXPORT/2')
        raw = subprocess.run([*command, '--legacy-jsonl'], capture_output=True, text=True)
        self.assertEqual(raw.returncode, 0, raw.stderr)
        self.assertEqual([json.loads(line) for line in raw.stdout.splitlines()], self.records)
        conflict = subprocess.run([*command, '--legacy-json', '--legacy-jsonl'], capture_output=True, text=True)
        self.assertEqual(conflict.returncode, 2)
        self.assertIn('not allowed with argument', conflict.stderr)
        self.assertEqual(conflict.stdout, '')
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
