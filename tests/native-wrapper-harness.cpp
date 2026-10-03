#include "native-wrappers.h"

#include <string>

int main(int argc, char **argv) {
  if (argc < 2)
    return 2;
  const int result = runNativeWrapper(argv[1], argc, argv, 2);
  return result < 0 ? 127 : result;
}
