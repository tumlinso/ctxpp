from __future__ import annotations

import os
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ctxpp_runtime as runtime


@dataclass
class Result:
    value: int
    peak_memory_mb: int


class ResourceDetectionTests(unittest.TestCase):
    def test_cpu_affinity_and_cgroup_quota_are_hard_caps(self) -> None:
        values = {
            "/sys/fs/cgroup/cpuset.cpus.effective": "0-7",
            "/sys/fs/cgroup/cpu.max": "300000 100000",
            "/sys/fs/cgroup/memory.max": str(16 * 1024**3),
            "/sys/fs/cgroup/memory.current": str(2 * 1024**3),
        }
        with mock.patch.object(os, "cpu_count", return_value=80), \
             mock.patch.object(os, "sched_getaffinity", return_value=set(range(12))), \
             mock.patch.object(os, "getloadavg", return_value=(0.0, 0.0, 0.0)), \
             mock.patch.object(runtime, "_read_text", side_effect=lambda path: values.get(path, "")), \
             mock.patch.object(runtime, "_memory_info", return_value=(128 * 1024**3, 64 * 1024**3, 0)):
            envelope = runtime.detect_resources()
        self.assertEqual(envelope.effective_cpus, 3)
        self.assertEqual(envelope.cpu_budget, 3)
        self.assertEqual(envelope.memory_limit, 16 * 1024**3)
        self.assertLess(envelope.memory_budget, 14 * 1024**3)


class SchedulerTests(unittest.TestCase):
    def envelope(self, cpus: int, memory_mb: int) -> runtime.ResourceEnvelope:
        return runtime.ResourceEnvelope(cpus, cpus, cpus, cpus, cpus, cpus, 1, memory_mb * 1024**2,
                                        memory_mb * 1024**2, memory_mb * 1024**2, memory_mb * 1024**2)

    def test_pending_work_and_memory_bound_concurrency_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            jobs = [{"history_key": str(i), "input_fingerprint": "a", "memory_hint_mb": 700, "size": 0} for i in range(2)]
            scheduler = runtime.ResourceScheduler(Path(temp), envelope=self.envelope(32, 1000))
            scheduler.run(jobs, lambda job: (time.sleep(0.01), Result(int(job["history_key"]), 700))[1])
            self.assertEqual(scheduler.initial_worker_cap, 2)
            self.assertEqual(scheduler.peak_workers, 1)

    def test_small_jobs_use_more_concurrency_and_history_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobs = [{"history_key": str(i), "input_fingerprint": "a", "memory_hint_mb": 100, "size": 0} for i in range(6)]
            scheduler = runtime.ResourceScheduler(root, envelope=self.envelope(8, 1200))
            scheduler.run(jobs, lambda job: (time.sleep(0.02), Result(int(job["history_key"]), 120))[1])
            self.assertGreater(scheduler.peak_workers, 1)
            learned = runtime.ResourceScheduler(root, envelope=self.envelope(8, 1200))
            self.assertIn("0", learned.history)
            same = learned.estimate(jobs[0])[1]
            changed = learned.estimate({**jobs[0], "input_fingerprint": "changed", "memory_hint_mb": 300})[1]
            self.assertGreaterEqual(changed, same)

    def test_swap_or_major_fault_pressure_triggers_fast_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            jobs = [{"history_key": str(i), "input_fingerprint": "a", "memory_hint_mb": 50, "size": 0} for i in range(8)]
            samples = [(8 * 1024**3, 1024, 10), (8 * 1024**3, 1000, 11)] + [(8 * 1024**3, 1000, 11)] * 20
            scheduler = runtime.ResourceScheduler(Path(temp), envelope=self.envelope(4, 8192))
            with mock.patch.object(runtime, "_pressure_sample", side_effect=samples):
                scheduler.run(jobs, lambda job: (time.sleep(0.01), Result(int(job["history_key"]), 50))[1])
            self.assertGreaterEqual(scheduler.backoffs, 1)


if __name__ == "__main__":
    unittest.main()
