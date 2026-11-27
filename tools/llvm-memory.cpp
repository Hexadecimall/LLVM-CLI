// One-file LLVM multicall container. Tool modules and resources are read from
// the host image, decompressed into memory, and never materialized on disk.

#include "pack-format.h"
#include "native-wrappers.h"
#include "incremental-build.h"

#include <zstd.h>

#ifndef LLVM_CLI_LLVM_VERSION
#define LLVM_CLI_LLVM_VERSION "23.1.6"
#endif
#define LLVM_CLI_VERSION "1.8.0"

#include <algorithm>
#include <array>
#include <cstdarg>
#include <cerrno>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#if defined(__APPLE__)
#include <mach-o/dyld.h>
#include <mach-o/getsect.h>
#include <mach-o/loader.h>
#elif defined(__linux__)
#include <linux/memfd.h>
#include <sys/syscall.h>
#elif defined(_WIN32)
#define NOMINMAX
#include <windows.h>
#include <shellapi.h>
#endif
#include <ranges>
#ifndef _WIN32
#include <dlfcn.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#endif
#include <string>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>

#if defined(__APPLE__)
extern const mach_header_64 _mh_execute_header;
#elif defined(__linux__)
extern "C" const unsigned char _binary_LLVM_pack_start[];
extern "C" const unsigned char _binary_LLVM_pack_end[];
#endif
extern "C" int LLVM_llvm_main(int argc, char **argv);

namespace {

#ifdef _WIN32
#define LLVM_CLI_EXPORT __declspec(dllexport)
#else
#define LLVM_CLI_EXPORT __attribute__((visibility("default")))
#endif

struct Entry {
  llvm_pack::EntryHeader Header{};
  std::string Path;
  const std::uint8_t *Stored{};
};

std::vector<Entry> Entries;
std::unordered_map<std::string, std::size_t> ByPath;
int (*CurrentEntrypoint)(int, char **) = nullptr;
std::string CurrentBundleName;
std::string MainExecutablePath;
#ifdef _WIN32
std::wstring MainExecutableWidePath;

std::string utf8(const wchar_t *value) {
  const int length = ::WideCharToMultiByte(CP_UTF8, 0, value, -1, nullptr, 0, nullptr, nullptr);
  if (!length)
    return {};
  std::string result(length, '\0');
  ::WideCharToMultiByte(CP_UTF8, 0, value, -1, result.data(), length, nullptr, nullptr);
  result.pop_back();
  return result;
}

bool appendWindowsArgument(std::wstring &command, const std::string &argument) {
  const int length = ::MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, argument.c_str(), -1, nullptr, 0);
  if (!length)
    return false;
  std::wstring wide(length, L'\0');
  ::MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, argument.c_str(), -1, wide.data(), length);
  wide.pop_back();
  command += L" \"";
  std::size_t escapes = 0;
  for (wchar_t ch : wide) {
    if (ch == L'\\') {
      ++escapes;
      continue;
    }
    command.append(ch == L'"' ? 2 * escapes + 1 : escapes, L'\\');
    escapes = 0;
    command += ch;
  }
  command.append(2 * escapes, L'\\');
  command += L'"';
  return true;
}

int runWindowsWorker(const std::vector<std::string> &arguments) {
  std::wstring command = L"\"" + MainExecutableWidePath + L"\"";
  for (const auto &argument : arguments)
    if (!appendWindowsArgument(command, argument))
      return 127;
  STARTUPINFOW startup{};
  startup.cb = sizeof(startup);
  PROCESS_INFORMATION process{};
  if (!::CreateProcessW(MainExecutableWidePath.c_str(), command.data(), nullptr,
                        nullptr, TRUE, 0, nullptr, nullptr, &startup, &process))
    return 127;
  ::CloseHandle(process.hThread);
  DWORD code = 127;
  if (::WaitForSingleObject(process.hProcess, INFINITE) == WAIT_OBJECT_0)
    ::GetExitCodeProcess(process.hProcess, &code);
  ::CloseHandle(process.hProcess);
  return static_cast<int>(code);
}
#endif
std::string ActiveCompileTarget;
bool ActiveNativeCxxHeaders = false;
bool ActiveSdkHeaders = false;

std::string baseName(const char *path);

#ifndef _WIN32
struct VirtualFile {
  std::vector<char> Bytes;
  off_t Offset = 0;
  ino_t Inode = 0;
};

std::mutex VirtualFilesMutex;
std::unordered_map<int, VirtualFile> VirtualFiles;
int NextVirtualFd = 100000;
#endif

bool initializeIndex() {
#if defined(__APPLE__)
  unsigned long sectionSize = 0;
  const auto *bytes = reinterpret_cast<const std::uint8_t *>(getsectiondata(
      &_mh_execute_header, "__LLVM", "__pack", &sectionSize));
#elif defined(__linux__)
  const auto *bytes = _binary_LLVM_pack_start;
  const std::size_t sectionSize = static_cast<std::size_t>(
      _binary_LLVM_pack_end - _binary_LLVM_pack_start);
#elif defined(_WIN32)
  const HRSRC resource = ::FindResourceW(nullptr, MAKEINTRESOURCEW(1), MAKEINTRESOURCEW(10));
  if (!resource)
    return false;
  const HGLOBAL loaded = ::LoadResource(nullptr, resource);
  const auto *bytes = static_cast<const std::uint8_t *>(::LockResource(loaded));
  const std::size_t sectionSize = ::SizeofResource(nullptr, resource);
#else
#error Unsupported host image format
#endif
  if (!bytes || sectionSize < sizeof(llvm_pack::Header))
    return false;

  const auto *cursor = bytes;
  const auto *end = bytes + sectionSize;
  llvm_pack::Header header{};
  std::memcpy(&header, cursor, sizeof(header));
  cursor += sizeof(header);
  if (std::memcmp(header.MagicBytes, llvm_pack::Magic,
                  sizeof(header.MagicBytes)) != 0 ||
      header.VersionNumber != llvm_pack::Version)
    return false;

  Entries.reserve(header.EntryCount);
  for (std::uint32_t index = 0; index < header.EntryCount; ++index) {
    if (static_cast<std::size_t>(end - cursor) <
        sizeof(llvm_pack::EntryHeader))
      return false;
    Entry entry;
    std::memcpy(&entry.Header, cursor, sizeof(entry.Header));
    cursor += sizeof(entry.Header);
    if (static_cast<std::size_t>(end - cursor) < entry.Header.PathSize ||
        entry.Header.DataOffset > sectionSize ||
        entry.Header.StoredSize > sectionSize - entry.Header.DataOffset)
      return false;
    entry.Path.assign(reinterpret_cast<const char *>(cursor),
                      entry.Header.PathSize);
    cursor += entry.Header.PathSize;
    entry.Stored = bytes + entry.Header.DataOffset;
    ByPath.emplace(entry.Path, Entries.size());
    Entries.push_back(std::move(entry));
  }
  return true;
}

std::vector<char> decompress(const Entry &entry) {
  std::vector<char> output(static_cast<std::size_t>(entry.Header.OriginalSize));
  if (output.empty())
    return output;
  const std::size_t result = ZSTD_decompress(
      output.data(), output.size(), entry.Stored,
      static_cast<std::size_t>(entry.Header.StoredSize));
  if (ZSTD_isError(result) || result != output.size())
    return {};
  return output;
}

const Entry *findEntry(std::string_view path) {
  const auto found = ByPath.find(std::string(path));
  return found == ByPath.end() ? nullptr : &Entries[found->second];
}

const Entry *findResolvedEntry(std::string path) {
  for (unsigned depth = 0; depth != 16; ++depth) {
    const Entry *entry = findEntry(path);
    if (!entry || entry->Header.Type != llvm_pack::EntryType::Symlink)
      return entry;
    const std::vector<char> targetBytes = decompress(*entry);
    if (targetBytes.empty())
      return nullptr;
    const std::string target(targetBytes.begin(), targetBytes.end());
    if (target.starts_with("/__llvm/"))
      path = "root/" + target.substr(8);
    else
      path = (std::filesystem::path(path).parent_path() / target)
                 .lexically_normal().generic_string();
    if (!path.starts_with("root/"))
      return nullptr;
  }
  return nullptr;
}

std::string embeddedText(std::string_view path) {
  const Entry *entry = findEntry(path);
  if (!entry)
    return {};
  std::vector<char> bytes = decompress(*entry);
  return {bytes.begin(), bytes.end()};
}

std::string packedPathForVirtual(std::string_view path) {
  constexpr std::string_view rootPrefix = "/__llvm/";
  constexpr std::string_view scriptPrefix = "/__llvm/scripts/";
  if (path.starts_with(scriptPrefix))
    return "scripts/" + std::string(path.substr(scriptPrefix.size()));
  if (path.starts_with(rootPrefix))
    return "root/" + std::string(path.substr(rootPrefix.size()));
  return {};
}

#ifndef _WIN32
ino_t virtualInode(std::string_view path) {
  std::uint64_t hash = 14695981039346656037ULL;
  for (unsigned char byte : path) {
    hash ^= byte;
    hash *= 1099511628211ULL;
  }
  return static_cast<ino_t>(hash == 0 ? 1 : hash);
}
#endif

void setEnvironment(const char *name, const char *value) {
#ifdef _WIN32
  ::_putenv_s(name, value);
#else
  ::setenv(name, value, 1);
#endif
}

std::string resolveTool(std::string requested) {
  if (requested == "llvm" || findEntry("bundles/" + requested + ".bundle"))
    return requested;
  if (const Entry *alias = findEntry("aliases/" + requested)) {
    std::string target = embeddedText(alias->Path);
    if (target.ends_with(".exe"))
      target.resize(target.size() - 4);
    if (target == "llvm" || findEntry("bundles/" + target + ".bundle"))
      return target;
  }
  if (!requested.starts_with("llvm-")) {
    const std::string prefixed = "llvm-" + requested;
    if (findEntry("bundles/" + prefixed + ".bundle"))
      return prefixed;
  }
  return {};
}

bool hasScript(std::string_view requested) {
  return findEntry("scripts/" + std::string(requested)) != nullptr;
}

std::string canonicalTool(std::string requested) {
  if (!resolveTool(requested).empty() || hasScript(requested) ||
      requested.starts_with("llvm-"))
    return requested;
  const std::string prefixed = "llvm-" + requested;
  if (!resolveTool(prefixed).empty() || hasScript(prefixed))
    return prefixed;
  return requested;
}

std::string entrySymbol(std::string tool) {
  for (char &character : tool)
    if (!std::isalnum(static_cast<unsigned char>(character)))
      character = '_';
#if defined(__APPLE__)
  return "_LLVM_" + tool + "_main";
#else
  return "LLVM_" + tool + "_main";
#endif
}

void configureAppleSdk() {
  if (const char *existing = std::getenv("SDKROOT"); existing && *existing)
    return;
  if (findEntry("root/targets/macos/SDKSettings.json") &&
      findEntry("root/targets/macos/usr/lib/libSystem.tbd")) {
    setEnvironment("SDKROOT", "/__llvm/targets/macos");
    return;
  }
#if defined(__APPLE__)
  std::vector<std::string> candidates;
  if (const char *developer = std::getenv("DEVELOPER_DIR");
      developer && *developer) {
    candidates.emplace_back(std::string(developer) +
        "/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk");
  }
  candidates.emplace_back(
      "/Applications/Xcode-beta.app/Contents/Developer/Platforms/"
      "MacOSX.platform/Developer/SDKs/MacOSX.sdk");
  candidates.emplace_back(
      "/Applications/Xcode.app/Contents/Developer/Platforms/"
      "MacOSX.platform/Developer/SDKs/MacOSX.sdk");
  candidates.emplace_back(
      "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk");
  struct stat status {};
  for (const std::string &candidate : candidates) {
    if (::stat(candidate.c_str(), &status) == 0 && S_ISDIR(status.st_mode)) {
      setEnvironment("SDKROOT", candidate.c_str());
      return;
    }
  }
#endif
}

bool configureEmbeddedPython() {
  constexpr const char *path =
      "/__llvm/deps/lib/python314.zip:"
      "/__llvm/lib/python3.14/site-packages:"
      "/__llvm/lib";
  if (!findEntry("root/deps/lib/python314.zip"))
    return false;
  setEnvironment("PYTHONHOME", "/__llvm/deps");
  setEnvironment("PYTHONPATH", path);
  return true;
}

bool isClangDriver(std::string_view name) {
  return name == "clang" || name == "clang++" || name == "clang-cpp" ||
         name == "clang-cl";
}

bool isFlangDriver(std::string_view name) {
  return name == "flang" || name == "flang-new";
}

bool isCompilerCommand(std::string_view name) {
  return isClangDriver(name) || isFlangDriver(name) || name == "llc";
}

struct CompileTarget {
  std::string Requested;
  std::string Triple;
  std::string Os;
  std::string Arch;
  std::vector<std::string> DriverOptions;
  std::string Error;
  bool Ghidra = false;
};

bool isCataloguedGhidraTarget(std::string_view value) {
  const std::string catalog = embeddedText("root/share/ghidra-targets.txt");
  std::size_t start = 0;
  while (start < catalog.size()) {
    const std::size_t end = catalog.find('\n', start);
    const std::string_view line(catalog.data() + start,
        (end == std::string::npos ? catalog.size() : end) - start);
    const std::size_t separator = line.find("  ");
    if (line.substr(0, separator) == value)
      return true;
    if (end == std::string::npos)
      break;
    start = end + 1;
  }
  return false;
}

