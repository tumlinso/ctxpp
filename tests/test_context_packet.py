from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
FIXTURE = SKILL / "tests/fixtures/sample"
SCHEMA = SKILL / "schemas/context-packet-v1.schema.json"
sys.path.insert(0, str(SCRIPTS))


class ContextPacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run([str(SKILL / "scripts/build_tool.sh")], cwd=SKILL, check=True, text=True, capture_output=True)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        shutil.copytree(FIXTURE, self.root, ignore=shutil.ignore_patterns("build", ".ctxpp", "__pycache__"))
        os.chmod(self.root / "tests/tokenizer.py", 0o755)
        subprocess.run(["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"], cwd=self.root,
                       check=True, text=True, capture_output=True)
        subprocess.run(["cmake", "--build", "build", "--parallel", "2"], cwd=self.root,
                       check=True, text=True, capture_output=True)
        self.ctxpp("scan")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def ctxpp(self, *args: str, json_output: bool = True, ok: bool = True) -> subprocess.CompletedProcess[str]:
        command = [str(SCRIPTS / "ctxpp"), "--root", str(self.root)]
        if json_output:
            command.append("--json")
        command.extend(args)
        proc = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        if ok and proc.returncode != 0:
            self.fail(f"ctxpp failed: {' '.join(command)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

    def packet(self, *extra: str) -> dict:
        return json.loads(self.ctxpp("packet", "demo::PackingPlan::freeze", "--intent", "edit",
                                    "--budget", "10000", "--max-items", "32", *extra).stdout)

    def test_packet_selects_hash_verified_canonical_target_and_bounded_context(self) -> None:
        packet = self.packet()
        self.assertEqual(packet["format"], "CTXPP-CONTEXT-PACKET/1")
        self.assertEqual(packet["schema_version"], 1)
        self.assertTrue(packet["readonly"])
        target = packet["target"]
        location = target["location"]
        source = self.root / location["path"]
        data = source.read_bytes()
        selected = data[location["byte_start"]:location["byte_end"]].decode()
        self.assertEqual(target["content"], selected)
        self.assertEqual(location["content_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(target["text_sha256"], hashlib.sha256(selected.encode()).hexdigest())
        self.assertEqual(packet["trust"]["target_range"], "hash-verified")
        self.assertEqual(packet["trust"]["relationships"], "semantic")
        self.assertTrue(packet["coverage"]["sufficient"])
        self.assertLessEqual(sum(len(values) for values in packet["context"].values()), 32)
        self.assertLessEqual(len(packet["expansions"]), 2)
        self.assertTrue(all(isinstance(item["argv"], list) for item in packet["expansions"]))
        unhashed = dict(packet)
        digest = unhashed.pop("packet_hash")
        encoded = json.dumps(unhashed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(digest, hashlib.sha256(encoded).hexdigest())
        json.loads(SCHEMA.read_text(encoding="utf-8"))

    def test_budget_removes_optional_relationships_and_reports_mandatory_overflow(self) -> None:
        bounded = json.loads(self.ctxpp("packet", "demo::PackingPlan::freeze", "--budget", "2500",
                                       "--max-items", "12").stdout)
        self.assertLessEqual(bounded["estimated_tokens"], 2500)
        self.assertFalse(bounded["coverage"]["budget_exceeded"])
        self.assertGreater(sum(bounded["coverage"]["omitted"].values()), 0)
        mandatory = json.loads(self.ctxpp("packet", "demo::PackingPlan::freeze", "--budget", "100",
                                         "--max-items", "1").stdout)
        self.assertTrue(mandatory["coverage"]["budget_exceeded"])
        self.assertFalse(mandatory["coverage"]["sufficient"])
        self.assertIn("PackingPlan::freeze", mandatory["target"]["content"])

    def test_packet_reports_types_dependencies_tests_and_contract_invariants(self) -> None:
        packet = self.packet()
        names = {item["name"] for item in packet["context"]["types"]}
        dependencies = {item["name"] for item in packet["context"]["dependencies"]}
        tests = packet["context"]["tests"]
        invariants = {(item["kind"], item["text"]) for item in packet["invariants"]}
        self.assertIn("demo::Candidate", names)
        self.assertIn("demo::PackingPlan::frozen_score_", dependencies)
        self.assertTrue(any(item["location"]["path"] == "tests/test_plan.cpp" for item in tests))
        self.assertIn(("req", "limit>=0"), invariants)
        self.assertIn(("mut", "frozen_score_"), invariants)
        self.assertIn(("err", "throws on negative"), invariants)

    def test_packet_refreshes_the_target_and_changes_source_identity(self) -> None:
        before = self.packet()
        source = self.root / "src/plan.cpp"
        source.write_text(source.read_text(encoding="utf-8").replace("negative limit", "invalid limit"), encoding="utf-8")
        after = self.packet()
        self.assertIn("invalid limit", after["target"]["content"])
        self.assertNotEqual(before["source_identity"]["fingerprint"], after["source_identity"]["fingerprint"])
        self.assertNotEqual(before["target"]["text_sha256"], after["target"]["text_sha256"])
        self.assertEqual(after["trust"]["target_range"], "hash-verified")

    def test_inspect_is_human_first_and_json_uses_the_packet_contract(self) -> None:
        human = self.ctxpp("inspect", "demo::PackingPlan::freeze", "--max-items", "5", json_output=False).stdout
        self.assertIn("demo::PackingPlan::freeze", human)
        self.assertIn("edit src/plan.cpp:", human)
        self.assertIn("source=canonical", human)
        self.assertIn("expand: ctxpp expand", human)
        machine = json.loads(self.ctxpp("inspect", "demo::PackingPlan::freeze", "--max-items", "5").stdout)
        self.assertEqual(machine["format"], "CTXPP-CONTEXT-PACKET/1")

    def test_ambiguous_target_returns_candidates_not_a_guessed_packet(self) -> None:
        ambiguous = json.loads(self.ctxpp("packet", "overloaded").stdout)
        self.assertEqual(ambiguous["format"], "CTXPP-AMBIGUOUS/1")
        self.assertEqual(len(ambiguous["candidates"]), 2)

    def test_task_spec_packet_v2_supports_multiple_targets_and_intent_trust(self) -> None:
        spec = json.dumps({
            "objective": "Edit the freeze path and its candidate contract",
            "role": "edit",
            "read_paths": ["include/plan.hpp"],
            "write_paths": ["src/plan.cpp"],
            "forbidden_paths": ["vendor"],
            "target_symbols": ["demo::PackingPlan::freeze", "demo::Candidate"],
            "failing_tests": ["cpContextPacketTest"],
            "interface_ids": ["ctxpp-task-packet-v2"],
            "acceptance_gates": ["focused"],
        })
        packet = json.loads(self.ctxpp(
            "packet", "--task-spec", spec, "--consumer", "local-worker",
            "--budget", "30000", "--max-items", "32",
        ).stdout)
        self.assertEqual(packet["format"], "CTXPP-CONTEXT-PACKET/2")
        self.assertEqual(packet["schema_version"], 2)
        self.assertEqual(len(packet["canonical_targets"]), 2)
        self.assertEqual(packet["consumer"], "local-worker")
        self.assertIn("edit", packet["trust"]["sufficient_for"], packet["trust"] | {
            "budget_exceeded": packet["budget_exceeded"],
            "estimated_tokens": packet["estimated_tokens"],
            "target_paths": [item["location"]["path"] for item in packet["canonical_targets"]],
        })
        self.assertEqual(packet["trust"]["missing_required"], [])
        self.assertEqual(packet["source_identity"]["algorithm_version"], 1)
        self.assertTrue(all(item["canonical"] for item in packet["canonical_targets"]))
        unhashed = dict(packet)
        digest = unhashed.pop("packet_hash")
        encoded = json.dumps(unhashed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(digest, hashlib.sha256(encoded).hexdigest())
        schema = json.loads((SKILL / "schemas/context-packet-v2.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["format"]["const"], "CTXPP-CONTEXT-PACKET/2")

    def test_task_spec_reports_missing_required_canonical_paths(self) -> None:
        spec = json.dumps({
            "objective": "Review freeze while requiring another source file",
            "intent": "review",
            "write_paths": ["src/other.cpp"],
            "target_symbols": ["demo::PackingPlan::freeze"],
        })
        packet = json.loads(self.ctxpp(
            "packet", "--task-spec", spec, "--budget", "10000", "--max-items", "16",
        ).stdout)
        self.assertEqual(packet["trust"]["missing_required"], ["src/other.cpp"])
        self.assertEqual(packet["trust"]["sufficient_for"], [])

    def test_performance_task_packet_retains_accepted_path_and_campaign_routing(self) -> None:
        spec = json.dumps({
            "objective": "Measure the accepted freeze implementation for campaign freeze-latency",
            "intent": "performance", "task_id": "CUDA-EDIT", "campaign_id": "freeze-latency",
            "accepted_changed_paths": ["src/plan.cpp"], "read_paths": ["src/plan.cpp"],
            "target_symbols": ["demo::PackingPlan::freeze"],
        })
        packet = json.loads(self.ctxpp(
            "packet", "--task-spec", spec, "--consumer", "cuda", "--budget", "10000",
        ).stdout)
        self.assertEqual(packet["request"]["intent"], "performance")
        self.assertEqual({key: packet["task_spec"][key] for key in (
            "task_id", "campaign_id", "accepted_changed_paths",
        )}, {
            "task_id": "CUDA-EDIT", "campaign_id": "freeze-latency",
            "accepted_changed_paths": ["src/plan.cpp"],
        })
        self.assertEqual(packet["consumer"], "cuda")


if __name__ == "__main__":
    unittest.main()
