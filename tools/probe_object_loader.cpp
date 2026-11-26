// Verify native object loading, static constructors, TLS, and destructors using
// LLVM's existing object linker. The probe's runtime and module are embedded.
#include "llvm/ExecutionEngine/Orc/ExecutionUtils.h"
#include "llvm/ExecutionEngine/Orc/LLJIT.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/raw_ostream.h"

#if defined(__APPLE__)
#include <mach-o/getsect.h>
#include <mach-o/loader.h>

extern const mach_header_64 _mh_execute_header;
#elif defined(__linux__)
extern "C" const char _binary_orc_runtime_a_start[], _binary_orc_runtime_a_end[];
extern "C" const char _binary_module_o_start[], _binary_module_o_end[];
#endif

static std::unique_ptr<llvm::MemoryBuffer> resource(const char *section) {
#if defined(__APPLE__)
  unsigned long size = 0;
  const auto *data = getsectiondata(&_mh_execute_header, "__LLVM", section, &size);
  if (!data || !size)
    return nullptr;
  return llvm::MemoryBuffer::getMemBufferCopy(
      llvm::StringRef(reinterpret_cast<const char *>(data), size), section);
#elif defined(__linux__)
  const bool runtime = llvm::StringRef(section) == "__orc";
  const char *start = runtime ? _binary_orc_runtime_a_start : _binary_module_o_start;
  const char *end = runtime ? _binary_orc_runtime_a_end : _binary_module_o_end;
  return llvm::MemoryBuffer::getMemBufferCopy(llvm::StringRef(start, end - start), section);
#else
#error Unsupported probe host
#endif
}

int main(int argc, char **argv) {
  llvm::InitializeNativeTarget();
  llvm::InitializeNativeTargetAsmPrinter();
  auto runtime = resource("__orc");
  auto module = resource("__module");
  if (!runtime || !module) {
    llvm::errs() << "missing embedded runtime or module\n";
    return 1;
  }
  auto instance = llvm::orc::LLJITBuilder()
      .setPlatformSetUp(llvm::orc::ExecutorNativePlatform(std::move(runtime)))
      .create();
  if (!instance) {
    llvm::logAllUnhandledErrors(instance.takeError(), llvm::errs(), "loader: ");
    return 1;
  }
  auto &jit = **instance;
  auto &library = jit.getMainJITDylib();
  if (auto error = jit.addObjectFile(library, std::move(module))) {
    llvm::logAllUnhandledErrors(std::move(error), llvm::errs(), "object: ");
    return 1;
  }
  auto entry = jit.lookup("LLVM_probe_main");
  if (!entry) {
    llvm::logAllUnhandledErrors(entry.takeError(), llvm::errs(), "entry: ");
    return 1;
  }
  if (auto error = jit.initialize(library)) {
    llvm::logAllUnhandledErrors(std::move(error), llvm::errs(), "initialize: ");
    return 1;
  }
  const int result = entry->toPtr<int (*)(int, char **)>()(argc, argv);
  if (auto error = jit.deinitialize(library)) {
    llvm::logAllUnhandledErrors(std::move(error), llvm::errs(), "deinitialize: ");
    return 1;
  }
  llvm::outs() << "object entry returned " << result << '\n';
  return result == 42 ? 0 : 1;
}