bool parseGhidraCompileTarget(std::string_view value, CompileTarget &target) {
  target.Requested = value;
  target.Ghidra = true;
  if (!isCataloguedGhidraTarget(value)) {
    target.Error = "Ghidra processor ID is not in the embedded catalog: " +
                   std::string(value);
    return false;
  }
  const std::size_t first = value.find(':');
  const std::size_t second = value.find(':', first + 1);
  const std::size_t third = value.find(':', second + 1);
  const std::string_view family = value.substr(0, first);
  const std::string_view endian = value.substr(first + 1, second - first - 1);
  const std::string_view bits = value.substr(second + 1, third - second - 1);
  const std::string_view variant = value.substr(third + 1);
  target.Os = "freestanding";
  auto setArch = [&](std::string arch, std::string triple) {
    target.Arch = std::move(arch);
    target.Triple = std::move(triple);
  };
  auto option = [&](std::string value) {
    target.DriverOptions.push_back(std::move(value));
  };

  if (family == "Xtensa" && bits == "32" && endian == "LE" &&
      variant == "default")
    setArch("xtensa", "xtensa-unknown-none");
  else if (family == "PowerPC" && (bits == "32" || bits == "64") &&
           (endian == "BE" || endian == "LE")) {
    const bool wide = bits == "64";
    const bool little = endian == "LE";
    setArch(wide ? (little ? "powerpc64le" : "powerpc64")
                 : (little ? "powerpcle" : "powerpc"),
            wide ? (little ? "powerpc64le-unknown-none" :
                             "powerpc64-unknown-none")
                 : (little ? "powerpcle-unknown-none" :
                             "powerpc-unknown-none"));
    if (variant == "4xx" && !wide)
      option("-mcpu=440");
    else if (variant == "e500" && !wide)
      option("-mcpu=e500");
    else if (variant == "e500mc" && !wide)
      option("-mcpu=e500mc");
    else if (variant != "default")
      target.Triple.clear();
  } else if (family == "MIPS" && (bits == "32" || bits == "64") &&
             (endian == "BE" || endian == "LE")) {
    const bool wide = bits == "64";
    const bool little = endian == "LE";
    setArch(wide ? (little ? "mips64el" : "mips64")
                 : (little ? "mipsel" : "mips"),
            wide ? (little ? "mips64el-unknown-none" :
                             "mips64-unknown-none")
                 : (little ? "mipsel-unknown-none" :
                             "mips-unknown-none"));
    if (variant == "16e")
      option("-mips16");
    else if (variant == "micro" && !wide)
      option("-mmicromips");
    else if (variant == "R6" && !wide) {
      option("-march=mips32r6");
      option("-mmicromips");
    }
    else if (variant == "64-32addr" && wide)
      option("-mabi=n32");
    else if (variant != "default")
      target.Triple.clear();
  } else if (family == "x86" && endian == "LE" && variant == "default") {
    if (bits == "32")
      setArch("x86", "i386-unknown-none");
    else if (bits == "64")
      setArch("x86_64", "x86_64-unknown-none");
  } else if (family == "ARM" && bits == "32" &&
             (endian == "LE" || endian == "BE")) {
    setArch("arm", endian == "LE" ? "arm-unknown-none-eabi" :
                                      "armeb-unknown-none-eabi");
    if (variant == "v4" || variant == "v4t" || variant == "v5" ||
        variant == "v5t" || variant == "v6" || variant == "v7" ||
        variant == "v8" || variant == "v8T") {
      const std::string march = variant == "v7" ? "armv7-a" :
          (variant == "v8" || variant == "v8T") ? "armv8-a" :
          "arm" + std::string(variant);
      option("-march=" + march);
      if (variant == "v8T")
        option("-mthumb");
    } else if (variant == "v8-m") {
      option("-march=armv8-m.main");
      option("-mthumb");
    } else {
      target.Triple.clear();
    }
  } else if (family == "RISCV" && endian == "LE" &&
             (bits == "32" || bits == "64")) {
    const std::string width(bits);
    setArch("riscv" + width, "riscv" + width + "-unknown-none-elf");
    if (variant == "default")
      option("-march=rv" + width + "i");
    else if (variant == "RV" + width + "I" ||
             variant == "RV" + width + "IC" ||
             variant == "RV" + width + "G" ||
             variant == "RV" + width + "GC" ||
             (width == "32" && variant == "RV32IMC")) {
      std::string isa(variant);
      std::ranges::transform(isa, isa.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
      });
      option("-march=" + isa);
    } else {
      target.Triple.clear();
    }
  } else if (family == "AARCH64" && bits == "64" && variant == "v8A" &&
             (endian == "LE" || endian == "BE")) {
    setArch("aarch64", endian == "LE" ? "aarch64-unknown-none" :
                                          "aarch64_be-unknown-none");
    option("-march=armv8.5-a");
  } else if (family == "sparc" && endian == "BE" && variant == "default") {
    if (bits == "32")
      setArch("sparc", "sparc-unknown-none");
    else if (bits == "64")
      setArch("sparc64", "sparcv9-unknown-none");
  } else if (family == "68000" && endian == "BE" && bits == "32") {
    setArch("m68k", "m68k-unknown-none");
    if (variant == "default")
      option("-mcpu=m68040");
    else if (variant == "MC68030" || variant == "MC68020")
      option("-mcpu=m680" + std::string(variant.substr(5)));
    else
      target.Triple.clear();
  }
  if (target.Triple.empty()) {
    target.Error = "Ghidra processor '" + std::string(value) +
        "' has no verified LLVM-CLI code-generation mapping";
    return false;
  }
  return true;
}

bool parseCompileTarget(std::string_view value, CompileTarget &target) {
  if (value == "native") {
#if defined(__APPLE__)
    target = {"native", "", "macos", "aarch64"};
#elif defined(__linux__)
    target = {"native", "", "linux",
#if defined(__aarch64__)
              "aarch64"};
#else
              "x86_64"};
#endif
#elif defined(_WIN32)
    target = {"native", "", "windows", "x86_64"};
#endif
    return true;
  }
  if (value.find(':') != std::string_view::npos)
    return parseGhidraCompileTarget(value, target);
  if (value.empty() || value.find_first_not_of(
          "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.+-") !=
                           std::string_view::npos)
    return false;
  std::string normalized(value);
  const std::size_t first = normalized.find('-');
  if (first == std::string_view::npos || first == 0)
    return false;
  std::size_t second = normalized.find('-', first + 1);
  std::string arch = normalized.substr(0, first);
  std::string os = normalized.substr(first + 1,
      second == std::string::npos ? second : second - first - 1);
  std::string abi = second == std::string::npos
      ? "" : normalized.substr(second + 1);
  bool hadVendor = false;
  if ((os == "unknown" || os == "pc" || os == "apple" || os == "w64") &&
      !abi.empty()) {
    hadVendor = true;
    const std::size_t vendorEnd = abi.find('-');
    os = abi.substr(0, vendorEnd);
    abi = vendorEnd == std::string::npos ? "" : abi.substr(vendorEnd + 1);
  }
  if (arch == "linux" &&
      (os == "arm64" || os == "aarch64" || os == "amd64" ||
       os == "x86_64")) {
    arch = os;
    os = "linux";
    if (abi.empty())
      abi = "musl";
  }
  if (arch == "arm64")
    arch = "aarch64";
  else if (arch == "amd64")
    arch = "x86_64";
  if (os == "darwin" || os == "macosx")
    os = "macos";
  else if (os == "wasip1")
    os = "wasi";
  if (os.empty() || (second != std::string::npos && abi.empty() &&
                     !hadVendor))
    return false;
  normalized = arch + "-" + os;
  if (!abi.empty() && abi != "none")
    normalized += "-" + abi;
  target.Arch = arch;
  target.Requested = normalized;
  target.Os = os;
  const std::string clangArch = arch == "x86" ? "i386" :
                                arch == "aarch64" ? "aarch64" : arch;
  if (target.Os == "macos" || target.Os == "ios" || target.Os == "tvos" ||
      target.Os == "watchos" || target.Os == "visionos") {
    target.Triple = clangArch + "-apple-" +
        (target.Os == "macos" ? "macosx" : target.Os);
    if (!abi.empty() && abi != "none")
      target.Triple += "-" + abi;
  } else if (target.Os == "windows") {
    target.Triple = clangArch + "-w64-windows-" +
                    (abi.empty() ? "gnu" : abi);
  } else if (target.Os == "wasi") {
    target.Triple = clangArch + "-unknown-wasip1";
  } else if (target.Os == "freestanding") {
    target.Triple = clangArch + "-unknown-none";
  } else {
    target.Triple = clangArch + "-unknown-" + target.Os;
    if (!abi.empty() && abi != "none")
      target.Triple += "-" + abi;
  }
  return true;
}

bool flangSupportsTarget(const CompileTarget &target) {
  constexpr std::array<std::string_view, 12> architectures{
      "x86", "x86_64", "aarch64", "powerpc", "powerpc64",
      "powerpc64le", "sparc", "sparc64", "riscv64", "amdgcn",
      "nvptx64", "loongarch64"};
  return std::ranges::find(architectures, target.Arch) != architectures.end();
}

bool hasArgument(int argc, char **argv, int firstArgument,
                 std::string_view exact) {
  for (int index = firstArgument; index < argc; ++index) {
    if (std::string_view(argv[index]) == "--")
      break;
    if (std::string_view(argv[index]) == exact)
      return true;
  }
  return false;
}

bool hasArgumentPrefix(int argc, char **argv, int firstArgument,
                       std::string_view prefix) {
  for (int index = firstArgument; index < argc; ++index) {
    if (std::string_view(argv[index]) == "--")
      break;
    if (std::string_view(argv[index]).starts_with(prefix))
      return true;
  }
  return false;
}

enum class LinkFlavor { Unknown, Elf, MachO, Coff, Wasm, Conflict };

LinkFlavor linkFlavorFromFile(std::string_view path) {
#ifdef _WIN32
  std::array<unsigned char, 8> bytes{};
  std::ifstream input(std::filesystem::path(std::u8string(path.begin(), path.end())),
                      std::ios::binary);
  input.read(reinterpret_cast<char *>(bytes.data()), bytes.size());
  const auto count = input.gcount();
#else
  const int fd = ::open(std::string(path).c_str(), O_RDONLY);
  if (fd < 0)
    return LinkFlavor::Unknown;
  std::array<unsigned char, 8> bytes{};
  const ssize_t count = ::read(fd, bytes.data(), bytes.size());
  ::close(fd);
#endif
  if (count < 4)
    return LinkFlavor::Unknown;
  if (bytes[0] == 0x7f && bytes[1] == 'E' && bytes[2] == 'L' &&
      bytes[3] == 'F')
    return LinkFlavor::Elf;
  if (bytes[0] == 0 && bytes[1] == 'a' && bytes[2] == 's' &&
      bytes[3] == 'm')
    return LinkFlavor::Wasm;
  if ((bytes[0] == 0xcf && bytes[1] == 0xfa && bytes[2] == 0xed &&
       bytes[3] == 0xfe) ||
      (bytes[0] == 0xfe && bytes[1] == 0xed && bytes[2] == 0xfa &&
       bytes[3] == 0xcf) ||
      (bytes[0] == 0xce && bytes[1] == 0xfa && bytes[2] == 0xed &&
       bytes[3] == 0xfe) ||
      (bytes[0] == 0xfe && bytes[1] == 0xed && bytes[2] == 0xfa &&
       bytes[3] == 0xce) ||
      (bytes[0] == 0xca && bytes[1] == 0xfe && bytes[2] == 0xba &&
       bytes[3] == 0xbe) ||
      (bytes[0] == 0xbe && bytes[1] == 0xba && bytes[2] == 0xfe &&
       bytes[3] == 0xca))
    return LinkFlavor::MachO;
  if (bytes[0] == 'M' && bytes[1] == 'Z')
    return LinkFlavor::Coff;
  if ((bytes[0] == 0x64 && bytes[1] == 0x86) ||
      (bytes[0] == 0x64 && bytes[1] == 0xaa) ||
      (bytes[0] == 0x4c && bytes[1] == 0x01) ||
      (bytes[0] == 0x00 && bytes[1] == 0x00 &&
       bytes[2] == 0xff && bytes[3] == 0xff))
    return LinkFlavor::Coff;
  return LinkFlavor::Unknown;
}

LinkFlavor mergeLinkFlavor(LinkFlavor current, LinkFlavor candidate) {
  if (candidate == LinkFlavor::Unknown)
    return current;
  if (current == LinkFlavor::Unknown || current == candidate)
    return candidate;
  return LinkFlavor::Conflict;
}

