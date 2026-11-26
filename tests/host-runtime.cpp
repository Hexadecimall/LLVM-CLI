// Hosted target-runtime validation: threads, TLS, exception ABI, and lifetime.
#include <iostream>
#include <stdexcept>
#include <thread>

thread_local int ThreadValue = 1;
struct Lifecycle {
  Lifecycle() { std::cout << "constructor\n"; }
  ~Lifecycle() { std::cout << "destructor\n"; }
} Instance;

int main() {
  int workerValue = 0;
  std::thread worker([&] { workerValue = ++ThreadValue; });
  worker.join();
  if (workerValue != 2 || ThreadValue != 1)
    return 1;
  try {
    throw std::runtime_error("host runtime passed");
  } catch (const std::exception &error) {
    std::cout << error.what() << '\n';
  }
}
