#include "plan.hpp"

namespace demo {

int call_overload() {
#if CTXPP_FAST
  return overloaded(4);
#else
  return overloaded(2);
#endif
}

auto make_incrementer(int base) {
  return [base](int value) { return base + value; };
}

}  // namespace demo
