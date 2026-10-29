# Evaluation

Use `evals/prompts.csv` and `evals/run_evals.py`. Compare canonical-only, slicing, slicing+view, sharding, and explicit model-profile compaction where supported.

Measure deterministic task success proxies, build/tests, loaded source tokens, bundle tokens, file reads/hops, tool calls, unnecessary rereads, changed-file scope, protected API changes, retries, and recovery. Treat unavailable measurements as unavailable, never zero.

Cover locating, explaining nonlocal invariants, one-function and cross-shard edits, tests, diagnostics, performance-sensitive work, authoring, monolith audit, explicit compaction, ordinary work with no rewrite, and unconfigured non-triggering.

Initial acceptance: no correctness loss; materially lower median loaded context; no substantial navigation thrash; no implicit mutation; no API/ABI/build/test regression. A 20-30% representative context reduction is useful, not universal. Save failures as regression prompts.

Run deterministic graders in `evals/graders/`; they inspect artifacts and command traces rather than subjective prose.
