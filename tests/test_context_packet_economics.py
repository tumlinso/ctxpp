from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
RUNNER = SKILL / "evals/run_packet_economics.py"


class ContextPacketEconomicsTests(unittest.TestCase):
    def test_fixed_budget_report_is_fresh_bounded_and_explicit_about_unavailable_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report.json"
            subprocess.run(["python", str(RUNNER), "--output", str(output)], cwd=SKILL,
                           check=True, text=True, capture_output=True)
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(report["format"], "CTXPP-PACKET-ECONOMICS/1")
        self.assertEqual(report["fixed_budgets"], [1200, 2500, 10000])
        self.assertEqual(len(report["events"]), 3)
        self.assertLessEqual(len(report["events"]), report["bounded_to"])
        self.assertTrue(report["tokenizer"]["exact"])
        for event in report["events"]:
            self.assertTrue(event["freshness"]["canonical_target"])
            self.assertFalse(event["freshness"]["relationships"])
            self.assertGreater(event["packet_latency_ms"], 0)
            self.assertGreater(event["exact_packet_tokens"], 0)
            self.assertGreater(event["compact_inspect_tokens"], 0)
            self.assertLess(event["compact_inspect_tokens"], event["exact_packet_tokens"])
            self.assertGreater(event["canonical_source_avoided_tokens"], 0)
            self.assertGreaterEqual(event["expansion_handles"], 1)
            self.assertEqual(event["broad_source_fallbacks"], 0)
            self.assertIsNone(event["local_worker_success"])
            self.assertIsNone(event["accepted_patch"])
            self.assertIsNone(event["codex_reinvestigation"])
        self.assertEqual(report["summary"]["fresh_canonical_targets"], 3)
        self.assertEqual(report["summary"]["fresh_relationship_sets"], 0)
        self.assertEqual(report["summary"]["broad_source_fallbacks"], 0)
        self.assertEqual(
            report["summary"]["availability"],
            {"accepted_patch": False, "codex_reinvestigation": False, "local_worker_success": False},
        )

    def test_checked_in_report_matches_deterministic_economics_fields(self) -> None:
        expected_path = SKILL / "tests/expected/context-packet-economics.json"
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            actual_path = Path(temp) / "report.json"
            subprocess.run(["python", str(RUNNER), "--output", str(actual_path)], cwd=SKILL,
                           check=True, text=True, capture_output=True)
            actual = json.loads(actual_path.read_text(encoding="utf-8"))

        def stable_fields(report: dict) -> dict:
            result = dict(report)
            result["summary"] = dict(result["summary"])
            result["summary"].pop("mean_packet_latency_ms", None)
            result["events"] = [
                {key: value for key, value in event.items() if key != "packet_latency_ms"}
                for event in result["events"]
            ]
            return result

        self.assertEqual(stable_fields(actual), stable_fields(expected))


if __name__ == "__main__":
    unittest.main()