LinkFlavor inferLinkFlavor(int argc, char **argv, int firstArgument) {
  LinkFlavor format = LinkFlavor::Unknown;
  std::string_view output;
  for (int index = firstArgument; index < argc; ++index) {
    const std::string_view arg(argv[index]);
    if ((arg == "-o" || arg == "--output") && index + 1 < argc) {
      output = argv[++index];
      continue;
    }
    if (arg.starts_with("/out:") || arg.starts_with("/OUT:")) {
      output = arg.substr(5);
      format = mergeLinkFlavor(format, LinkFlavor::Coff);
      continue;
    }
    if (arg == "-m" && index + 1 < argc) {
      const std::string_view mode(argv[++index]);
      if (mode.starts_with("elf"))
        format = mergeLinkFlavor(format, LinkFlavor::Elf);
      else if (mode.starts_with("wasm"))
        format = mergeLinkFlavor(format, LinkFlavor::Wasm);
      continue;
    }
    if (arg == "-arch") {
      format = mergeLinkFlavor(format, LinkFlavor::MachO);
      if (index + 1 < argc)
        ++index;
      continue;
    }
    if (arg.starts_with("/machine:") || arg.starts_with("/MACHINE:") ||
        arg.starts_with("/dll") || arg.starts_with("/DLL")) {
      format = mergeLinkFlavor(format, LinkFlavor::Coff);
      continue;
    }
    if (arg == "--import-memory" || arg == "--export-table" ||
        arg == "--shared-memory") {
      format = mergeLinkFlavor(format, LinkFlavor::Wasm);
      continue;
    }
    if (arg.starts_with('-'))
      continue;
    format = mergeLinkFlavor(format, linkFlavorFromFile(arg));
  }
  if (format != LinkFlavor::Unknown)
    return format;
  if (output.ends_with(".wasm"))
    return LinkFlavor::Wasm;
  if (output.ends_with(".dylib") || output.ends_with(".tbd"))
    return LinkFlavor::MachO;
  if (output.ends_with(".dll") || output.ends_with(".exe"))
    return LinkFlavor::Coff;
  if (output.ends_with(".so"))
    return LinkFlavor::Elf;
  return LinkFlavor::Unknown;
}

bool hasUserSysroot(int argc, char **argv, int firstArgument) {
  return hasArgument(argc, argv, firstArgument, "--sysroot") ||
         hasArgumentPrefix(argc, argv, firstArgument, "--sysroot=") ||
         hasArgument(argc, argv, firstArgument, "-isysroot");
}

bool isDriverLink(int argc, char **argv, int firstArgument) {
  if (firstArgument >= argc)
    return false;
  if (std::string_view(argv[firstArgument]).starts_with("-cc1") ||
      std::string_view(argv[firstArgument]) == "-fc1")
    return false;
  constexpr std::array<std::string_view, 8> nonLinking{
      "-E", "-S", "-c", "-fsyntax-only", "-emit-ast", "-M", "-MM",
      "-cc1"};
  for (std::string_view option : nonLinking)
    if (hasArgument(argc, argv, firstArgument, option))
      return false;
  for (int index = firstArgument; index < argc; ++index) {
    const std::string_view argument(argv[index]);
    if (argument == "--help" || argument == "-help" ||
        argument == "--version" || argument == "-dumpversion" ||
        argument == "-dumpmachine" || argument.starts_with("-print-") ||
        argument.starts_with("--print-"))
      return false;
  }
  return true;
}

