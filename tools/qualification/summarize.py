#!/usr/bin/env python3
"""Summarize probe traces; delivered bytes, not raw output, are context."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median

parser = argparse.ArgumentParser()
parser.add_argument("results_dir", type=Path)
args = parser.parse_args()
conditions = {}

for label in ("baseline", "candidate"):
    task_rows = []
    for task in "123":
        path = args.results_dir / f"{label}-{task}-capped.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        task_rows.append({
            "task": task,
            "delivered": sum(row["delivered_bytes"] for row in rows),
            "raw": sum(row["raw_output_bytes"] for row in rows),
            "omitted": sum(row.get("omitted_bytes", 0) for row in rows),
            "calls": len(rows),
        })
    conditions[label] = task_rows
    print(label)
    for row in task_rows:
        print(
            "  task {task}: delivered={delivered}B raw={raw}B "
            "omitted={omitted}B calls={calls}".format(**row)
        )
    print(
        "  median delivered: {} B".format(
            median(row["delivered"] for row in task_rows)
        )
    )

baseline = median(row["delivered"] for row in conditions["baseline"])
candidate = median(row["delivered"] for row in conditions["candidate"])
print("median delivered reduction: {:.1%}".format(1 - candidate / baseline))
print(
    "Totals include every probe-delivered stdout response, including git "
    "diff/status output. Raw and omitted bytes are diagnostic only."
)
