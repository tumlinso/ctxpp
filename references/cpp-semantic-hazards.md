# C++ semantic hazard gates

Reject or escalate a transform when its proof does not explicitly cover:

- overload resolution, ADL/name lookup, templates/dependent names, concepts/requires, `decltype`, unevaluated operands;
- cv/ref qualifiers, value categories, temporary materialization/lifetime, copy elision, narrowing, initialization form, aggregates, constructor versus assignment;
- short-circuiting, sequencing/evaluation order, exceptions/`noexcept`, coroutines, volatile/atomic access, aliasing, placement construction;
- macros, stringification/token pasting, conditional state, pragmas, source-location-sensitive code, include order;
- namespace initialization order, inline/ODR, linkage/visibility, RTTI/names, reflection/registration, schemas, bindings, test/benchmark registration;
- sanitizers/instrumentation, compiler attributes/extensions, modules;
- CUDA qualifiers, launches, address spaces, synchronization, cooperative/warp semantics, specialization, device linking, occupancy/register/memory-layout sensitivity.

Expansion ranges are never writable as spelled source. Headers require agreement across all observed translation units/configurations. Mark unsupported constructs opaque and include them verbatim when required.

For CUDA, require a faithful compilation database and parser. Preserve `__host__`, `__device__`, `__global__`, `__shared__`, `__constant__`, launch bounds, kernel syntax/configuration, synchronization, generated code, and device-link behavior. Otherwise disable mutation and use ordinary inspection.