int runBundle(const std::string &bundleName, const std::string &invokedName,
              int argc, char **argv, int firstArgument,
              std::string_view symbolOverride = {},
              const CompileTarget *compileTarget = nullptr) {
  if (bundleName == "lldb" || bundleName == "lldb-dap" ||
      bundleName == "lldb-mcp" || bundleName == "python3.14") {
    if (!configureEmbeddedPython()) {
      std::cerr << "LLVM: cannot prepare embedded Python standard library\n";
      return 1;
    }
  }
  int (*entrypoint)(int, char **) = nullptr;
  std::vector<char> imageBytes;
  if (bundleName == "llvm") {
    entrypoint = LLVM_llvm_main;
  } else {
    const Entry *entry = findEntry("bundles/" + bundleName + ".bundle");
    if (!entry)
      return 1;
    imageBytes = decompress(*entry);
    if (imageBytes.empty()) {
      std::cerr << "LLVM: cannot decompress " << bundleName << '\n';
      return 1;
    }

    std::string symbolName = symbolOverride.empty()
        ? entrySymbol(bundleName) : std::string(symbolOverride);
#if defined(__APPLE__)
    NSObjectFileImage image{};
    const NSObjectFileImageReturnCode imageResult =
        NSCreateObjectFileImageFromMemory(imageBytes.data(), imageBytes.size(),
                                          &image);
    if (imageResult != NSObjectFileImageSuccess) {
      std::cerr << "LLVM: dyld rejected embedded " << bundleName << " module\n";
      return 1;
    }
    NSModule module = NSLinkModule(
        image, bundleName.c_str(), NSLINKMODULE_OPTION_RETURN_ON_ERROR |
                                      NSLINKMODULE_OPTION_PRIVATE);
    if (!module) {
      NSLinkEditErrors errorClass{};
      int errorNumber = 0;
      const char *file = nullptr;
      const char *message = nullptr;
      NSLinkEditError(&errorClass, &errorNumber, &file, &message);
      std::cerr << "LLVM: cannot load " << bundleName << ": "
                << (message ? message : "unknown dyld error") << '\n';
      return 1;
    }

    NSSymbol symbol = NSLookupSymbolInModule(module, symbolName.c_str());
    if (!symbol) {
      std::cerr << "LLVM: embedded entrypoint missing for " << bundleName << '\n';
      return 1;
    }
    entrypoint = reinterpret_cast<int (*)(int, char **)>(
        NSAddressOfSymbol(symbol));
#elif defined(__linux__)
    const int fd = static_cast<int>(::syscall(
        SYS_memfd_create, bundleName.c_str(), MFD_CLOEXEC));
    if (fd < 0) {
      std::cerr << "LLVM: cannot allocate in-memory " << bundleName
                << " module: " << std::strerror(errno) << '\n';
      return 1;
    }
    for (std::size_t offset = 0; offset < imageBytes.size();) {
      const ssize_t count = ::write(fd, imageBytes.data() + offset,
                                    imageBytes.size() - offset);
      if (count < 0 && errno == EINTR)
        continue;
      if (count <= 0) {
        std::cerr << "LLVM: cannot load in-memory " << bundleName
                  << " module: " << std::strerror(errno) << '\n';
        ::close(fd);
        return 1;
      }
      offset += static_cast<std::size_t>(count);
    }
    const std::string memoryPath = "/proc/self/fd/" + std::to_string(fd);
    void *module = ::dlopen(memoryPath.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!module) {
      ::close(fd);
      std::cerr << "LLVM: cannot load " << bundleName << ": "
                << ::dlerror() << '\n';
      return 1;
    }
    if (!symbolName.empty() && symbolName.front() == '_')
      symbolName.erase(0, 1);
    entrypoint = reinterpret_cast<int (*)(int, char **)>(
        ::dlsym(module, symbolName.c_str()));
    if (!entrypoint) {
      std::cerr << "LLVM: embedded entrypoint missing for " << bundleName
                << ": " << ::dlerror() << '\n';
      return 1;
    }
#elif defined(_WIN32)
    std::cerr << "LLVM: embedded COFF module loading is unavailable for " << bundleName << '\n';
    return 1;
#endif
  }
  CurrentEntrypoint = entrypoint;
  CurrentBundleName = bundleName;

  std::vector<char *> arguments;
  std::string argumentZero = "/__llvm/bin/" + invokedName;
  arguments.push_back(argumentZero.data());
  std::string resourceOption;
  std::string resourceDirectory;
  std::string linkerOption;
  std::string linkerPathOption;
  std::string runtimeLibraryOption;
  std::string llvmLibraryOption;
  std::string targetOption;
  std::string embeddedBinOption;
  std::string targetRoot;
  std::array<std::string, 4> targetIncludes;
  std::deque<std::string> targetRuntimeFiles;
  std::deque<std::string> targetCppOptions;
  for (int index = firstArgument; index < argc; ++index)
    arguments.push_back(argv[index]);
  const std::size_t originalArgumentsEnd = arguments.size();
  const bool recursiveFrontend = firstArgument < argc &&
      (std::string_view(argv[firstArgument]).starts_with("-cc1") ||
       std::string_view(argv[firstArgument]) == "-fc1");
#ifdef _WIN32
  if (recursiveFrontend && ActiveCompileTarget.empty()) {
    // A frontend invocation receives the lowered triple instead of the
    // wrapper's -compile-target option.
    for (int index = firstArgument + 1; index + 1 < argc; ++index) {
      if (std::string_view(argv[index]) != "-triple")
        continue;
      CompileTarget frontendTarget;
      if (parseCompileTarget(argv[index + 1], frontendTarget))
        ActiveCompileTarget = frontendTarget.Requested;
      break;
    }
  }
#endif
  const bool crossTarget = compileTarget && !compileTarget->Triple.empty();
  std::string_view rawTarget;
  bool arm64Runtime = !crossTarget || compileTarget->Arch == "aarch64";
  for (int index = firstArgument; index < argc; ++index) {
    const std::string_view argument(argv[index]);
    if (argument == "--")
      break;
    if ((argument == "-target" || argument == "--target") && index + 1 < argc)
      rawTarget = argv[++index];
    else if (argument.starts_with("--target="))
      rawTarget = argument.substr(9);
    else if (argument.starts_with("-target="))
      rawTarget = argument.substr(8);
    else if (argument == "-arch" && index + 1 < argc) {
      const std::string_view arch(argv[++index]);
      arm64Runtime &= arch == "arm64" || arch == "aarch64";
    }
  }
  CompileTarget rawPlatform;
  const bool knownRawTarget = !rawTarget.empty() && parseCompileTarget(rawTarget, rawPlatform);
  const CompileTarget *platform = rawTarget.empty() ? compileTarget :
      knownRawTarget ? &rawPlatform : nullptr;
  const bool defaultPlatform = rawTarget.empty() && !crossTarget;
#ifdef __APPLE__
  const bool defaultApple = defaultPlatform;
#else
  const bool defaultApple = false;
#endif
  const bool applePlatform = defaultApple || (platform &&
      (platform->Os == "macos" || platform->Os == "ios" || platform->Os == "tvos" ||
       platform->Os == "watchos" || platform->Os == "visionos"));
  const bool nativeMacRuntime = defaultApple || (platform && platform->Os == "macos");
  const bool x86MacRuntime = platform && platform->Os == "macos" &&
      platform->Arch == "x86_64";
  const std::string nativeCxxRuntimeRoot = x86MacRuntime
      ? "/__llvm/targets/x86_64-macos" : "/__llvm";
  if (!rawTarget.empty())
    arm64Runtime &= knownRawTarget && rawPlatform.Arch == "aarch64";
  ActiveNativeCxxHeaders = (invokedName == "clang++" || invokedName == "clangd") && nativeMacRuntime &&
      !recursiveFrontend &&
      !hasArgument(argc, argv, firstArgument, "-nostdinc++") &&
      !hasArgument(argc, argv, firstArgument, "-stdlib=libstdc++") &&
      findEntry("root/include/c++/v1/__config_site");
  ActiveSdkHeaders = false;
  for (int index = firstArgument; index < argc; ++index) {
    const std::string_view argument(argv[index]);
    if ((argument.find("/__llvm/include") != std::string_view::npos ||
         argument.find("/__llvm/deps/include") != std::string_view::npos) &&
        argument.find("/__llvm/include/c++") == std::string_view::npos)
      ActiveSdkHeaders = true;
  }
  const bool bundledRuntime = !crossTarget || compileTarget->Os == "macos";
  const bool gnuLinux = crossTarget && compileTarget->Requested == "x86_64-linux-gnu";
  const bool embeddedLibc = crossTarget && isClangDriver(invokedName) &&
      (compileTarget->Requested == "x86_64-linux-musl" ||
       gnuLinux ||
       compileTarget->Requested == "aarch64-linux-musl" ||
       compileTarget->Requested == "wasm32-wasi" ||
       compileTarget->Requested == "x86_64-windows-gnu") &&
      findEntry("root/targets/" + compileTarget->Requested + "/lib/" +
                (compileTarget->Os == "windows" ? "zigc.lib" :
                 gnuLinux ? "libc.so.6" : "libc.a"));
  const bool useEmbeddedLibc = embeddedLibc &&
      invokedName != "clang-cpp" && invokedName != "clang-cl" &&
      !hasUserSysroot(argc, argv, firstArgument) &&
      !hasArgument(argc, argv, firstArgument, "-nostdlib");
  if (applePlatform &&
      (isClangDriver(invokedName) || isFlangDriver(invokedName) || invokedName == "ld64.lld") &&
      (hasArgument(argc, argv, firstArgument, "-static") ||
       hasArgument(argc, argv, firstArgument, "-static-pie")) &&
      isDriverLink(argc, argv, firstArgument)) {
    std::cerr << "LLVM: the embedded Mach-O linker does not implement fully static executables; "
                 "refusing to produce a dynamically linked executable\n";
    return 2;
  }
  if (crossTarget && compileTarget->Os == "windows" &&
      hasArgument(argc, argv, firstArgument, "-static") &&
      isDriverLink(argc, argv, firstArgument)) {
    std::cerr << "LLVM: -static cannot guarantee an import-free Windows "
                 "executable with the current embedded runtime; use "
                 "-nostdlib and an explicit entry point for a freestanding "
                 "PE, or omit -static for system DLL imports\n";
    return 2;
  }
  if (gnuLinux && useEmbeddedLibc && isDriverLink(argc, argv, firstArgument) &&
      (hasArgument(argc, argv, firstArgument, "-static") ||
       hasArgument(argc, argv, firstArgument, "-static-pie"))) {
    std::cerr << "LLVM: static GNU/Linux linking requires a GNU sysroot with static libc; "
                 "the embedded GNU runtime supports dynamic executables and shared libraries\n";
    return 2;
  }
  const bool sharedLink = hasArgument(argc, argv, firstArgument, "-shared") ||
      (hasArgument(argc, argv, firstArgument, "-dynamic") &&
       (!compileTarget || compileTarget->Os != "linux"));
  const bool dynamicLink =
      hasArgument(argc, argv, firstArgument, "-dynamic") ||
      hasArgument(argc, argv, firstArgument, "-Wl,-Bdynamic") ||
      hasArgument(argc, argv, firstArgument, "-pie");
  const bool dynamicExecutable = crossTarget &&
      compileTarget->Os == "linux" && (dynamicLink || gnuLinux) && !sharedLink &&
      !hasArgument(argc, argv, firstArgument, "-static") &&
      !hasArgument(argc, argv, firstArgument, "-static-pie");
  const bool embeddedCpp = embeddedLibc && invokedName == "clang++" &&
      findEntry("root/targets/" + compileTarget->Requested +
                "/lib/libc++.a") &&
      !hasUserSysroot(argc, argv, firstArgument);
  if (crossTarget && !recursiveFrontend) {
    if (useEmbeddedLibc && dynamicExecutable &&
        !findEntry("root/targets/" + compileTarget->Requested +
                   (gnuLinux ? "/lib/libc.so.6" : "/lib/libc.so"))) {
      std::cerr << "LLVM: dynamic Linux executables for '"
                << compileTarget->Requested
                << "' need --sysroot with a target dynamic libc and loader\n";
      return 2;
    }
    targetOption = bundleName == "llc"
        ? "-mtriple=" + compileTarget->Triple
        : "--target=" + compileTarget->Triple;
    arguments.push_back(targetOption.data());
    if (isClangDriver(invokedName))
      for (const std::string &option : compileTarget->DriverOptions)
        arguments.push_back(const_cast<char *>(option.c_str()));
    if (bundleName != "llc" && !bundledRuntime && !useEmbeddedLibc &&
        isDriverLink(argc, argv, firstArgument) &&
        !hasArgument(argc, argv, firstArgument, "-nostdlib") &&
        !hasUserSysroot(argc, argv, firstArgument) && !sharedLink) {
      if (compileTarget->Os == "linux" &&
          compileTarget->Requested.ends_with("-gnu"))
        std::cerr << "LLVM: '" << compileTarget->Requested
                  << "' needs a target glibc sysroot for linking; use "
                     "--sysroot=PATH, or choose "
                  << compileTarget->Arch << "-linux-musl for the bundled "
                     "runtime\n";
      else
        std::cerr << "LLVM: '" << compileTarget->Requested
                  << "' has no embedded target C runtime for linking; use "
                     "-c for object output, or supply -nostdlib and your "
                     "own entry/runtime\n";
      return 2;
    }
    if (isClangDriver(invokedName) && sharedLink && !bundledRuntime &&
        !useEmbeddedLibc && !hasUserSysroot(argc, argv, firstArgument) &&
        !hasArgument(argc, argv, firstArgument, "-nostdlib"))
      arguments.push_back(const_cast<char *>("-nostdlib"));
  }
  if (isClangDriver(invokedName) && !recursiveFrontend) {
    arguments.push_back(const_cast<char *>("-fintegrated-cc1"));
    const bool driverLink = isDriverLink(argc, argv, firstArgument);
#ifdef _WIN32
    // Darwin's SDKROOT fallback uses the host's definition of an absolute
    // path, which requires a drive letter on Windows.
    if (applePlatform && !hasUserSysroot(argc, argv, firstArgument))
      if (const char *sdk = std::getenv("SDKROOT"); sdk && *sdk) {
        arguments.push_back(const_cast<char *>("-isysroot"));
        arguments.push_back(const_cast<char *>(sdk));
      }
#endif
    if (applePlatform && driverLink &&
        (hasArgument(argc, argv, firstArgument, "-nodefaultlibs") ||
         hasArgument(argc, argv, firstArgument, "-nostdlib")) &&
        !hasArgument(argc, argv, firstArgument, "-lSystem"))
      arguments.push_back(const_cast<char *>("-lSystem"));
    if (ActiveNativeCxxHeaders) {
      arguments.push_back(const_cast<char *>("-nostdinc++"));
      arguments.push_back(const_cast<char *>("-isystem"));
      arguments.push_back(const_cast<char *>("/__llvm/include/c++/v1"));
    }
    if (nativeMacRuntime && invokedName == "clang++" && driverLink &&
        !hasArgument(argc, argv, firstArgument, "-stdlib=libstdc++") &&
        !hasArgument(argc, argv, firstArgument, "-nostdlib") &&
        !hasArgument(argc, argv, firstArgument, "-nodefaultlibs") &&
        !hasArgument(argc, argv, firstArgument, "-nostdlib++") &&
        !hasArgument(argc, argv, firstArgument, "-dynamic") &&
        (arm64Runtime || x86MacRuntime) &&
        findEntry(packedPathForVirtual(nativeCxxRuntimeRoot + "/lib/libc++.a")) &&
        findEntry(packedPathForVirtual(nativeCxxRuntimeRoot + "/lib/libc++abi.a")) &&
        findEntry(packedPathForVirtual(nativeCxxRuntimeRoot + "/lib/libunwind.a"))) {
      arguments.push_back(const_cast<char *>("-nostdlib++"));
      for (std::string_view library : {"libc++.a", "libc++abi.a",
                                       "libunwind.a"}) {
        targetRuntimeFiles.emplace_back("-Wl,-load_hidden," + nativeCxxRuntimeRoot +
                                        "/lib/" + std::string(library));
        arguments.push_back(targetRuntimeFiles.back().data());
      }
    }
    resourceOption = "-resource-dir";
    resourceDirectory = "/__llvm/lib/clang/23";
    if (!hasArgument(argc, argv, firstArgument, "-resource-dir") &&
        !hasArgumentPrefix(argc, argv, firstArgument, "-resource-dir=")) {
      arguments.push_back(resourceOption.data());
      arguments.push_back(resourceDirectory.data());
    }
    if (embeddedLibc && !hasUserSysroot(argc, argv, firstArgument)) {
      targetRoot = "/__llvm/targets/" + compileTarget->Requested;
      arguments.push_back(const_cast<char *>("-nostdlibinc"));
      if (embeddedCpp &&
          !hasArgument(argc, argv, firstArgument, "-nostdinc++")) {
        arguments.push_back(const_cast<char *>("-nostdinc++"));
        if (compileTarget->Requested.ends_with("-musl"))
          for (std::string_view option : {
                 "-D_LIBCPP_ABI_VERSION=1",
                 "-D_LIBCPP_ABI_NAMESPACE=__1",
                 "-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_NONE",
                 "-D_LIBCPP_HARDENING_MODE_DEFAULT=_LIBCPP_HARDENING_MODE_NONE",
                 "-D_LIBCPP_HAS_LOCALIZATION=1",
                 "-D_LIBCPP_HAS_THREADS=1",
                 "-D_LIBCPP_HAS_MONOTONIC_CLOCK=1",
                 "-D_LIBCPP_HAS_WIDE_CHARACTERS=1",
                 "-D_LIBCPP_HAS_UNICODE=1",
                 "-D_LIBCPP_HAS_FILESYSTEM=1",
                 "-D_LIBCPP_HAS_RANDOM_DEVICE=1",
                 "-D_LIBCPP_HAS_MUSL_LIBC=1",
                 "-D_LIBCPP_DISABLE_VISIBILITY_ANNOTATIONS",
                 "-D_LIBCXXABI_DISABLE_VISIBILITY_ANNOTATIONS"}) {
          targetCppOptions.emplace_back(option);
          arguments.push_back(targetCppOptions.back().data());
        }
        for (std::string_view include : {"c++", "cxxabi", "unwind"}) {
          targetCppOptions.emplace_back(targetRoot + "/include/" +
                                        std::string(include));
          arguments.push_back(const_cast<char *>("-isystem"));
          arguments.push_back(targetCppOptions.back().data());
        }
      }
      std::vector<std::string_view> includeLayers;
      if (compileTarget->Requested == "x86_64-linux-musl")
        includeLayers = {"x86_64-linux-musl", "generic-musl",
                         "x86-linux-any", "any-linux-any"};
      else if (compileTarget->Requested == "aarch64-linux-musl")
        includeLayers = {"aarch64-linux-musl", "generic-musl",
                         "aarch64-linux-any", "any-linux-any"};
      else if (compileTarget->Os == "windows")
        includeLayers = {"any-windows-any"};
      else if (gnuLinux) {
        includeLayers = {"gnu"};
        arguments.push_back(const_cast<char *>("-D__GLIBC_MINOR__=17"));
      }
      else
        includeLayers = {"wasm-wasi-musl", "generic-musl"};
      for (std::size_t index = 0; index < includeLayers.size(); ++index) {
        targetIncludes[index] = targetRoot + "/include/" +
                                std::string(includeLayers[index]);
        arguments.push_back(const_cast<char *>("-isystem"));
        arguments.push_back(targetIncludes[index].data());
      }
      if (useEmbeddedLibc && isDriverLink(argc, argv, firstArgument)) {
        arguments.push_back(const_cast<char *>("-nostdlib"));
        if (sharedLink && compileTarget->Os == "wasi") {
          if (!hasArgumentPrefix(argc, argv, firstArgument,
                                 "-fvisibility="))
            arguments.push_back(const_cast<char *>("-fvisibility=default"));
          if (!hasArgumentPrefix(argc, argv, firstArgument, "-Wl,--no-entry"))
            arguments.push_back(const_cast<char *>("-Wl,--no-entry"));
          arguments.push_back(const_cast<char *>("-Wl,--import-undefined"));
        }
        if (!sharedLink) {
          if (compileTarget->Os == "linux" && !dynamicExecutable)
            arguments.push_back(const_cast<char *>("-static"));
          if (dynamicExecutable &&
              !hasArgument(argc, argv, firstArgument, "-no-pie"))
            arguments.push_back(const_cast<char *>("-pie"));
          targetRuntimeFiles.emplace_back(targetRoot + "/lib/" +
              (compileTarget->Os == "wasi" ? "crt1-command.o" :
               compileTarget->Os == "windows" ? "crt2.obj" :
               gnuLinux ? "crt1.o" : dynamicExecutable &&
               !hasArgument(argc, argv, firstArgument, "-no-pie")
                   ? "Scrt1.o" : "crt1.o"));
          arguments.push_back(targetRuntimeFiles.back().data());
        } else if (compileTarget->Os == "windows") {
          targetRuntimeFiles.emplace_back(targetRoot + "/lib/dllcrt2.obj");
          arguments.push_back(targetRuntimeFiles.back().data());
        }
        if (gnuLinux) {
          targetRuntimeFiles.emplace_back(targetRoot + "/lib/crtbegin.o");
          arguments.push_back(targetRuntimeFiles.back().data());
          arguments.push_back(const_cast<char *>("-Wl,--start-group"));
        }
        if (embeddedCpp && !hasArgument(argc, argv, firstArgument, "-nostdlib++") &&
            (gnuLinux || compileTarget->Os == "windows")) {
          for (std::string_view name : {"libc++.a", "libc++abi.a", "libunwind.a"}) {
            targetRuntimeFiles.emplace_back(targetRoot + "/lib/" + std::string(name));
            arguments.push_back(targetRuntimeFiles.back().data());
          }
        }
        if (compileTarget->Os == "windows") {
          if (!hasArgumentPrefix(argc, argv, firstArgument, "-Wl,-e,") &&
              !hasArgumentPrefix(argc, argv, firstArgument, "-Wl,--entry")) {
            arguments.push_back(sharedLink
                ? const_cast<char *>("-Wl,-e,DllMainCRTStartup")
                : hasArgument(argc, argv, firstArgument, "-mwindows")
                    ? const_cast<char *>("-Wl,-e,WinMainCRTStartup")
                    : const_cast<char *>("-Wl,-e,mainCRTStartup"));
          }
          if (!sharedLink &&
              !hasArgument(argc, argv, firstArgument, "-mwindows"))
            arguments.push_back(const_cast<char *>("-Wl,--subsystem,console"));
          arguments.push_back(const_cast<char *>("-Wl,--start-group"));
          const std::string prefix = "root/targets/" +
              compileTarget->Requested + "/lib/";
          for (const Entry &entry : Entries) {
            if (!entry.Path.starts_with(prefix) ||
                !entry.Path.ends_with(".lib"))
              continue;
            targetRuntimeFiles.emplace_back("/__llvm/" + entry.Path.substr(5));
            arguments.push_back(targetRuntimeFiles.back().data());
          }
          arguments.push_back(const_cast<char *>("-Wl,--end-group"));
        } else if (gnuLinux) {
          for (std::string_view name : {"libc_nonshared.a", "libcompiler_rt.a", "libc.so.6",
                                       "libm.so.6", "libpthread.so.0", "libdl.so.2", "librt.so.1"}) {
            targetRuntimeFiles.emplace_back(targetRoot + "/lib/" + std::string(name));
            arguments.push_back(targetRuntimeFiles.back().data());
          }
          arguments.push_back(const_cast<char *>("-Wl,--end-group"));
          targetRuntimeFiles.emplace_back(targetRoot + "/lib/crtend.o");
          arguments.push_back(targetRuntimeFiles.back().data());
        } else {
          if (embeddedCpp &&
              !hasArgument(argc, argv, firstArgument, "-nostdlib++") &&
              !sharedLink) {
            for (std::string_view library : {"libc++.a", "libc++abi.a",
                                             "libunwind.a"}) {
              targetRuntimeFiles.emplace_back(targetRoot + "/lib/" +
                                              std::string(library));
              arguments.push_back(targetRuntimeFiles.back().data());
            }
          }
          if (compileTarget->Os == "linux" &&
              (dynamicExecutable || sharedLink)) {
            targetRuntimeFiles.emplace_back(targetRoot + "/lib/libc.so");
            arguments.push_back(targetRuntimeFiles.back().data());
          }
          for (std::string_view library : {"libc.a", "libzigc.a",
                                           "libcompiler_rt.a"}) {
            if (compileTarget->Os == "linux" &&
                (dynamicExecutable || sharedLink) && library != "libcompiler_rt.a")
              continue;
            if (compileTarget->Os == "linux" && sharedLink)
              continue;
            if (compileTarget->Os == "wasi" && sharedLink)
              continue;
            targetRuntimeFiles.emplace_back(targetRoot + "/lib/" +
                                            std::string(library));
            arguments.push_back(targetRuntimeFiles.back().data());
          }
        }
      }
    }
    if (invokedName != "clang-cpp" && invokedName != "clang-cl" &&
        driverLink) {
      if (!hasArgumentPrefix(argc, argv, firstArgument, "-fuse-ld=")) {
        linkerOption = "-fuse-ld=lld";
        arguments.push_back(linkerOption.data());
      }
      if ((crossTarget || !rawTarget.empty()) && !applePlatform &&
          !hasArgumentPrefix(argc, argv, firstArgument, "-B")) {
        embeddedBinOption = "-B/__llvm/bin";
        arguments.push_back(embeddedBinOption.data());
      }
      if (rawTarget.empty() && (compileTarget == nullptr || compileTarget->Os != "wasi")) {
        if (!hasArgumentPrefix(argc, argv, firstArgument, "--ld-path=")) {
          linkerPathOption = std::string("--ld-path=/__llvm/bin/") +
              (applePlatform ? "ld64.lld" : "ld.lld");
          arguments.push_back(linkerPathOption.data());
        }
      }
    }
  } else if (isFlangDriver(invokedName) && !recursiveFrontend &&
             isDriverLink(argc, argv, firstArgument)) {
    if (!hasArgumentPrefix(argc, argv, firstArgument, "-fuse-ld=")) {
      linkerOption = "-fuse-ld=lld";
      arguments.push_back(linkerOption.data());
    }
    if (!hasArgumentPrefix(argc, argv, firstArgument, "-B")) {
      linkerPathOption = "-B/__llvm/bin";
      arguments.push_back(linkerPathOption.data());
    }
    if (bundledRuntime &&
        !hasArgumentPrefix(argc, argv, firstArgument, "-L/__llvm/lib/clang/")) {
      runtimeLibraryOption = "-L/__llvm/lib/clang/23/lib/darwin";
      arguments.push_back(runtimeLibraryOption.data());
    }
    if (bundledRuntime &&
        !hasArgument(argc, argv, firstArgument, "-L/__llvm/lib")) {
      llvmLibraryOption = "-L/__llvm/lib";
      arguments.push_back(llvmLibraryOption.data());
    }
  }
  const auto originalEnd = arguments.begin() + originalArgumentsEnd;
  const auto separator = std::find_if(arguments.begin() + 1, originalEnd,
      [](const char *argument) { return std::string_view(argument) == "--"; });
  std::rotate(separator, originalEnd, arguments.end());
  arguments.push_back(nullptr);
  return entrypoint(static_cast<int>(arguments.size() - 1), arguments.data());
}

