#pragma once

#include <cstddef>
#include <vector>

#define CTXPP_SCALE(x) ((x) * 2)

namespace demo {

struct Candidate {
  int block;
  int score;
};

class PlanBase {
 public:
  virtual ~PlanBase() = default;
  virtual int size() const noexcept = 0;
};

template <class T>
constexpr T clamp_value(T value, T low, T high) {
  return value < low ? low : (high < value ? high : value);
}

class PackingPlan final : public PlanBase {
 public:
  explicit PackingPlan(std::vector<Candidate> candidates);
  int freeze(int limit);
  int score(std::size_t index) const;
  int size() const noexcept override;

 private:
  std::vector<Candidate> candidates_;
  int frozen_score_ = 0;
};

int overloaded(int value);
double overloaded(double value);

}  // namespace demo
