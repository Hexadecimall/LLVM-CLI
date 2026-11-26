// Exercise the patched upstream depfile writer without changing its module graph.
#include "clang/Frontend/DependencyOutputOptions.h"
#include "clang/Frontend/Utils.h"
#include "llvm/Support/raw_ostream.h"
#include <cstring>

static bool Embedded = true;
struct Generator : clang::DependencyFileGenerator {
  using clang::DependencyFileGenerator::DependencyFileGenerator;
  using clang::DependencyCollector::addDependency;
  void output(llvm::raw_ostream &stream) { outputDependencyFile(stream); }
};
extern "C" const char *LLVMExecutablePath() {
  return Embedded ? "/compiler path/llvm#test" : nullptr;
}

int main(int argc, char **argv) {
  clang::DependencyOutputOptions options;
  options.Targets.push_back("output.o");
  options.UsePhonyTargets = true;
  if (argc > 1) {
    Embedded = std::strcmp(argv[1], "standalone") != 0;
    options.AddMissingHeaderDeps = std::strcmp(argv[1], "missing") == 0;
  }
  Generator generator(options);
  for (const char *file : {"source.cpp", "/__llvm/one.h", "user header.h", "/__llvm/two.h"})
    generator.addDependency(file);
  generator.output(llvm::outs());
  const auto &graph = generator.getDependencies();
  return graph.size() == 4 && graph[1] == "/__llvm/one.h" ? 0 : 1;
}