int runScript(const std::string &name, int argc, char **argv,
              int firstArgument) {
  if (const int result = runNativeWrapper(name, argc, argv, firstArgument);
      result != -1)
    return result;
  if (name == "lldb-python") {
    return runBundle("lldb", "lldb-python", argc, argv, firstArgument,
                     "_LLVM_lldb_python_main");
  }
  const Entry *entry = findEntry("scripts/" + name);
  if (!entry)
    return 127;
  std::string script = embeddedText(entry->Path);
  const std::size_t firstLineEnd = script.find('\n');
  const std::string_view firstLine(script.data(),
      firstLineEnd == std::string::npos ? script.size() : firstLineEnd);

  if (firstLine.find("python") != std::string_view::npos) {
    std::vector<std::string> storage;
    storage.reserve(static_cast<std::size_t>(argc - firstArgument + 4));
    storage.emplace_back("python3.14");
    storage.emplace_back("-c");
    storage.emplace_back(
        "import sys\n"
        "__file__ = '/__llvm/scripts/" + name + "'\n"
        "source = sys.argv.pop(1)\n"
        "sys.argv[0] = __file__\n"
        "exec(compile(source, __file__, 'exec'), globals(), globals())\n");
    storage.emplace_back(std::move(script));
    if (name == "update") {
      storage.emplace_back("--executable");
      storage.emplace_back(MainExecutablePath);
      storage.emplace_back("--current-version");
      storage.emplace_back(LLVM_CLI_VERSION);
    }
    for (int index = firstArgument; index < argc; ++index)
      storage.emplace_back(argv[index]);
    std::vector<char *> arguments;
    arguments.reserve(storage.size() + 1);
    for (std::string &argument : storage)
      arguments.push_back(argument.data());
    arguments.push_back(nullptr);
    return runBundle("python3.14", "python3.14",
                     static_cast<int>(storage.size()), arguments.data(), 1);
  }

  const std::string replacement =
      name == "scan-build" ? "scan-build-py" :
      name == "c++-analyzer" ? "analyze-c++" :
      name == "ccc-analyzer" ? "analyze-cc" : "";
  if (!replacement.empty())
    return runScript(replacement, argc, argv, firstArgument);

  if (name == "wcurl") {
    std::vector<std::string> storage{"curl", "--remote-name-all"};
    for (int index = firstArgument; index < argc; ++index)
      storage.emplace_back(argv[index]);
    std::vector<char *> arguments;
    for (std::string &argument : storage)
      arguments.push_back(argument.data());
    arguments.push_back(nullptr);
    return runBundle("curl", "curl", static_cast<int>(storage.size()),
                     arguments.data(), 1);
  }

  std::cerr << "LLVM: '" << name
            << "' has not yet been converted from its upstream interpreter "
               "script to a native embedded command\n";
  return 1;
}

std::string baseName(const char *path) {
  std::string value = path ? path : "LLVM";
#ifdef _WIN32
  const std::size_t slash = value.find_last_of("/\\");
  if (value.ends_with(".exe"))
    value.resize(value.size() - 4);
#else
  const std::size_t slash = value.find_last_of('/');
#endif
  return slash == std::string::npos ? value : value.substr(slash + 1);
}

constexpr std::array<std::pair<std::string_view, std::string_view>, 12>
    Shortcuts{{
        {"cc", "clang"},
        {"c++", "clang++"},
        {"cpp", "clang-cpp"},
        {"fortran", "flang"},
        {"link", "ld.lld"},
        {"debug", "lldb"},
        {"python", "python3"},
        {"format", "clang-format"},
        {"tidy", "clang-tidy"},
        {"disasm", "llvm-objdump"},
        {"symbols", "llvm-nm"},
        {"archive", "llvm-ar"},
    }};

std::string shortcutTarget(std::string_view requested) {
  for (const auto &[shortcut, target] : Shortcuts)
    if (shortcut == requested)
      return std::string(target);
  return {};
}

std::vector<std::string> toolNames() {
  std::vector<std::string> names{"llvm", "ld", "build", "licenses"};
  for (const Entry &entry : Entries) {
    constexpr std::string_view bundlePrefix = "bundles/";
    constexpr std::string_view aliasPrefix = "aliases/";
    if (entry.Path.starts_with(bundlePrefix) && entry.Path.ends_with(".bundle"))
      names.push_back(entry.Path.substr(bundlePrefix.size(),
                                        entry.Path.size() - bundlePrefix.size() - 7));
    else if (entry.Path.starts_with(aliasPrefix))
      names.push_back(entry.Path.substr(aliasPrefix.size()));
    else if (entry.Path.starts_with("scripts/"))
      names.push_back(entry.Path.substr(8));
  }
  for (const auto &[shortcut, target] : Shortcuts)
    names.emplace_back(shortcut);
  const std::size_t prefixedCount = names.size();
  for (std::size_t index = 0; index < prefixedCount; ++index)
    if (names[index].starts_with("llvm-") && names[index].size() > 5)
      names.push_back(names[index].substr(5));
  std::ranges::sort(names);
  names.erase(std::unique(names.begin(), names.end()), names.end());
  return names;
}

void listTools(std::string_view filter = {}) {
  const std::vector<std::string> names = toolNames();
  for (const std::string &name : names)
    if (filter.empty() || name.find(filter) != std::string::npos)
      std::cout << name << '\n';
}

void printVersion() {
  std::cout << "LLVM-CLI " LLVM_CLI_VERSION "\n"
               "LLVM " LLVM_CLI_LLVM_VERSION "\n"
               "Target: arm64-apple-darwin\n";
}

void printLinkerHelp() {
  std::cout <<
      "LLVM-CLI embedded cross-linker (LLD " LLVM_CLI_LLVM_VERSION ")\n"
      "Usage: llvm lld [linker arguments and input objects]\n"
      "       llvm ld  [linker arguments and input objects]\n"
      "Input objects select ELF, Mach-O, COFF, or WebAssembly.\n"
      "For ambiguous inputs use -flavor gnu|darwin|link|wasm.\n"
      "Direct names: ld.lld, ld64.lld, lld-link, wasm-ld.\n";
}

int printLicenses(std::string component) {
  if (component == "--help" || component == "-h") {
    std::cout << "Usage: llvm licenses [component]\n";
    return 0;
  }
  std::ranges::transform(component, component.begin(), [](unsigned char value) {
    return static_cast<char>(std::tolower(value));
  });
  constexpr std::string_view prefix = "root/share/licenses/";
  std::vector<std::string> names;
  bool found = false;
  for (const Entry &entry : Entries) {
    if (entry.Header.Type != llvm_pack::EntryType::File || !entry.Path.starts_with(prefix))
      continue;
    const std::size_t separator = entry.Path.find('/', prefix.size());
    if (separator == std::string::npos)
      continue;
    const std::string name = entry.Path.substr(prefix.size(), separator - prefix.size());
    names.push_back(name);
    if (name == component) {
      std::cout << "== " << entry.Path.substr(prefix.size()) << " ==\n"
                << embeddedText(entry.Path) << '\n';
      found = true;
    }
  }
  if (component.empty() && !names.empty()) {
    std::ranges::sort(names);
    names.erase(std::unique(names.begin(), names.end()), names.end());
    for (const std::string &name : names)
      std::cout << name << '\n';
    std::cout << "Read notices with: llvm licenses <component>\n";
    return 0;
  }
  if (!found)
    std::cerr << "LLVM: bundled license notices not found"
              << (component.empty() ? "" : " for ") << component << '\n';
  return found ? 0 : 1;
}

void printHelp() {
  std::cout <<
      "LLVM-CLI - the complete LLVM toolchain in one command\n\n"
      "Usage:\n"
      "  llvm <tool> [arguments]       Run any embedded tool\n"
      "  llvm <compiler> -compile-target <target> [arguments]\n"
      "  llvm build [options] <sources...>  Incremental project build\n"
      "  llvm clang -incremental file.c -o app  Cache a Clang build\n"
      "  llvm run <tool> [arguments]   Explicit tool invocation\n\n"
      "Cross compilation:\n"
      "  llvm cc -compile-target <arch-os-abi> file.c -c -o file.o\n"
      "  llvm cc -compile-target 'MIPS:BE:32:default' file.c -c\n"
      "  llvm c++ -compile-target <arch-os-abi> file.cpp -c\n"
      "  llvm c++ -O2 -compile-target <arch-os-abi> file.cpp -c\n"
      "  llvm fortran -compile-target <arch-os-abi> file.f90 -c\n"
      "  llvm llc -compile-target <arch-os-abi> module.ll -o module.o\n"
      "  llvm compile-targets          Show target and linking support\n"
      "  llvm compile-targets ghidra   Show Ghidra processor mappings\n\n"
      "Starting commands:\n"
      "  help                          Show this help\n"
      "  version                       Show LLVM-CLI and LLVM versions\n"
      "  update [--check]              Install a verified GitHub release\n"
      "  list [filter]                 List or search embedded commands\n"
      "  info <tool>                   Describe how a command is provided\n"
      "  extras [tool]                 Show LLVM-CLI tools and added flags\n"
      "  env                           Show virtual toolchain paths\n"
      "  doctor                        Check the embedded toolchain payload\n"
      "  licenses [component]          Read bundled license notices\n"
      "  build                         Compile only changed sources, then link\n"
      "  root                          Print the virtual root (/__llvm)\n\n"
      "Friendly shortcuts:\n"
      "  cc       -> clang             c++      -> clang++\n"
      "  cpp      -> clang-cpp         fortran  -> flang\n"
      "  ld/lld   -> infer embedded linker from inputs or flags\n"
      "  link     -> ld.lld           or use lld -flavor <name>\n"
      "  wasm-ld, ld.lld, and lld-link are also directly available\n"
      "  debug    -> lldb\n"
      "  python   -> python3           format   -> clang-format\n"
      "  tidy     -> clang-tidy        disasm   -> llvm-objdump\n"
      "  symbols  -> llvm-nm           archive  -> llvm-ar\n\n"
      "Examples:\n"
      "  llvm cc hello.c -o hello\n"
      "  llvm build --output hello hello.c\n"
      "  llvm c++ app.cpp -std=c++23 -o app\n"
      "  llvm fortran simulation.f90 -o simulation\n"
      "  llvm debug ./hello\n"
      "  llvm list clang\n"
      "  llvm info llvm-objdump\n"
      "  llvm nm ./hello\n"
      "  llvm clang --help\n";
}

