# Compact comment contracts

Use fields in canonical order:

`in | out | req | ens | inv | mut | own | thr | sync | err | cost | num | abbr | why`

Examples:

```cpp
//@in:g CSR|req:rp asc,rp[n]=nnz|out:p perm|inv:sum(nnz)|mut:none|cost:O(nnz)
//@abbr:bi=block idx,ci=candidate idx,d=delta cost|out:d|req:ci<nc
```

Unicode notation (`->`, flow; empty; delta; sum; all/exists; bounds) is allowed only when configured and measured. ASCII is always valid.

Retain non-obvious invariants, units/domains/NaN/overflow, ownership/lifetime/aliasing, threading/synchronization, mutation, failures, algorithmic rationale, performance/hardware constraints, and ordering. Remove syntax narration. Keep a compact `why:` clause rather than turning complex rationale into riddles.

The deterministic linter validates field names/order, duplicates, separators, and abbreviation syntax; it does not summarize prose. Preserve licenses, copyright, Doxygen directives, NOLINT/IWYU/format/coverage controls, generated markers, build/test controls, and quoted user-facing text.
