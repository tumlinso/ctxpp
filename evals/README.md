# Evaluation harness

Run from the skill directory:

```bash
python evals/run_evals.py --output tests/expected/eval-report.json
python evals/graders/grade_report.py tests/expected/eval-report.json
python evals/run_demo.py --output tests/expected/demo-report.json
```

The harness copies the semantic fixture to temporary state, builds it, scans it, and compares canonical-file context with budgeted slices and compact views over the prompt set. It checks target resolution, artifact generation, estimated file reads/hops, token deltas, and absence of canonical mutation. Explicit compaction and sharding prompts exercise routing in this small V1 harness; transactional behavior is covered by integration tests.

The checked-in report is deterministic for the bundled external tokenizer and current fixture. Regenerate it after fixture, ranking, mapping, or tokenization changes.