void printCompileTargets(std::string_view filter = {}) {
  if (filter == "ghidra" || filter.starts_with("ghidra:")) {
    const std::string_view family = filter == "ghidra" ?
        std::string_view{} : filter.substr(7);
    const std::string catalog = embeddedText("root/share/ghidra-targets.txt");
    std::size_t supported = 0;
    std::size_t unsupported = 0;
    std::size_t start = 0;
    while (start < catalog.size()) {
      const std::size_t end = catalog.find('\n', start);
      const std::string_view line(catalog.data() + start,
          (end == std::string::npos ? catalog.size() : end) - start);
      const std::size_t separator = line.find("  ");
      const std::string_view id = line.substr(0, separator);
      if (!id.empty() &&
          (family.empty() || id.starts_with(std::string(family) + ":"))) {
        CompileTarget target;
        if (parseCompileTarget(id, target)) {
          ++supported;
          std::cout << id << " -> " << target.Triple;
          for (const std::string &option : target.DriverOptions)
            std::cout << ' ' << option;
          std::cout << '\n';
        } else {
          ++unsupported;
          std::cout << id << " -> unsupported\n";
        }
      }
      if (end == std::string::npos)
        break;
      start = end + 1;
    }
    std::cout << supported << " object/assembly mappings, " << unsupported
              << " unsupported profiles; no Ghidra target C runtime\n";
    return;
  }
  if (!filter.empty()) {
    CompileTarget target;
    if (parseCompileTarget(filter, target)) {
      std::cout << filter << " -> " << target.Requested << " -> "
                << target.Triple;
      for (const std::string &option : target.DriverOptions)
        std::cout << ' ' << option;
      std::cout << '\n';
      if (target.Os == "linux" &&
          !findEntry("root/targets/" + target.Requested + "/lib/libc.a"))
        std::cout << "  linking: supply a compatible sysroot, or use "
                     "x86_64-linux-musl/aarch64-linux-musl\n";
    } else {
      std::cout << (target.Error.empty() ? "invalid target" : target.Error)
                << '\n';
    }
    return;
  }
  std::cout <<
      "Compile targets accept arch-os-abi names and common LLVM triples.\n"
      "linux-arm64 and linux-x86_64 default to the embedded musl runtime.\n"
      "Examples: x86_64-linux-musl, aarch64-linux-gnu, "
      "x86_64-windows-gnu, wasm32-wasi, x86_64-macos, "
      "riscv64-freestanding.\n"
      "Object/assembly output: supported wherever this LLVM build has a "
      "compatible backend.\n"
      "Executable linking: macOS uses the embedded linker and Darwin libc resources. "
      "x86_64-linux-musl, aarch64-linux-musl, wasm32-wasi, and "
      "x86_64-windows-gnu use embedded headers and runtimes.\n"
      "Linux musl supports static executables by default and dynamic PIE "
      "executables with -dynamic.\n"
      "WASI supports command modules and experimental shared modules; "
      "runtime loading is host-dependent.\n"
      "x86_64-linux-musl also embeds libc++ for C++ linking.\n"
      "Ghidra processor IDs: llvm compile-targets ghidra "
      "(or ghidra:<family>).\n"
      "Mapped Ghidra processors emit freestanding objects/assembly only.\n"
      "Other targets require -nostdlib with your own entry/runtime, or "
      "an explicit --sysroot.\n";
}

bool printToolInfo(std::string requested) {
  if (requested == "licenses") {
    std::cout << "licenses\n  kind: embedded license notices\n"
                 "  syntax: llvm licenses [component]\n";
    return true;
  }
  if (requested == "build") {
    std::cout << "build\n  kind: incremental in-binary build command\n"
                 "  syntax: llvm build --output APP [--target TARGET] SOURCE...\n"
                 "  help: llvm build --help\n";
    return true;
  }
  if (requested == "ld" || requested == "lld") {
    std::cout << requested
              << "\n  kind: in-process LLD cross-linker dispatcher"
                 "\n  formats: ELF, Mach-O, COFF, WebAssembly"
                 "\n  override: llvm " << requested
              << " -flavor <gnu|darwin|link|wasm> [arguments]\n";
    return true;
  }
  const std::string shortcut = shortcutTarget(requested);
  if (!shortcut.empty()) {
    std::cout << requested << "\n  kind: shortcut\n  target: " << shortcut
              << "\n  run: llvm " << requested << " [arguments]\n";
    return true;
  }
  const std::string canonical = canonicalTool(requested);
  if (canonical != requested) {
    std::cout << requested << "\n  kind: LLVM tool shorthand\n  target: "
              << canonical << "\n  run: llvm " << requested
              << " [arguments]\n";
    return true;
  }
  if (const Entry *alias = findEntry("aliases/" + requested)) {
    std::cout << requested << "\n  kind: alias\n  target: "
              << embeddedText(alias->Path) << "\n  path: /__llvm/bin/"
              << requested << '\n';
    return true;
  }
  if (findEntry("bundles/" + requested + ".bundle")) {
    std::cout << requested << "\n  kind: native embedded module\n  path: "
              << "/__llvm/bin/" << requested << '\n';
    return true;
  }
  if (requested == "lldb-python") {
    std::cout << requested
              << "\n  kind: native entrypoint in embedded LLDB module"
                 "\n  module: lldb\n  path: /__llvm/bin/lldb-python\n";
    return true;
  }
  if (isNativeWrapper(requested)) {
    std::cout << requested
              << "\n  kind: native command compiled into LLVM-CLI"
                 "\n  path: /__llvm/bin/" << requested << '\n';
    return true;
  }
  if (hasScript(requested)) {
    std::cout << requested << "\n  kind: embedded script\n  path: "
              << "/__llvm/scripts/" << requested << '\n';
    return true;
  }
  const std::string resolved = resolveTool(requested);
  if (!resolved.empty()) {
    std::cout << requested << "\n  kind: LLVM multicall command\n  module: "
              << resolved << "\n  path: /__llvm/bin/" << requested << '\n';
    return true;
  }
  return false;
}

bool printExtras(std::string requested = {}) {
  if (requested.empty()) {
    std::cout <<
        "LLVM-CLI extras\n"
        "  llvm extras <tool>          Added flags and command mapping\n"
        "  llvm list [filter]          Search embedded tools and short names\n"
        "  llvm info <tool>            Show a tool's embedded implementation\n"
        "  llvm compile-targets        Show cross-target runtime coverage\n"
        "  llvm doctor                 Check the packed payload\n\n"
        "  llvm update [--check]       Install or check a verified GitHub release\n"
        "  llvm build                  Incremental C/C++ project build\n\n"
        "Added flag (compiler commands):\n"
        "  llvm <compiler> -compile-target <arch-os-abi> [arguments]\n"
        "  llvm clang|c++ -incremental [Clang arguments]\n"
        "  Applies to cc, c++, cpp, clang, clang++, clang-cpp, "
        "clang-cl, flang, flang-new, fortran, and llc.\n"
        "  Use llvm extras <compiler> for its mapping.\n\n"
        "Short names:\n"
        "  llvm nm -> llvm-nm; llvm ar -> llvm-ar; "
        "llvm objdump -> llvm-objdump.\n"
        "  llvm list shows every available name.\n";
    return true;
  }
  if (!std::ranges::binary_search(toolNames(), requested))
    return false;
  if (requested == "build") {
    std::cout << "build\n  syntax: llvm build [options] SOURCE...\n"
                 "  flags: --target, --output, --build-dir, --release, "
                 "--cflag, --ldflag\n";
    return true;
  }
  const std::string shortcut = shortcutTarget(requested);
  const std::string canonical = canonicalTool(
      shortcut.empty() ? requested : shortcut);
  std::cout << requested << "\n  embedded tool: " << canonical << '\n';
  if (isCompilerCommand(canonical)) {
    std::cout << "  added flag: -compile-target <arch-os-abi>\n"
              << "  syntax: llvm " << requested
              << " -compile-target <target> [arguments]\n"
              << "  targets: llvm compile-targets\n";
    if (canonical == "clang" || canonical == "clang++")
      std::cout << "  added flag: -incremental (optional -build-dir PATH)\n";
  } else {
    std::cout << "  added flags: none\n"
              << "  syntax: llvm " << requested << " [arguments]\n";
  }
  if (canonical != requested && !canonical.starts_with("llvm-"))
    std::cout << "  shorthand for: llvm " << canonical << '\n';
  return true;
}

int doctor() {
  std::size_t bundles = 0;
  std::size_t aliases = 0;
  std::size_t scripts = 0;
  for (const Entry &entry : Entries) {
    bundles += entry.Path.starts_with("bundles/") &&
               entry.Path.ends_with(".bundle");
    aliases += entry.Path.starts_with("aliases/");
    scripts += entry.Path.starts_with("scripts/");
  }
  const std::array required{
      "bundles/flang-23.bundle", "bundles/lldb.bundle",
      "bundles/python3.14.bundle",
      "root/include/llvm/ADT/StringRef.h",
      "root/include/clang/Basic/Version.h",
      "root/lib/clang/23/lib/darwin/libclang_rt.osx.a",
      "root/deps/lib/python314.zip",
      "root/share/ghidra-targets.txt",
      "root/targets/x86_64-linux-musl/include/generic-musl/stdio.h",
      "root/targets/x86_64-linux-musl/lib/crt1.o",
      "root/targets/x86_64-linux-musl/lib/Scrt1.o",
      "root/targets/x86_64-linux-musl/lib/libc.a",
      "root/targets/x86_64-linux-musl/lib/libc.so",
      "root/targets/x86_64-linux-musl/include/c++/iostream",
      "root/targets/x86_64-linux-musl/lib/libc++.a",
      "root/targets/aarch64-linux-musl/include/generic-musl/stdio.h",
      "root/targets/aarch64-linux-musl/lib/crt1.o",
      "root/targets/aarch64-linux-musl/lib/Scrt1.o",
      "root/targets/aarch64-linux-musl/lib/libc.a",
      "root/targets/aarch64-linux-musl/lib/libc.so",
      "root/targets/wasm32-wasi/include/wasm-wasi-musl/stdio.h",
      "root/targets/wasm32-wasi/lib/crt1-command.o",
      "root/targets/wasm32-wasi/lib/libc.a",
      "root/targets/x86_64-windows-gnu/include/any-windows-any/stdio.h",
      "root/targets/x86_64-windows-gnu/lib/crt2.obj",
      "root/targets/x86_64-windows-gnu/lib/dllcrt2.obj",
      "root/targets/x86_64-windows-gnu/lib/zigc.lib"};
  bool healthy = true;
  for (const char *path : required)
    healthy &= findEntry(path) != nullptr;
  std::printf("LLVM-CLI doctor\n"
              "  payload: %s\n"
              "  packed entries: %zu\n"
              "  native modules: %zu\n"
              "  aliases: %zu\n"
              "  scripts: %zu\n"
              "  exposed commands: %zu\n"
              "  virtual root: /__llvm (memory only)\n"
              "  SDKROOT: %s\n",
              healthy ? "healthy" : "incomplete", Entries.size(), bundles,
              aliases, scripts, toolNames().size(),
              std::getenv("SDKROOT") ? std::getenv("SDKROOT") : "unset");
  return healthy ? 0 : 1;
}

} // namespace

#ifndef _WIN32
extern "C" __attribute__((visibility("default"))) int
open(const char *path, int flags, ...) {
  if (path && std::string_view(path).starts_with("/__llvm/")) {
    const std::string packedPath = packedPathForVirtual(path);
    if (const Entry *entry = findResolvedEntry(packedPath)) {
      if (entry->Header.Type != llvm_pack::EntryType::File ||
          (flags & O_ACCMODE) != O_RDONLY) {
        errno = EACCES;
        return -1;
      }
      VirtualFile file;
      file.Bytes = decompress(*entry);
      file.Inode = virtualInode(path);
      if (file.Bytes.empty() && entry->Header.OriginalSize != 0) {
        errno = EIO;
        return -1;
      }
      std::lock_guard lock(VirtualFilesMutex);
      const int fd = NextVirtualFd++;
      VirtualFiles.emplace(fd, std::move(file));
      return fd;
    }
    errno = ENOENT;
    return -1;
  }

  using Function = int (*)(const char *, int, ...);
  static auto realOpen = reinterpret_cast<Function>(::dlsym(RTLD_NEXT, "open"));
  if (flags & O_CREAT) {
    va_list arguments;
    va_start(arguments, flags);
    const mode_t mode = static_cast<mode_t>(va_arg(arguments, int));
    va_end(arguments);
    return realOpen(path, flags, mode);
  }
  return realOpen(path, flags);
}

extern "C" __attribute__((visibility("default"))) ssize_t
read(int fd, void *buffer, size_t count) {
  std::lock_guard lock(VirtualFilesMutex);
  if (auto found = VirtualFiles.find(fd); found != VirtualFiles.end()) {
    VirtualFile &file = found->second;
    const std::size_t available = file.Offset < file.Bytes.size()
        ? file.Bytes.size() - static_cast<std::size_t>(file.Offset) : 0;
    const std::size_t amount = std::min(count, available);
    std::memcpy(buffer, file.Bytes.data() + file.Offset, amount);
    file.Offset += static_cast<off_t>(amount);
    return static_cast<ssize_t>(amount);
  }
  using Function = ssize_t (*)(int, void *, size_t);
  static auto realRead = reinterpret_cast<Function>(::dlsym(RTLD_NEXT, "read"));
  return realRead(fd, buffer, count);
}

