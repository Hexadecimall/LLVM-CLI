// Self-contained LLVM toolchain dispatcher.
// The complete toolchain is stored in the __LLVM,__pack Mach-O section and is
// materialized into a content-addressed cache before a tool is executed.

#include "pack-format.h"

#include <zstd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <mach-o/dyld.h>
#include <mach-o/getsect.h>
#include <mach-o/loader.h>
#include <sys/stat.h>
#include <unistd.h>

namespace fs = std::filesystem;

extern const mach_header_64 _mh_execute_header;

namespace {

struct ByteView {
  const std::uint8_t *Data{};
  std::size_t Size{};

  bool take(void *destination, std::size_t amount) {
    if (amount > Size)
      return false;
    std::memcpy(destination, Data, amount);
    Data += amount;
    Size -= amount;
    return true;
  }

  bool skip(std::size_t amount) {
    if (amount > Size)
      return false;
    Data += amount;
    Size -= amount;
    return true;
  }
};

class LockDirectory {
public:
  explicit LockDirectory(fs::path path) : Path(std::move(path)) {}
  ~LockDirectory() {
    if (Owned) {
      std::error_code ignored;
      fs::remove(Path, ignored);
    }
  }

  bool acquire(const fs::path &completionMarker) {
    for (int attempt = 0; attempt != 6000; ++attempt) {
      if (::mkdir(Path.c_str(), 0700) == 0) {
        Owned = true;
        return true;
      }
      if (errno != EEXIST) {
        std::cerr << "LLVM: cannot create extraction lock: "
                  << std::strerror(errno) << '\n';
        return false;
      }
      if (fs::exists(completionMarker))
        return true;
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    std::cerr << "LLVM: timed out waiting for toolchain extraction\n";
    return false;
  }

  bool ownsLock() const { return Owned; }

private:
  fs::path Path;
  bool Owned = false;
};

bool validRelativePath(std::string_view path) {
  if (path.empty() || path.front() == '/')
    return false;
  fs::path parsed(path);
  for (const fs::path &component : parsed) {
    if (component == "..")
      return false;
  }
  return true;
}

bool writeFile(const fs::path &path, const std::vector<char> &contents,
               std::uint32_t mode) {
  const int descriptor =
      ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL, mode & 07777);
  if (descriptor < 0) {
    std::cerr << "LLVM: cannot create " << path << ": "
              << std::strerror(errno) << '\n';
    return false;
  }

  std::size_t offset = 0;
  while (offset < contents.size()) {
    const ssize_t result =
        ::write(descriptor, contents.data() + offset, contents.size() - offset);
    if (result <= 0) {
      std::cerr << "LLVM: write failed for " << path << ": "
                << std::strerror(errno) << '\n';
      ::close(descriptor);
      return false;
    }
    offset += static_cast<std::size_t>(result);
  }

