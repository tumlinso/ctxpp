#include "plan.hpp"

#include <cassert>

int main() {
  demo::PackingPlan plan({{0, 2}, {1, 3}});
  assert(plan.size() == 2);
  assert(plan.freeze(2) == 13);
  assert(plan.score(1) == 3);
  assert(demo::clamp_value(8, 0, 5) == 5);
  return 0;
}