extern "C" __attribute__((visibility("default"))) ssize_t
pread(int fd, void *buffer, size_t count, off_t offset) {
  std::lock_guard lock(VirtualFilesMutex);
  if (auto found = VirtualFiles.find(fd); found != VirtualFiles.end()) {
    const VirtualFile &file = found->second;
    const std::size_t start = offset < 0 ? file.Bytes.size()
                                         : static_cast<std::size_t>(offset);
    const std::size_t available = start < file.Bytes.size()
        ? file.Bytes.size() - start : 0;
    const std::size_t amount = std::min(count, available);
    std::memcpy(buffer, file.Bytes.data() + start, amount);
    return static_cast<ssize_t>(amount);
  }
  using Function = ssize_t (*)(int, void *, size_t, off_t);
  static auto realPread = reinterpret_cast<Function>(::dlsym(RTLD_NEXT, "pread"));
  return realPread(fd, buffer, count, offset);
}

extern "C" __attribute__((visibility("default"))) off_t
lseek(int fd, off_t offset, int whence) {
  std::lock_guard lock(VirtualFilesMutex);
  if (auto found = VirtualFiles.find(fd); found != VirtualFiles.end()) {
    VirtualFile &file = found->second;
    off_t base = 0;
    if (whence == SEEK_CUR)
      base = file.Offset;
    else if (whence == SEEK_END)
      base = static_cast<off_t>(file.Bytes.size());
    else if (whence != SEEK_SET) {
      errno = EINVAL;
      return -1;
    }
    if (offset < -base) {
      errno = EINVAL;
      return -1;
    }
    file.Offset = base + offset;
    return file.Offset;
  }
  using Function = off_t (*)(int, off_t, int);
  static auto realLseek = reinterpret_cast<Function>(::dlsym(RTLD_NEXT, "lseek"));
  return realLseek(fd, offset, whence);
}

extern "C" __attribute__((visibility("default"))) int
close(int fd) {
  {
    std::lock_guard lock(VirtualFilesMutex);
    if (VirtualFiles.erase(fd))
      return 0;
  }
  using Function = int (*)(int);
  static auto realClose = reinterpret_cast<Function>(::dlsym(RTLD_NEXT, "close"));
  return realClose(fd);
}

extern "C" __attribute__((visibility("default"))) int
fstat(int fd, struct stat *status) {
  std::lock_guard lock(VirtualFilesMutex);
  if (auto found = VirtualFiles.find(fd); found != VirtualFiles.end()) {
    std::memset(status, 0, sizeof(*status));
    status->st_dev = static_cast<dev_t>(0x4c564d);
    status->st_ino = found->second.Inode;
    status->st_mode = S_IFREG | 0444;
    status->st_nlink = 1;
    status->st_size = static_cast<off_t>(found->second.Bytes.size());
    return 0;
  }
  using Function = int (*)(int, struct stat *);
  static auto realFstat = reinterpret_cast<Function>(::dlsym(RTLD_NEXT, "fstat"));
  return realFstat(fd, status);
}

extern "C" __attribute__((visibility("default"))) int
stat(const char *path, struct stat *status) {
  if (path && std::string_view(path).starts_with("/__llvm/")) {
    const std::string packedPath = packedPathForVirtual(path);
    if (const Entry *entry = findResolvedEntry(packedPath)) {
      std::memset(status, 0, sizeof(*status));
      status->st_dev = static_cast<dev_t>(0x4c564d);
      status->st_ino = virtualInode(path);
      status->st_mode = (entry->Header.Type == llvm_pack::EntryType::Directory
                             ? S_IFDIR
                             : S_IFREG) |
                        (entry->Header.Mode & 0777);
      status->st_nlink = 1;
      status->st_size = static_cast<off_t>(entry->Header.OriginalSize);
      return 0;
    }
    errno = ENOENT;
    return -1;
  }
  using Function = int (*)(const char *, struct stat *);
  static auto realStat = reinterpret_cast<Function>(::dlsym(RTLD_NEXT, "stat"));
  return realStat(path, status);
}

extern "C" __attribute__((visibility("default"))) int
fcntl(int fd, int command, ...) {
  {
    std::lock_guard lock(VirtualFilesMutex);
    if (VirtualFiles.contains(fd)) {
      if (command == F_GETFD)
        return FD_CLOEXEC;
      if (command == F_SETFD)
        return 0;
      errno = EINVAL;
      return -1;
    }
  }
  using Function = int (*)(int, int, ...);
  static auto realFcntl = reinterpret_cast<Function>(::dlsym(RTLD_NEXT, "fcntl"));
  if (command == F_GETFD || command == F_GETFL || command == F_GETOWN)
    return realFcntl(fd, command);
  va_list arguments;
  va_start(arguments, command);
  void *argument = va_arg(arguments, void *);
  va_end(arguments);
  return realFcntl(fd, command, argument);
}
#endif

extern "C" LLVM_CLI_EXPORT void
LLVMEnumerateEmbeddedFiles(
    void (*visitor)(const char *, const void *, std::size_t, void *),
    void *context) {
  for (const Entry &entry : Entries) {
    if (entry.Header.Type != llvm_pack::EntryType::File ||
        !entry.Path.starts_with("root/"))
      continue;
    const bool resourceHeader =
        entry.Path.starts_with("root/lib/clang/23/include/");
    const bool fortranModule =
        entry.Path.starts_with("root/lib/clang/23/finclude/");
    const bool nativeCxxHeader = ActiveNativeCxxHeaders &&
        entry.Path.starts_with("root/include/c++/v1/");
    const bool sdkHeader = ActiveSdkHeaders &&
        (entry.Path.starts_with("root/include/") || entry.Path.starts_with("root/deps/include/"));
    const bool darwinResource = entry.Path.starts_with("root/targets/macos/") &&
        std::getenv("SDKROOT") &&
        std::string_view(std::getenv("SDKROOT")) == "/__llvm/targets/macos";
    const bool targetResource = !ActiveCompileTarget.empty() &&
        entry.Path.starts_with("root/targets/" + ActiveCompileTarget +
                               "/include/");
#ifdef _WIN32
    // Windows cannot use POSIX stat/open interposition. Clang checks the
    // existence of explicit archive and startup inputs through its VFS.
    const bool targetLibraries = !ActiveCompileTarget.empty() &&
        entry.Path.starts_with("root/targets/" + ActiveCompileTarget + "/lib/");
#else
    const bool targetLibraries = false;
#endif
    if (!resourceHeader && !fortranModule && !targetResource && !nativeCxxHeader &&
        !sdkHeader && !darwinResource && !targetLibraries)
      continue;
    const std::vector<char> bytes = decompress(entry);
    if (bytes.empty() && entry.Header.OriginalSize != 0)
      continue;
    const std::string virtualPath = "/__llvm/" + entry.Path.substr(5);
    visitor(virtualPath.c_str(), bytes.data(), bytes.size(), context);
  }
  static const char Empty = 0;
  for (const Entry &entry : Entries) {
    std::string name;
    if (entry.Path.starts_with("aliases/"))
      name = entry.Path.substr(8);
    else if (entry.Path.starts_with("bundles/") &&
             entry.Path.ends_with(".bundle"))
      name = entry.Path.substr(8, entry.Path.size() - 15);
    if (!name.empty()) {
      const std::string virtualPath = "/__llvm/bin/" + name;
      visitor(virtualPath.c_str(), &Empty, 0, context);
    }
  }
}

extern "C" LLVM_CLI_EXPORT bool
LLVMHasEmbeddedTool(const char *path) {
  const std::string requested = canonicalTool(baseName(path));
  const bool found = path &&
      (!resolveTool(requested).empty() || hasScript(requested));
  if (std::getenv("LLVM_CLI_DEBUG"))
    std::cerr << "LLVM: embedded tool probe " << (path ? path : "(null)")
              << " -> " << (found ? "yes" : "no") << '\n';
  return found;
}

#ifdef _WIN32
extern "C" LLVM_CLI_EXPORT bool
LLVMReadEmbeddedFile(const char *path,
                     void (*visitor)(const void *, std::size_t, void *),
                     void *context) {
  std::string normalized(path);
  std::ranges::replace(normalized, '\\', '/');
  if (normalized.size() > 2 && normalized[1] == ':' &&
      std::string_view(normalized).substr(2).starts_with("/__llvm/"))
    normalized.erase(0, 2);
  const Entry *entry = findResolvedEntry(packedPathForVirtual(normalized));
  if (!entry || entry->Header.Type != llvm_pack::EntryType::File)
    return false;
  const auto bytes = decompress(*entry);
  if (bytes.empty() && entry->Header.OriginalSize)
    return false;
  visitor(bytes.data(), bytes.size(), context);
  return true;
}
#endif

extern "C" LLVM_CLI_EXPORT const char *
LLVMExecutablePath() {
  return MainExecutablePath.c_str();
}

extern "C" LLVM_CLI_EXPORT int
LLVMRunEmbeddedTool(const char *program, const char *const *arguments,
                    std::size_t argumentCount) {
  if (!CurrentEntrypoint || argumentCount == 0)
    return 127;
#ifdef _WIN32
  std::vector<std::string> worker{"--llvm-internal-worker",
                                  canonicalTool(baseName(program)), ActiveCompileTarget};
  for (std::size_t index = 1; index < argumentCount; ++index)
    worker.emplace_back(arguments[index]);
  return runWindowsWorker(worker);
#else
  const pid_t child = ::fork();
  if (child < 0)
    return 127;
  if (child == 0) {
    std::vector<char *> mutableArguments;
    mutableArguments.reserve(argumentCount + 1);
    for (std::size_t index = 0; index < argumentCount; ++index)
      mutableArguments.push_back(const_cast<char *>(arguments[index]));
    mutableArguments.push_back(nullptr);
    const std::string requested = canonicalTool(baseName(program));
    const std::string canonicalPath = "/__llvm/bin/" + requested;
    if (requested != baseName(program))
      mutableArguments[0] = const_cast<char *>(canonicalPath.c_str());
    const std::string bundle = resolveTool(requested);
    const int result = bundle.empty() || bundle == CurrentBundleName
        ? CurrentEntrypoint(static_cast<int>(argumentCount),
                            mutableArguments.data())
        : runBundle(bundle, requested, static_cast<int>(argumentCount),
                    mutableArguments.data(), 1);
    ::_exit(result);
  }
  int status = 0;
  while (::waitpid(child, &status, 0) < 0 && errno == EINTR) {
  }
  if (WIFEXITED(status))
    return WEXITSTATUS(status);
  if (WIFSIGNALED(status))
    return 128 + WTERMSIG(status);
  return 127;
#endif
}

