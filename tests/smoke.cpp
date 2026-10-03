#include <iostream>
#include <vector>

int main() {
  std::vector<int> values{20, 22};
  std::cout << "LLVM-CLI C++ smoke " << values[0] + values[1] << '\n';
}
