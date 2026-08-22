# Context packets

`ctxpp packet TARGET` emits `CTXPP-CONTEXT-PACKET/1`, a bounded machine-readable routing packet. `ctxpp inspect TARGET` renders the same packet as a concise ordinary-language front door; add global `--json` to receive the packet instead.

Packets are evidence, never source authority. `target.location` names the exact canonical byte and line range, `target.content` is verbatim canonical text, and both the file and selected text carry SHA-256 hashes. Reopen that canonical range before editing. Generated `.ctxpp/` files remain read-only.

The packet contains compact types, dependencies, callers, callees, tests, nearby contract invariants, source identity, and trust metadata. `trust.relationships=semantic` means the selected target and its reported graph relationships came from a current semantic index. `lexical-or-partial` is useful for routing but must not be treated as semantic proof. `coverage.sufficient` is deliberately conservative: it is true only for a complete hash-verified semantic target with no reported omissions inside the request. A false value requires expansion or canonical inspection before an edit.

Use `--intent`, `--budget`, and `--max-items` to bound the request. The exact target and contract metadata are mandatory even when they exceed the requested budget; that case sets `coverage.budget_exceeded=true` and `coverage.sufficient=false`. Related items are removed deterministically to meet the budget and are always bounded by `max-items`; omissions are counted by category. `expansions` contains structured argv fragments for exact canonical source or a larger packet. The packet hash covers every field preceding `packet_hash` and is deterministic for a stable source/index state.

Examples:

```bash
ctxpp --json packet demo::PackingPlan::freeze --intent edit --budget 2400 --max-items 12
ctxpp inspect src/plan.cpp:15
ctxpp --json inspect c:@N@demo@S@PackingPlan@F@freeze#I#
```