  if (::fchmod(descriptor, mode & 07777) != 0 || ::close(descriptor) != 0) {
    std::cerr << "LLVM: finalizing " << path << " failed: "
              << std::strerror(errno) << '\n';
    return false;
  }
  return true;
}

std::string hashString(std::uint64_t value) {
  char buffer[17]{};
  std::snprintf(buffer, sizeof(buffer), "%016llx",
                static_cast<unsigned long long>(value));
  return buffer;
}

fs::path cacheBase() {
  if (const char *overridePath = std::getenv("LLVM_CACHE_DIR")) {
    if (*overridePath)
      return overridePath;
  }
  if (const char *home = std::getenv("HOME")) {
    if (*home)
      return fs::path(home) / "Library" / "Caches" / "LLVM";
  }
  return fs::temp_directory_path() /
         ("LLVM-" + std::to_string(static_cast<unsigned long>(::getuid())));
}

bool unpack(ByteView archive, const fs::path &destination,
            llvm_pack::Header &header) {
  if (!archive.take(&header, sizeof(header)) ||
      std::memcmp(header.MagicBytes, llvm_pack::Magic,
                  sizeof(header.MagicBytes)) != 0 ||
      header.VersionNumber != llvm_pack::Version) {
    std::cerr << "LLVM: embedded toolchain archive is invalid\n";
    return false;
  }

  std::error_code error;
  fs::create_directories(destination, error);
  if (error) {
    std::cerr << "LLVM: cannot create extraction directory: "
              << error.message() << '\n';
    return false;
  }

  std::vector<char> stored;
  std::vector<char> original;

  for (std::uint32_t index = 0; index < header.EntryCount; ++index) {
    llvm_pack::EntryHeader entry{};
    if (!archive.take(&entry, sizeof(entry)) || entry.PathSize > archive.Size) {
      std::cerr << "LLVM: truncated archive entry\n";
      return false;
    }

    std::string relative(reinterpret_cast<const char *>(archive.Data),
                         entry.PathSize);
    if (!archive.skip(entry.PathSize) || !validRelativePath(relative) ||
        entry.StoredSize > archive.Size ||
        entry.OriginalSize > static_cast<std::uint64_t>(SIZE_MAX)) {
      std::cerr << "LLVM: unsafe or corrupt archive entry\n";
      return false;
    }

    const fs::path outputPath = destination / fs::path(relative);
    fs::create_directories(outputPath.parent_path(), error);
    if (error) {
      std::cerr << "LLVM: cannot create parent directory for " << relative
                << ": " << error.message() << '\n';
      return false;
    }

    if (entry.Type == llvm_pack::EntryType::Directory) {
      fs::create_directories(outputPath, error);
      if (error || ::chmod(outputPath.c_str(), entry.Mode & 07777) != 0) {
        std::cerr << "LLVM: cannot create directory " << relative << '\n';
        return false;
      }
      continue;
    }

    stored.assign(reinterpret_cast<const char *>(archive.Data),
                  reinterpret_cast<const char *>(archive.Data + entry.StoredSize));
    archive.skip(static_cast<std::size_t>(entry.StoredSize));
    original.resize(static_cast<std::size_t>(entry.OriginalSize));
    if (!original.empty()) {
      const std::size_t result =
          ZSTD_decompress(original.data(), original.size(), stored.data(),
                          stored.size());
      if (ZSTD_isError(result) || result != original.size()) {
        std::cerr << "LLVM: decompression failed for " << relative << '\n';
        return false;
      }
    } else if (!stored.empty()) {
      std::cerr << "LLVM: corrupt empty archive entry\n";
      return false;
    }

    if (entry.Type == llvm_pack::EntryType::File) {
      if (!writeFile(outputPath, original, entry.Mode))
        return false;
    } else if (entry.Type == llvm_pack::EntryType::Symlink) {
      const std::string target(original.begin(), original.end());
      if (::symlink(target.c_str(), outputPath.c_str()) != 0) {
        std::cerr << "LLVM: cannot create symlink " << relative << ": "
                  << std::strerror(errno) << '\n';
        return false;
      }
    } else {
      std::cerr << "LLVM: unknown archive entry type\n";
      return false;
    }
  }

  return true;
}

std::pair<const std::uint8_t *, std::size_t> embeddedArchive() {
  unsigned long size = 0;
  const std::uint8_t *data = getsectiondata(
      &_mh_execute_header, "__LLVM", "__pack", &size);
  return {data, static_cast<std::size_t>(size)};
}

fs::path materializeToolchain() {
  const auto [data, size] = embeddedArchive();
  if (!data || size < sizeof(llvm_pack::Header)) {
    std::cerr << "LLVM: embedded toolchain section is missing\n";
    return {};
  }

  llvm_pack::Header header{};
  std::memcpy(&header, data, sizeof(header));
  if (std::memcmp(header.MagicBytes, llvm_pack::Magic,
                  sizeof(header.MagicBytes)) != 0 ||
      header.VersionNumber != llvm_pack::Version) {
    std::cerr << "LLVM: embedded toolchain header is invalid\n";
    return {};
  }

  const fs::path base = cacheBase();
  const fs::path root = base / hashString(header.ContentHash);
  const fs::path marker = root / ".complete";
  if (fs::exists(marker))
    return root;

  std::error_code error;
  fs::create_directories(base, error);
  if (error) {
    std::cerr << "LLVM: cannot create cache: " << error.message() << '\n';
    return {};
  }

  LockDirectory lock(base / (hashString(header.ContentHash) + ".lock"));
  if (!lock.acquire(marker))
    return {};
  if (!lock.ownsLock())
    return root;
  if (fs::exists(marker))
    return root;

  const fs::path staging =
      base / ("." + hashString(header.ContentHash) + ".tmp." +
              std::to_string(static_cast<unsigned long>(::getpid())));
  fs::remove_all(staging, error);
  error.clear();

  std::cerr << "LLVM: unpacking the embedded toolchain (first run only)...\n";
  llvm_pack::Header unpackedHeader{};
  if (!unpack({data, size}, staging, unpackedHeader)) {
    fs::remove_all(staging, error);
    return {};
  }

  if (!writeFile(staging / ".complete", {}, 0644)) {
    fs::remove_all(staging, error);
    return {};
  }

  fs::rename(staging, root, error);
  if (error && !fs::exists(marker)) {
    std::cerr << "LLVM: cannot publish extracted toolchain: "
              << error.message() << '\n';
    fs::remove_all(staging, error);
    return {};
  }
  if (error)
    fs::remove_all(staging, error);
  std::cerr << "LLVM: toolchain ready at " << root << '\n';
  return root;
}

std::string baseName(const char *path) {
  return fs::path(path ? path : "LLVM").filename().string();
}

bool executableFile(const fs::path &path) {
  std::error_code error;
  return fs::is_regular_file(path, error) && ::access(path.c_str(), X_OK) == 0;
}

void listTools(const fs::path &root) {
  std::vector<std::string> tools;
  std::error_code error;
  for (fs::directory_iterator iterator(root / "bin", error), end;
       iterator != end && !error; iterator.increment(error)) {
    if (executableFile(iterator->path()) || fs::is_symlink(iterator->symlink_status()))
      tools.push_back(iterator->path().filename().string());
  }
  std::ranges::sort(tools);
  tools.erase(std::unique(tools.begin(), tools.end()), tools.end());
  for (const std::string &tool : tools)
    std::cout << tool << '\n';
}

void prependEnvironmentPath(const char *name, const fs::path &path) {
  std::string value = path.string();
  if (const char *existing = std::getenv(name); existing && *existing) {
    value.push_back(':');
    value.append(existing);
  }
  ::setenv(name, value.c_str(), 1);
}

void configureAppleSdk() {
  if (const char *existing = std::getenv("SDKROOT");
      existing && *existing && fs::is_directory(existing))
    return;

  FILE *command = ::popen("/usr/bin/xcrun --sdk macosx --show-sdk-path", "r");
  if (!command)
    return;
  char buffer[4096]{};
  const bool readPath = std::fgets(buffer, sizeof(buffer), command) != nullptr;
  const int status = ::pclose(command);
  if (!readPath || status != 0)
    return;

  std::string path(buffer);
  while (!path.empty() && (path.back() == '\n' || path.back() == '\r'))
    path.pop_back();
  if (!path.empty() && fs::is_directory(path))
    ::setenv("SDKROOT", path.c_str(), 1);
}

} // namespace

