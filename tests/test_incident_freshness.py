from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
from ctxpp_store import open_store


class IncidentFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'include').mkdir()
        (self.root / 'src').mkdir()
        (self.root / '.ctxpp.toml').write_text('version=1\nsources=["include/**/*.hpp","src/**/*.cpp"]\n')
        (self.root / 'include/api.hpp').write_text('int foo();\n')
        (self.root / 'src/foo.cpp').write_text('#include "api.hpp"\nint foo() { return 1; }\n')
        (self.root / 'src/caller.cpp').write_text('#include "api.hpp"\nint caller() { return foo(); }\n')
        (self.root / 'src/unrelated.cpp').write_text('int unrelated() { return 3; }\n')
        commands = [{'directory': str(self.root), 'file': str(self.root / name),
                     'arguments': ['c++', '-std=c++17', '-I' + str(self.root / 'include'), '-c', str(self.root / name)]}
                    for name in ('src/foo.cpp', 'src/caller.cpp', 'src/unrelated.cpp')]
        (self.root / 'compile_commands.json').write_text(json.dumps(commands))
        self.cli('scan')

    def cli(self, *args, profile=None):
        environment = dict(os.environ)
        if profile:
            environment['CTXPP_PROFILE_PATH'] = str(profile)
        command = [str(SKILL / 'scripts/ctxpp'), '--root', str(self.root), '--json', *args]
        result = subprocess.run(command, capture_output=True, text=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_normal_callee_inspect_refreshes_removed_last_caller_only(self):
        initial = self.cli('inspect', 'foo', '--budget', '10000', '--max-items', '24')
        self.assertEqual([item['name'] for item in initial['context']['callers']], ['caller'])
        (self.root / 'src/caller.cpp').write_text('#include "api.hpp"\nint caller() { return 0; }\n')
        # An unrelated dirty source must not be parsed for this callee request.
        (self.root / 'src/unrelated.cpp').write_text('int unrelated() { return 4; }\n')
        profile = self.root / 'refresh.json'
        current = self.cli('inspect', 'foo', '--budget', '10000', '--max-items', '24', profile=profile)
        self.assertEqual(current['context']['callers'], [])
        counters = json.loads(profile.read_text())['counters']
        self.assertEqual(counters.get('tus_parsed', 0), 1)
        self.assertEqual(counters.get('incremental_store_updates', 0), 1)
        store = open_store(self.root)
        try:
            foo = store.exact('foo', 1)[0]
            self.assertFalse(any(edge.get('type') == 'call' for edge in store.edges_for(foo['id'])))
        finally:
            store.close()
        repeated_profile = self.root / 'repeated.json'
        self.cli('inspect', 'foo', '--budget', '10000', profile=repeated_profile)
        self.assertEqual(json.loads(repeated_profile.read_text())['counters'].get('tus_parsed', 0), 0)

    def test_read_only_omits_removed_caller_without_state_writes(self):
        (self.root / 'src/caller.cpp').write_text('#include "api.hpp"\nint caller() { return 0; }\n')
        state = self.root / '.ctxpp'
        before = {str(p.relative_to(state)): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in state.rglob('*') if p.is_file()}
        packet = self.cli('--read-only', 'inspect', 'foo', '--budget', '10000', '--max-items', '24')
        self.assertEqual(packet['context']['callers'], [])
        self.assertGreater(packet['coverage']['omitted'].get('stale_relationships', 0), 0)
        self.assertFalse(packet['coverage']['sufficient'])
        self.assertEqual(packet['trust']['relationships'], 'lexical-or-partial')
        self.assertEqual({str(p.relative_to(state)): (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in state.rglob('*') if p.is_file()}, before)

    def test_read_only_target_edited_before_packet_does_not_get_current_rendering(self):
        (self.root / 'src/foo.cpp').write_text('// edited before packet construction\n#include "api.hpp"\nint foo() { return 20; }\n')
        packet = self.cli('--read-only', 'inspect', 'foo', '--budget', '10000')
        self.assertEqual(packet['trust']['target_range'], 'unverified')
        command = [str(SKILL / 'scripts/ctxpp'), '--root', str(self.root), '--read-only',
                   'inspect', 'foo', '--budget', '2400']
        human = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn('stale or unverified', human.stdout)
        self.assertNotIn('source=canonical current-sha256=', human.stdout)
        self.assertNotIn('canonical source, complete range', human.stdout)
        self.assertNotIn('#include', human.stdout)
        spec = json.dumps({'task_id': 'stale-target', 'target_symbols': ['foo'],
                           'read_paths': ['src/foo.cpp'], 'intent': 'edit'})
        task_packet = self.cli('--read-only', 'inspect', '--task-spec', spec, '--budget', '10000')
        self.assertEqual(task_packet['trust']['freshness'], 'partial')
        task_command = [str(SKILL / 'scripts/ctxpp'), '--root', str(self.root), '--read-only',
                        'inspect', '--task-spec', spec, '--budget', '2400']
        task_human = subprocess.run(task_command, capture_output=True, text=True)
        self.assertEqual(task_human.returncode, 0, task_human.stderr)
        self.assertIn('stale or unverified', task_human.stdout)
        self.assertNotIn('canonical source, complete range', task_human.stdout)
        self.assertNotIn('#include', task_human.stdout)


if __name__ == '__main__':
    unittest.main()
