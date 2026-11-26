#include <cstdio>
#include <pthread.h>

static int Initialized = 0;
thread_local int ThreadValue = 1;
struct Lifecycle {
  Lifecycle() {
    std::puts("embedded constructor ran");
    Initialized = 41;
  }
  ~Lifecycle() { std::puts("embedded destructor ran"); }
};
static Lifecycle Instance;

static void *checkThread(void *value) {
  auto *result = static_cast<int *>(value);
  *result = ThreadValue;
  ThreadValue = 7;
  return nullptr;
}

extern "C" int LLVM_probe_main(int, char **) {
  for (int i = 0; i != 2; ++i) {
    int initial = 0;
    pthread_t thread;
    if (pthread_create(&thread, nullptr, checkThread, &initial) ||
        pthread_join(thread, nullptr) || initial != 1)
      return -1;
  }
  return Initialized + ThreadValue;
}