int main(int argc, char **argv) {
  const fs::path root = materializeToolchain();
  if (root.empty())
    return 1;

  const std::string invokedAs = baseName(argc > 0 ? argv[0] : nullptr);
  const bool explicitDispatcher = invokedAs == "LLVM" || invokedAs == "llvm";

  if (explicitDispatcher &&
      (argc < 2 || std::string_view(argv[1]) == "--help" ||
       std::string_view(argv[1]) == "-h" ||
       std::string_view(argv[1]) == "help")) {
    std::cout << "Usage: LLVM <tool> [arguments]\n\n"
                 "Every embedded executable is available as a subcommand.\n"
                 "Use `LLVM --list` to print the complete list.\n"
                 "Extracted toolchain root: "
              << root << '\n';
    return argc < 2 ? 1 : 0;
  }

  if (explicitDispatcher && std::string_view(argv[1]) == "--list") {
    listTools(root);
    return 0;
  }
  if (explicitDispatcher && std::string_view(argv[1]) == "--root") {
    std::cout << root << '\n';
    return 0;
  }

  const std::string requested = explicitDispatcher ? argv[1] : invokedAs;
  if (requested.empty() || requested.find('/') != std::string::npos) {
    std::cerr << "LLVM: invalid tool name\n";
    return 1;
  }

  fs::path tool = root / "bin" / requested;
  if (!executableFile(tool) && !requested.starts_with("llvm-")) {
    const fs::path prefixed = root / "bin" / ("llvm-" + requested);
    if (executableFile(prefixed))
      tool = prefixed;
  }
  if (!executableFile(tool)) {
    std::cerr << "LLVM: embedded tool '" << requested << "' was not found\n";
    return 1;
  }

  prependEnvironmentPath("PATH", root / "bin");
  prependEnvironmentPath("DYLD_FALLBACK_LIBRARY_PATH", root / "deps/lib/dylibs");
  prependEnvironmentPath("DYLD_FALLBACK_LIBRARY_PATH", root / "deps/lib");
  prependEnvironmentPath("DYLD_FALLBACK_LIBRARY_PATH", root / "lib");
  ::setenv("PYTHONHOME", (root / "deps").c_str(), 1);
  ::setenv("LLVM_EMBEDDED_ROOT", root.c_str(), 1);
  configureAppleSdk();

  std::vector<char *> toolArguments;
  toolArguments.reserve(static_cast<std::size_t>(argc) + 1);
  std::string toolArgumentZero = tool.string();
  toolArguments.push_back(toolArgumentZero.data());
  const int firstArgument = explicitDispatcher ? 2 : 1;
  for (int index = firstArgument; index < argc; ++index)
    toolArguments.push_back(argv[index]);
  toolArguments.push_back(nullptr);

  ::execv(tool.c_str(), toolArguments.data());
  std::cerr << "LLVM: cannot execute " << requested << ": "
            << std::strerror(errno) << '\n';
  return 1;
}
