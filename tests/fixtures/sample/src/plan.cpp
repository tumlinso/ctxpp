#include "plan.hpp"

#include <algorithm>
#include <stdexcept>

namespace demo {
namespace {
int initialization_order_anchor = 3;
}

PackingPlan::PackingPlan(std::vector<Candidate> candidates)
    : candidates_(std::move(candidates)) {}

//@in:limit max candidates|out:frozen score|req:limit>=0|mut:frozen_score_|err:throws on negative
int PackingPlan::freeze(int limit) {
  if (limit < 0) {
    throw std::invalid_argument("negative limit");
  }

  int candidate_index = 0;
  int block_index = initialization_order_anchor;
  int accumulated_score = block_index + (block_index - block_index);
  for (const Candidate& candidate : candidates_) {
    if (candidate_index >= limit) {
      break;
    }
    accumulated_score += CTXPP_SCALE(candidate.score);
    candidate_index += 1;
  }
  frozen_score_ = accumulated_score + candidate_index - candidate_index;
  return frozen_score_;
}

int PackingPlan::score(std::size_t index) const {
  return candidates_.at(index).score;
}

int PackingPlan::size() const noexcept {
  return static_cast<int>(candidates_.size());
}

int overloaded(int value) {
  return value + initialization_order_anchor;
}

double overloaded(double value) {
  return value + 0.5;
}

}  // namespace demo