int main(int argc, char **argv) {
#if defined(_WIN32)
  std::wstring path(32768, L'\0');
  const DWORD length = ::GetModuleFileNameW(nullptr, path.data(), static_cast<DWORD>(path.size()));
  if (!length || length >= path.size())
    return 127;
  path.resize(length);
  MainExecutableWidePath = std::move(path);
  MainExecutablePath = utf8(MainExecutableWidePath.c_str());
  int wideCount = 0;
  wchar_t **wideArguments = ::CommandLineToArgvW(::GetCommandLineW(), &wideCount);
  if (!wideArguments)
    return 127;
  std::vector<std::string> utf8Arguments;
  for (int index = 0; index < wideCount; ++index)
    utf8Arguments.push_back(utf8(wideArguments[index]));
  ::LocalFree(wideArguments);
  std::vector<char *> utf8Pointers;
  for (auto &argument : utf8Arguments)
    utf8Pointers.push_back(argument.data());
  utf8Pointers.push_back(nullptr);
  argc = wideCount;
  argv = utf8Pointers.data();
#elif defined(__APPLE__)
  uint32_t executablePathSize = 0;
  if (_NSGetExecutablePath(nullptr, &executablePathSize) == -1 &&
      executablePathSize != 0) {
    std::vector<char> executablePath(executablePathSize);
    if (_NSGetExecutablePath(executablePath.data(), &executablePathSize) == 0)
      MainExecutablePath = executablePath.data();
  }
#elif defined(__linux__)
  std::array<char, 4096> executablePath{};
  const ssize_t pathLength = ::readlink(
      "/proc/self/exe", executablePath.data(), executablePath.size() - 1);
  if (pathLength > 0)
    MainExecutablePath.assign(executablePath.data(),
                              static_cast<std::size_t>(pathLength));
#endif
  if (MainExecutablePath.empty() && argc > 0 && argv[0])
    MainExecutablePath = argv[0];

  if (!initializeIndex()) {
    std::cerr << "LLVM: embedded payload is missing or corrupt\n";
    return 1;
  }
#ifdef _WIN32
  if (argc >= 4 && std::string_view(argv[1]) == "--llvm-internal-worker") {
    const std::string name = canonicalTool(argv[2]);
    const std::string bundle = resolveTool(name);
    ActiveCompileTarget = argv[3];
    return bundle.empty() ? 127 : runBundle(bundle, name, argc, argv, 4);
  }
#endif
  const std::string invokedAs = baseName(argc ? argv[0] : nullptr);
  const bool dispatcher = invokedAs == "LLVM" || invokedAs == "llvm" ||
      (resolveTool(invokedAs).empty() && !hasScript(invokedAs));
  std::string targetCommand = invokedAs;
  std::string targetCommandName = invokedAs;
  if (dispatcher) {
    const int commandIndex = argc > 1 &&
        (std::string_view(argv[1]) == "run" ||
         std::string_view(argv[1]) == "tool") ? 2 : 1;
    if (argc > commandIndex && argv[commandIndex][0] != '-') {
      targetCommandName = argv[commandIndex];
      targetCommand = targetCommandName;
    } else {
      targetCommand.clear();
    }
  }
  if (!targetCommand.empty()) {
    if (const std::string shortcut = shortcutTarget(targetCommand);
        !shortcut.empty())
      targetCommand = shortcut;
    targetCommand = canonicalTool(std::move(targetCommand));
  }
  CompileTarget compileTarget;
  bool selectedTarget = false;
  std::vector<char *> filteredArguments;
  filteredArguments.reserve(static_cast<std::size_t>(argc) + 1);
  filteredArguments.push_back(argv[0]);
  for (int index = 1; index < argc; ++index) {
    const std::string_view argument(argv[index]);
    if (argument == "--") {
      filteredArguments.insert(filteredArguments.end(), argv + index, argv + argc);
      break;
    }
    if (argument == "-compile-target" ||
        argument.starts_with("-compile-target=")) {
      if (!targetCommand.empty() && !isCompilerCommand(targetCommand)) {
        std::cerr << "LLVM: -compile-target is only for compiler commands; '"
                  << targetCommandName << "' is not a compiler\n";
        return 2;
      }
      if (selectedTarget) {
        std::cerr << "LLVM: -compile-target may be specified only once\n";
        return 2;
      }
      const std::string_view value = argument == "-compile-target"
          ? (index + 1 < argc ? std::string_view(argv[++index])
                              : std::string_view{})
          : argument.substr(sizeof("-compile-target=" ) - 1);
      if (!parseCompileTarget(value, compileTarget)) {
        if (!compileTarget.Error.empty())
          std::cerr << "LLVM: " << compileTarget.Error << '\n';
        else
          std::cerr << "LLVM: expected -compile-target <arch-os-abi>, "
                       "<LLVM triple>, or <Ghidra processor ID>\n";
        return 2;
      }
      selectedTarget = true;
      continue;
    }
    filteredArguments.push_back(argv[index]);
  }
  filteredArguments.push_back(nullptr);
  argc = static_cast<int>(filteredArguments.size() - 1);
  argv = filteredArguments.data();
  if (selectedTarget)
    ActiveCompileTarget = compileTarget.Requested;
  if (selectedTarget && (invokedAs == "LLVM" || invokedAs == "llvm") &&
      argc > 2 && argv[1][0] == '-' &&
      std::string_view(argv[1]) != "--help" &&
      std::string_view(argv[1]) != "-h") {
    for (int index = 2; index < argc; ++index) {
      const std::string shortcut = shortcutTarget(argv[index]);
      const std::string_view candidate = shortcut.empty()
          ? std::string_view(argv[index]) : std::string_view(shortcut);
      if (isCompilerCommand(candidate)) {
        char *command = argv[index];
        for (int shift = index; shift > 1; --shift)
          argv[shift] = argv[shift - 1];
        argv[1] = command;
        break;
      }
    }
  }
  const BuildInvoke invokeBuildCompiler = [&](std::string_view compiler,
      std::string_view target, const std::vector<std::string> &options) {
#if defined(__linux__)
    if (target.empty() || target == "native")
      target = "x86_64-linux-gnu";
#elif defined(_WIN32)
    if (target.empty() || target == "native")
      target = "x86_64-windows-gnu";
#endif
    CompileTarget parsed;
    if (!target.empty() && !parseCompileTarget(target, parsed)) {
      std::cerr << "LLVM build: invalid target '" << target << "'\n";
      return 2;
    }
    if (target.empty() || parsed.Os == "macos")
      configureAppleSdk();
    ActiveCompileTarget = target.empty() ? "" : parsed.Requested;
#ifdef _WIN32
    std::vector<std::string> command{std::string(compiler)};
    if (!target.empty()) {
      command.emplace_back("-compile-target");
      command.emplace_back(target);
    }
    command.insert(command.end(), options.begin(), options.end());
    return runWindowsWorker(command);
#else
    const pid_t child = ::fork();
    if (child < 0) {
      std::cerr << "LLVM build: cannot start embedded compiler: "
                << std::strerror(errno) << '\n';
      return 1;
    }
    if (child == 0) {
      std::vector<std::string> storage{"llvm"};
      storage.insert(storage.end(), options.begin(), options.end());
      std::vector<char *> arguments;
      for (std::string &option : storage)
        arguments.push_back(option.data());
      arguments.push_back(nullptr);
      const int result = runBundle("llvm", std::string(compiler),
          static_cast<int>(storage.size()), arguments.data(), 1, {},
          target.empty() ? nullptr : &parsed);
      std::cout.flush();
      std::cerr.flush();
      ::_exit(result);
    }
    int status = 0;
    while (::waitpid(child, &status, 0) < 0) {
      if (errno == EINTR)
        continue;
      return 1;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) :
           WIFSIGNALED(status) ? 128 + WTERMSIG(status) : 1;
#endif
  };
  std::string requested;
  int firstArgument = 1;
  if (dispatcher) {
    if (argc < 2) {
      printHelp();
      return 0;
    }
    const std::string_view command(argv[1]);
    if (command == "--help" || command == "-h" || command == "help") {
      if (argc >= 3) {
        if (!printToolInfo(argv[2])) {
          std::cerr << "LLVM: unknown tool '" << argv[2] << "'\n";
          return 1;
        }
        std::cout << "  help: llvm " << argv[2] << " --help\n";
      } else {
        printHelp();
      }
      return 0;
    }
    if (command == "--version" || command == "-V" || command == "version") {
      printVersion();
      return 0;
    }
    if (command == "licenses") {
      if (argc > 3) {
        std::cerr << "Usage: llvm licenses [component]\n";
        return 2;
      }
      return printLicenses(argc == 3 ? argv[2] : "");
    }
    if (command == "compile-targets") {
      printCompileTargets(argc >= 3 ? std::string_view(argv[2]) :
                                        std::string_view{});
      return 0;
    }
    if (command == "build") {
      return runIncrementalBuild(argc, argv, 2, MainExecutablePath,
          invokeBuildCompiler);
    }
    if (command == "extras") {
      if (!printExtras(argc >= 3 ? argv[2] : "")) {
        std::cerr << "LLVM: unknown tool '" << argv[2] << "'\n";
        return 1;
      }
      return 0;
    }
    if (command == "--list" || command == "list") {
      listTools(argc >= 3 ? std::string_view(argv[2]) : std::string_view{});
      return 0;
    }
    if (command == "--root" || command == "root") {
      std::cout << "/__llvm\n";
      return 0;
    }
    if (command == "env") {
      configureAppleSdk();
      std::cout << "LLVM_ROOT=/__llvm\n"
                   "LLVM_BIN=/__llvm/bin\n"
                   "LLVM_INCLUDE=/__llvm/include\n"
                   "LLVM_LIBRARY=/__llvm/lib\n"
                   "LLVM_DEPENDENCIES=/__llvm/deps\n"
                << "SDKROOT="
                << (std::getenv("SDKROOT") ? std::getenv("SDKROOT") : "")
                << '\n';
      return 0;
    }
    if (command == "doctor") {
      configureAppleSdk();
      return doctor();
    }
    if (command == "info") {
      if (argc < 3) {
        std::cerr << "usage: llvm info <tool>\n";
        return 2;
      }
      if (!printToolInfo(argv[2])) {
        std::cerr << "LLVM: unknown tool '" << argv[2] << "'\n";
        return 1;
      }
      return 0;
    }
    if (command == "run" || command == "tool") {
      if (argc < 3) {
        std::cerr << "usage: llvm " << command << " <tool> [arguments]\n";
        return 2;
      }
      requested = argv[2];
      firstArgument = 3;
    } else {
      requested = argv[1];
      firstArgument = 2;
    }
    if (const std::string target = shortcutTarget(requested); !target.empty())
      requested = target;
  } else {
    requested = invokedAs;
  }
  requested = canonicalTool(std::move(requested));
#if defined(__linux__) || defined(_WIN32)
  if (isClangDriver(requested) && requested != "clang-cl" &&
      (!selectedTarget || compileTarget.Requested == "native") &&
      (firstArgument >= argc || !std::string_view(argv[firstArgument]).starts_with("-cc1")) &&
      !hasArgument(argc, argv, firstArgument, "-target") &&
      !hasArgument(argc, argv, firstArgument, "--target") &&
      !hasArgumentPrefix(argc, argv, firstArgument, "--target=") &&
      !hasArgumentPrefix(argc, argv, firstArgument, "-target=")) {
#ifdef _WIN32
    parseCompileTarget("x86_64-windows-gnu", compileTarget);
#else
    parseCompileTarget("x86_64-linux-gnu", compileTarget);
#endif
    selectedTarget = true;
    ActiveCompileTarget = compileTarget.Requested;
  }
#endif
  if (requested == "ld" || requested == "lld") {
    if (!hasArgument(argc, argv, firstArgument, "-flavor") &&
        !hasArgumentPrefix(argc, argv, firstArgument, "-flavor=") &&
        (hasArgument(argc, argv, firstArgument, "--help") ||
         hasArgument(argc, argv, firstArgument, "-h"))) {
      printLinkerHelp();
      return 0;
    }
    if (hasArgument(argc, argv, firstArgument, "--version") &&
        argc == firstArgument + 1) {
      std::cout << "LLD " LLVM_CLI_LLVM_VERSION " (embedded; ELF, Mach-O, COFF, WebAssembly)\n";
      return 0;
    }
    if (hasArgument(argc, argv, firstArgument, "-flavor") ||
        hasArgumentPrefix(argc, argv, firstArgument, "-flavor=")) {
      requested = "lld";
    } else {
      const LinkFlavor flavor = inferLinkFlavor(argc, argv, firstArgument);
      if (flavor == LinkFlavor::Conflict) {
        std::cerr << "LLVM: linker inputs or flags use conflicting formats; "
                     "select one with -flavor <gnu|darwin|link|wasm>\n";
        return 2;
      }
      if (flavor == LinkFlavor::Unknown) {
        if (hasArgument(argc, argv, firstArgument, "--version") ||
            hasArgument(argc, argv, firstArgument, "-v"))
          requested = "lld";
        else {
          std::cerr << "LLVM: cannot infer linker format; pass an object "
                       "file or -flavor <gnu|darwin|link|wasm>\n";
          return 2;
        }
      } else {
        requested = flavor == LinkFlavor::Elf ? "ld.lld" :
                    flavor == LinkFlavor::MachO ? "ld64.lld" :
                    flavor == LinkFlavor::Coff ? "lld-link" : "wasm-ld";
      }
    }
  }

  const std::string bundle = resolveTool(requested);
  if (!bundle.empty()) {
    if (selectedTarget && !isCompilerCommand(requested)) {
      std::cerr << "LLVM: -compile-target applies to clang, flang, and "
                   "llc compiler commands\n";
      return 2;
    }
    if (selectedTarget && isFlangDriver(requested) &&
        !flangSupportsTarget(compileTarget)) {
      std::cerr << "LLVM: Flang 23 has no lowering for target architecture '"
                << compileTarget.Arch << "'\n";
      return 2;
    }
    if (selectedTarget && compileTarget.Ghidra &&
        !compileTarget.DriverOptions.empty() &&
        (!isClangDriver(requested) || requested == "clang-cl")) {
      std::cerr << "LLVM: Ghidra processor mode '"
                << compileTarget.Requested
                << "' currently requires the Clang cc/c++/cpp driver\n";
      return 2;
    }
    if ((isClangDriver(requested) || isFlangDriver(requested) || requested == "clangd") &&
        (!selectedTarget || compileTarget.Os == "macos"))
      configureAppleSdk();
    if (selectedTarget &&
        (hasArgument(argc, argv, firstArgument, "-target") ||
         hasArgumentPrefix(argc, argv, firstArgument, "--target=") ||
         hasArgumentPrefix(argc, argv, firstArgument, "-target=") ||
         (requested == "llc" &&
          hasArgumentPrefix(argc, argv, firstArgument, "-mtriple")))) {
      std::cerr << "LLVM: use either -compile-target or Clang's "
                   "-target/--target, not both\n";
      return 2;
    }
    if ((requested == "clang" || requested == "clang++") &&
        (hasArgument(argc, argv, firstArgument, "-incremental") ||
         hasArgument(argc, argv, firstArgument, "--incremental")))
      return runIncrementalClang(argc, argv, firstArgument, requested,
          selectedTarget ? compileTarget.Requested : "", MainExecutablePath,
          invokeBuildCompiler);
    return runBundle(bundle, requested, argc, argv, firstArgument, {},
                     selectedTarget ? &compileTarget : nullptr);
  }
  if (selectedTarget) {
    std::cerr << "LLVM: -compile-target applies to clang, flang, and "
                 "llc compiler commands\n";
    return 2;
  }
  if (hasScript(requested))
    return runScript(requested, argc, argv, firstArgument);
  {
    std::cerr << "LLVM: embedded tool '" << requested << "' was not found\n";
    std::size_t shown = 0;
    for (const std::string &name : toolNames()) {
      if ((name.find(requested) != std::string::npos ||
           requested.find(name) != std::string::npos) && shown++ < 6)
        std::cerr << "  " << name << '\n';
    }
    std::cerr << "Try 'llvm list " << requested << "' or 'llvm --help'.\n";
    return 1;
  }
}
