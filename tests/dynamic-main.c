#include <stdio.h>

int llvm_cli_answer(void);

int main(void) {
  printf("LLVM-CLI dynamic link %d\n", llvm_cli_answer());
  return llvm_cli_answer() == 42 ? 0 : 1;
}
