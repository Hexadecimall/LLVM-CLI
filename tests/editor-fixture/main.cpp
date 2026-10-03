#include "include/example.hpp"
#include <vector>

int main() {
  std::vector<int> values{embedded_header_answer()};
  return values.front() - 42;
}
