#include "incremental-build.h"

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <set>
#include <string>
#include <system_error>
#include <vector>

namespace {

namespace fs = std::filesystem;

std::string readText(const fs::path &path) {
  std::ifstream input(path);
  if (!input)
    return {};
  return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

bool writeText(const fs::path &path, const std::string &value) {
  std::ofstream output(path, std::ios::trunc);
  output << value;
  return static_cast<bool>(output);
}

std::uint64_t pathHash(std::string_view text) {
  std::uint64_t hash = 14695981039346656037ULL;
  for (unsigned char character : text) {
    hash ^= character;
    hash *= 1099511628211ULL;
  }
  return hash;
}

std::vector<fs::path> readDependencies(const fs::path &path) {
  const std::string content = readText(path);
  const std::size_t colon = content.find(':');
  if (colon == std::string::npos)
    return {};
  std::vector<fs::path> result;
  std::string token;
  for (std::size_t index = colon + 1; index < content.size(); ++index) {
    const char character = content[index];
    if (character == '\\' && index + 1 < content.size()) {
      if (content[index + 1] != '\n')
        token += content[index + 1];
      ++index;
    } else if (std::isspace(static_cast<unsigned char>(character))) {
      if (!token.empty()) {
        result.emplace_back(std::move(token));
        token.clear();
      }
    } else {
      token += character;
    }
  }
  if (!token.empty())
    result.emplace_back(std::move(token));
  return result;
}

bool newerOrMissing(const fs::path &dependency, fs::file_time_type built) {
  std::error_code error;
  const auto modified = fs::last_write_time(dependency, error);
  return error || modified > built;
}

bool needsCompile(const fs::path &object, const fs::path &depfile,
                  const fs::path &keyfile, const std::string &key) {
  std::error_code error;
  const auto built = fs::last_write_time(object, error);
  if (error || readText(keyfile) != key)
    return true;
  const auto dependencies = readDependencies(depfile);
  if (dependencies.empty())
    return true;
  for (const fs::path &dependency : dependencies)
    if (newerOrMissing(dependency, built))
      return true;
  return false;
}

std::string cacheKey(const std::vector<std::string> &parts,
                     const fs::path &executable) {
  std::string key;
  for (const std::string &part : parts)
    key += std::to_string(part.size()) + ":" + part;
  std::error_code error;
  const auto modified = fs::last_write_time(executable, error);
  if (error)
    return key + "|unknown-executable";
  const auto size = fs::file_size(executable, error);
  if (error)
    return key + "|unknown-executable";
  return key + "|" + std::to_string(static_cast<long long>(
      modified.time_since_epoch().count())) + ":" + std::to_string(size);
}

bool isSource(const fs::path &path) {
  const std::string extension = path.extension().string();
  return extension == ".c" || extension == ".cc" || extension == ".cpp" ||
         extension == ".cxx" || extension == ".C" || extension == ".m" ||
         extension == ".mm" || extension == ".s" || extension == ".S";
}

bool usesCxx(const fs::path &path) {
  const std::string extension = path.extension().string();
  return extension == ".cc" || extension == ".cpp" ||
         extension == ".cxx" || extension == ".C" || extension == ".mm";
}

void printUsage() {
  std::cout <<
      "Usage: llvm build [options] <source.c|source.cpp|source.s>...\n"
      "  --target TARGET     Cross target, such as x86_64-linux-musl\n"
      "  --output PATH       Linked executable (default: a.out)\n"
      "  --build-dir PATH    Durable objects (default: .llvm-cli-build)\n"
      "  --release           Compile with -O3 -flto=thin\n"
      "  --cflag FLAG        Extra compiler flag; repeat as needed\n"
      "  --ldflag FLAG       Extra linker flag; repeat as needed\n";
}

} // namespace

int runIncrementalBuild(int argc, char **argv, int firstArgument,
                        std::string_view executablePath,
                        const BuildInvoke &invoke,
                        std::string_view compiler) {
  std::string target;
  fs::path output = "a.out";
  fs::path buildDirectory = ".llvm-cli-build";
  bool release = false;
  std::vector<std::string> compilerFlags;
  std::vector<std::string> linkerFlags;
  std::vector<fs::path> sources;
  std::set<fs::path> seenSources;
  for (int index = firstArgument; index < argc; ++index) {
    const std::string_view option(argv[index]);
    if (option == "--help" || option == "-h") {
      printUsage();
      return 0;
    }
    if (option == "--release") {
      release = true;
      continue;
    }
    const std::size_t equal = option.find('=');
    const std::string_view name = option.substr(0, equal);
    if (name == "--target" || name == "--output" ||
        name == "--build-dir" || name == "--cflag" ||
        name == "--ldflag") {
      if (equal == std::string_view::npos && index + 1 >= argc) {
        std::cerr << "LLVM build: missing value for " << name << '\n';
        return 2;
      }
      const std::string value = equal == std::string_view::npos
          ? argv[++index] : std::string(option.substr(equal + 1));
      if (value.empty()) {
        std::cerr << "LLVM build: empty value for " << name << '\n';
        return 2;
      }
      if (name == "--target") target = value;
      else if (name == "--output") output = value;
      else if (name == "--build-dir") buildDirectory = value;
      else if (name == "--cflag") compilerFlags.push_back(value);
      else linkerFlags.push_back(value);
      continue;
    }
    if (option.starts_with('-')) {
      std::cerr << "LLVM build: unknown option " << option << '\n';
      return 2;
    }
    const fs::path source = fs::absolute(argv[index]).lexically_normal();
    if (!isSource(source) || !fs::is_regular_file(source)) {
      std::cerr << "LLVM build: expected an existing C, C++, Objective-C, "
                   "or assembly source: " << source << '\n';
      return 2;
    }
    if (seenSources.insert(source).second)
      sources.push_back(source);
  }
  if (sources.empty()) {
    printUsage();
    return 2;
  }

  std::error_code error;
  buildDirectory = fs::absolute(buildDirectory).lexically_normal();
  output = fs::absolute(output).lexically_normal();
  fs::create_directories(buildDirectory, error);
  if (error) {
    std::cerr << "LLVM build: cannot create " << buildDirectory << ": "
              << error.message() << '\n';
    return 1;
  }
  fs::create_directories(output.parent_path(), error);
  if (error) {
    std::cerr << "LLVM build: cannot create output directory: "
              << error.message() << '\n';
    return 1;
  }
  const fs::path executable(executablePath);
  std::vector<fs::path> objects;
  bool rebuilt = false;
  bool linkCxx = false;
  for (const fs::path &source : sources) {
    const bool cxx = compiler.empty() ? usesCxx(source) :
                     compiler == "clang++";
    linkCxx |= cxx;
    char suffix[17]{};
    std::snprintf(suffix, sizeof(suffix), "%016llx",
                  static_cast<unsigned long long>(pathHash(source.string())));
    const fs::path object = buildDirectory /
        (source.stem().string() + "-" + suffix + ".o");
    const fs::path depfile = object.string() + ".d";
    const fs::path keyfile = object.string() + ".key";
    objects.push_back(object);
    std::vector<std::string> command{"-c", source.string(), "-o",
                                      object.string(), "-MMD", "-MF",
                                      depfile.string()};
    command.push_back(release ? "-O3" : "-O0");
    if (release) command.push_back("-flto=thin");
    command.insert(command.end(), compilerFlags.begin(), compilerFlags.end());
    const std::string key = cacheKey(command, executable) + "|" + target +
                            "|" + (cxx ? "c++" : "cc");
    if (!needsCompile(object, depfile, keyfile, key)) {
      std::cout << "build: unchanged " << source.string() << '\n';
      continue;
    }
    std::cout << "build: compile " << source.string() << '\n' << std::flush;
    const int status = invoke(compiler.empty() ?
        (cxx ? "clang++" : "clang") : compiler, target, command);
    if (status != 0)
      return status;
    if (!fs::is_regular_file(object)) {
      std::cerr << "LLVM build: compiler did not create " << object << '\n';
      return 1;
    }
    if (!writeText(keyfile, key)) {
      std::cerr << "LLVM build: cannot write " << keyfile << '\n';
      return 1;
    }
    rebuilt = true;
  }

  std::vector<std::string> linkCommand;
  for (const fs::path &object : objects)
    linkCommand.push_back(object.string());
  linkCommand.push_back("-o");
  linkCommand.push_back(output.string());
  if (release) linkCommand.push_back("-flto=thin");
  linkCommand.insert(linkCommand.end(), linkerFlags.begin(), linkerFlags.end());
  const fs::path linkKeyFile = buildDirectory / "link.key";
  const std::string linkKey = cacheKey(linkCommand, executable) + "|" +
                              target + "|" + (linkCxx ? "c++" : "cc");
  const auto linked = fs::last_write_time(output, error);
  bool relink = rebuilt || error || readText(linkKeyFile) != linkKey;
  if (!relink)
    for (const fs::path &object : objects)
      relink |= newerOrMissing(object, linked);
  if (!relink) {
    std::cout << "build: up to date " << output.string() << '\n';
    return 0;
  }
  std::cout << "build: link " << output.string() << '\n' << std::flush;
  const int status = invoke(compiler.empty() ?
      (linkCxx ? "clang++" : "clang") : compiler, target, linkCommand);
  if (status != 0)
    return status;
  if (!writeText(linkKeyFile, linkKey)) {
    std::cerr << "LLVM build: cannot write " << linkKeyFile << '\n';
    return 1;
  }
  return 0;
}

int runIncrementalClang(int argc, char **argv, int firstArgument,
                        std::string_view compiler, std::string_view target,
                        std::string_view executable,
                        const BuildInvoke &invoke) {
  fs::path buildDirectory = ".llvm-cli-build";
  fs::path output;
  bool compileOnly = false;
  std::vector<std::string> compilerFlags;
  std::vector<std::string> linkerFlags;
  std::vector<std::string> original;
  std::vector<std::string> sources;
  for (int index = firstArgument; index < argc; ++index) {
    const std::string_view option(argv[index]);
    if (option == "-incremental" || option == "--incremental")
      continue;
    if (option == "-build-dir" || option == "--build-dir") {
      if (++index >= argc || !*argv[index]) {
        std::cerr << "LLVM: -build-dir requires a path\n";
        return 2;
      }
      buildDirectory = argv[index];
      continue;
    }
    if (option == "-o") {
      if (++index >= argc || !*argv[index]) {
        std::cerr << "LLVM: -o requires a path\n";
        return 2;
      }
      output = argv[index];
      original.emplace_back("-o");
      original.emplace_back(argv[index]);
      continue;
    }
    if (option == "-c") {
      compileOnly = true;
      original.emplace_back(option);
      continue;
    }
    if (option == "-I" || option == "-D" || option == "-U" ||
        option == "-isystem" || option == "-include" ||
        option == "-iquote" || option == "-F" || option == "-iframework" ||
        option == "-L" || option == "-l" || option == "-Xlinker" ||
        option == "-framework") {
      if (++index >= argc) {
        std::cerr << "LLVM: " << option << " requires a value\n";
        return 2;
      }
      const std::string value(argv[index]);
      const bool linkOnly = option == "-L" || option == "-l" ||
          option == "-Xlinker" || option == "-framework";
      auto &flags = linkOnly ? linkerFlags : compilerFlags;
      flags.emplace_back(option);
      flags.push_back(value);
      original.emplace_back(option);
      original.push_back(value);
      continue;
    }
    if (option.starts_with("-L") || option.starts_with("-l") ||
        option.starts_with("-Wl,") || option.starts_with("-fuse-ld=") ||
        option == "-static" || option == "-shared" ||
        option == "-pie" || option == "-no-pie" ||
        option == "-nostdlib" || option == "-nodefaultlibs" ||
        option == "-nostartfiles" || option == "-rdynamic" ||
        option == "-dynamic") {
      linkerFlags.emplace_back(option);
      original.emplace_back(option);
      continue;
    }
    if (option == "-pthread" || option.starts_with("-flto") ||
        option.starts_with("-fsanitize=") ||
        option.starts_with("-stdlib=")) {
      compilerFlags.emplace_back(option);
      linkerFlags.emplace_back(option);
      original.emplace_back(option);
      continue;
    }
    if (option.starts_with("-I") || option.starts_with("-D") ||
        option.starts_with("-U") || option.starts_with("-F") ||
        option.starts_with("-O") || option.starts_with("-g") ||
        option.starts_with("-std=") || option.starts_with("-f") ||
        option.starts_with("-m") || option.starts_with("-W") ||
        option == "-pedantic" || option == "-pedantic-errors" ||
        option == "-pipe") {
      compilerFlags.emplace_back(option);
      original.emplace_back(option);
      continue;
    }
    if (option.starts_with('-')) {
      std::cerr << "LLVM: unsupported incremental compiler option '"
                << option << "'; run without -incremental for the full "
                   "Clang driver\n";
      return 2;
    }
    const fs::path source = fs::absolute(option).lexically_normal();
    if (!isSource(source) || !fs::is_regular_file(source)) {
      std::cerr << "LLVM: -incremental expects source files, not '"
                << option << "'; run without -incremental for other "
                   "link inputs\n";
      return 2;
    }
    sources.push_back(source.string());
    original.emplace_back(option);
  }
  if (sources.empty()) {
    std::cerr << "LLVM: -incremental requires a source file\n";
    return 2;
  }
  if (compileOnly) {
    if (sources.size() != 1) {
      std::cerr << "LLVM: -incremental -c currently requires one source "
                   "per command\n";
      return 2;
    }
    if (output.empty())
      output = fs::path(sources.front()).stem().string() + ".o";
    output = fs::absolute(output).lexically_normal();
    buildDirectory = fs::absolute(buildDirectory).lexically_normal();
    std::error_code error;
    fs::create_directories(buildDirectory, error);
    if (!error)
      fs::create_directories(output.parent_path(), error);
    if (error) {
      std::cerr << "LLVM: cannot create incremental build directory: "
                << error.message() << '\n';
      return 1;
    }
    char suffix[17]{};
    std::snprintf(suffix, sizeof(suffix), "%016llx",
                  static_cast<unsigned long long>(pathHash(
                      sources.front() + output.string())));
    const fs::path depfile = buildDirectory / (std::string(suffix) + ".d");
    const fs::path keyfile = buildDirectory / (std::string(suffix) + ".key");
    std::vector<std::string> command = original;
    if (std::find(command.begin(), command.end(), "-o") == command.end()) {
      command.emplace_back("-o");
      command.push_back(output.string());
    }
    command.emplace_back("-MMD");
    command.emplace_back("-MF");
    command.push_back(depfile.string());
    const std::string key = cacheKey(command, executable) + "|" +
                            std::string(target) + "|" + std::string(compiler);
    if (!needsCompile(output, depfile, keyfile, key)) {
      std::cout << "build: unchanged " << sources.front() << '\n';
      return 0;
    }
    std::cout << "build: compile " << sources.front() << '\n' << std::flush;
    const int status = invoke(compiler, target, command);
    if (status != 0)
      return status;
    if (!fs::is_regular_file(output) || !writeText(keyfile, key)) {
      std::cerr << "LLVM: incremental compiler output is missing or "
                   "cannot be recorded\n";
      return 1;
    }
    return 0;
  }
  std::vector<std::string> translated{"llvm", "build", "--build-dir",
                                      buildDirectory.string()};
  if (!target.empty()) {
    translated.emplace_back("--target");
    translated.emplace_back(target);
  }
  if (!output.empty()) {
    translated.emplace_back("--output");
    translated.emplace_back(output.string());
  }
  for (const std::string &flag : compilerFlags) {
    translated.emplace_back("--cflag");
    translated.push_back(flag);
  }
  for (const std::string &flag : linkerFlags) {
    translated.emplace_back("--ldflag");
    translated.push_back(flag);
  }
  translated.insert(translated.end(), sources.begin(), sources.end());
  std::vector<char *> arguments;
  for (std::string &argument : translated)
    arguments.push_back(argument.data());
  return runIncrementalBuild(static_cast<int>(arguments.size()),
                             arguments.data(), 2, executable, invoke,
                             compiler);
}
