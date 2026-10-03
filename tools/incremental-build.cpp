#include "incremental-build.h"

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
                        const BuildInvoke &invoke) {
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
    const bool cxx = usesCxx(source);
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
    const int status = invoke(cxx ? "clang++" : "clang", target, command);
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
  const int status = invoke(linkCxx ? "clang++" : "clang", target,
                            linkCommand);
  if (status != 0)
    return status;
  if (!writeText(linkKeyFile, linkKey)) {
    std::cerr << "LLVM build: cannot write " << linkKeyFile << '\n';
    return 1;
  }
  return 0;
}
