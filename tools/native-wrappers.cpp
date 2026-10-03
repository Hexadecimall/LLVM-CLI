#include "native-wrappers.h"

#include <bzlib.h>

#include <algorithm>
#include <cerrno>
#include <cctype>
#include <charconv>
#include <climits>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <filesystem>
#include <iostream>
#include <iterator>
#include <regex>
#include <span>
#include <string>
#include <string_view>
#include <vector>
#include <unistd.h>

namespace {

std::vector<char> readStream(std::istream &input) {
  return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

bool readFile(const std::string &path, std::vector<char> &bytes) {
  if (path == "-") {
    bytes = readStream(std::cin);
    return !std::cin.bad();
  }
  std::ifstream input(path, std::ios::binary);
  if (!input)
    return false;
  bytes = readStream(input);
  return !input.bad();
}

bool decompressBzip(std::span<const char> input, std::vector<char> &output) {
  if (input.size() < 3 || std::string_view(input.data(), 3) != "BZh") {
    output.assign(input.begin(), input.end());
    return true;
  }
  unsigned int capacity = static_cast<unsigned int>(
      std::min<std::size_t>(std::max<std::size_t>(input.size() * 5, 65536),
                            64U * 1024U * 1024U));
  for (;;) {
    output.resize(capacity);
    unsigned int size = capacity;
    const int result = BZ2_bzBuffToBuffDecompress(
        output.data(), &size, const_cast<char *>(input.data()),
        static_cast<unsigned int>(input.size()), 0, 0);
    if (result == BZ_OK) {
      output.resize(size);
      return true;
    }
    if (result != BZ_OUTBUFF_FULL || capacity > UINT_MAX / 2)
      return false;
    capacity *= 2;
  }
}

bool loadExpanded(std::string path, std::vector<char> &output,
                  bool trySuffix = false) {
  std::vector<char> input;
  if (!readFile(path, input) && trySuffix) {
    path += ".bz2";
    if (!readFile(path, input))
      return false;
  } else if (input.empty() && path != "-") {
    std::ifstream probe(path, std::ios::binary);
    if (!probe)
      return false;
  }
  return decompressBzip(input, output);
}

int writeExpanded(const std::string &name, int argc, char **argv,
                  int firstArgument) {
  if (firstArgument == argc) {
    if (::isatty(STDIN_FILENO)) {
      std::cerr << "usage: " << name << " files...\n";
      return 1;
    }
    std::vector<char> output;
    if (!loadExpanded("-", output))
      return 2;
    std::cout.write(output.data(), static_cast<std::streamsize>(output.size()));
    return 0;
  }
  for (int index = firstArgument; index < argc; ++index) {
    std::vector<char> output;
    if (!loadExpanded(argv[index], output)) {
      std::cerr << name << ": " << argv[index] << ": "
                << std::strerror(errno ? errno : EINVAL) << '\n';
      return 2;
    }
    if (argc - firstArgument > 1)
      std::cout << "------> " << argv[index] << " <------\n";
    std::cout.write(output.data(), static_cast<std::streamsize>(output.size()));
  }
  return 0;
}

struct GrepOptions {
  bool IgnoreCase = false;
  bool Invert = false;
  bool Number = false;
  bool Count = false;
  bool List = false;
  bool ListWithout = false;
  bool Quiet = false;
  bool NoFilename = false;
  bool WithFilename = false;
  bool Fixed = false;
  std::string Pattern;
  std::vector<std::string> Files;
};

bool parseGrepOptions(const std::string &name, int argc, char **argv,
                      int firstArgument, GrepOptions &options) {
  options.Fixed = name == "bzfgrep";
  for (int index = firstArgument; index < argc; ++index) {
    std::string_view argument(argv[index]);
    if (argument == "--") {
      for (++index; index < argc; ++index) {
        if (options.Pattern.empty())
          options.Pattern = argv[index];
        else
          options.Files.emplace_back(argv[index]);
      }
      break;
    }
    if (argument == "-e" && index + 1 < argc) {
      options.Pattern = argv[++index];
      continue;
    }
    if (argument.starts_with("-") && argument.size() > 1 &&
        options.Pattern.empty()) {
      for (char flag : argument.substr(1)) {
        if (flag == 'i') options.IgnoreCase = true;
        else if (flag == 'v') options.Invert = true;
        else if (flag == 'n') options.Number = true;
        else if (flag == 'c') options.Count = true;
        else if (flag == 'l') options.List = true;
        else if (flag == 'L') options.ListWithout = true;
        else if (flag == 'q') options.Quiet = true;
        else if (flag == 'h') options.NoFilename = true;
        else if (flag == 'H') options.WithFilename = true;
        else if (flag == 'F') options.Fixed = true;
        else if (flag == 'E') options.Fixed = false;
        else {
          std::cerr << name << ": unsupported option -- " << flag << '\n';
          return false;
        }
      }
      continue;
    }
    if (options.Pattern.empty())
      options.Pattern = argument;
    else
      options.Files.emplace_back(argument);
  }
  if (options.Pattern.empty()) {
    std::cerr << "usage: " << name << " [grep_options] pattern [files]\n";
    return false;
  }
  if (options.Files.empty())
    options.Files.emplace_back("-");
  return true;
}

bool lineMatches(std::string_view line, const GrepOptions &options,
                 const std::regex *expression) {
  bool matched = false;
  if (options.Fixed) {
    if (options.IgnoreCase) {
      std::string value(line);
      std::string pattern(options.Pattern);
      std::ranges::transform(value, value.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
      });
      std::ranges::transform(pattern, pattern.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
      });
      matched = value.find(pattern) != std::string::npos;
    } else {
      matched = line.find(options.Pattern) != std::string_view::npos;
    }
  } else {
    matched = std::regex_search(line.begin(), line.end(), *expression);
  }
  return options.Invert ? !matched : matched;
}

int runBzgrep(const std::string &name, int argc, char **argv,
              int firstArgument) {
  GrepOptions options;
  if (!parseGrepOptions(name, argc, argv, firstArgument, options))
    return 2;
  std::regex expression;
  if (!options.Fixed) {
    try {
      auto flags = std::regex::extended;
      if (options.IgnoreCase)
        flags |= std::regex::icase;
      expression = std::regex(options.Pattern, flags);
    } catch (const std::regex_error &error) {
      std::cerr << name << ": " << error.what() << '\n';
      return 2;
    }
  }
  bool anyMatch = false;
  const bool prefix = options.WithFilename ||
      (options.Files.size() > 1 && !options.NoFilename);
  for (const std::string &file : options.Files) {
    std::vector<char> bytes;
    if (!loadExpanded(file, bytes, true)) {
      std::cerr << name << ": " << file << ": cannot read file\n";
      return 2;
    }
    std::string_view contents(bytes.data(), bytes.size());
    std::size_t position = 0;
    std::size_t lineNumber = 1;
    std::size_t matches = 0;
    while (position < contents.size()) {
      const std::size_t end = contents.find('\n', position);
      const std::size_t length = (end == std::string_view::npos
                                      ? contents.size() : end) - position;
      const std::string_view line = contents.substr(position, length);
      if (lineMatches(line, options, options.Fixed ? nullptr : &expression)) {
        anyMatch = true;
        ++matches;
        if (options.Quiet)
          return 0;
        if (!options.Count && !options.List && !options.ListWithout) {
          if (prefix) std::cout << file << ':';
          if (options.Number) std::cout << lineNumber << ':';
          std::cout.write(line.data(), static_cast<std::streamsize>(line.size()));
          std::cout << '\n';
        }
      }
      if (end == std::string_view::npos)
        break;
      position = end + 1;
      ++lineNumber;
    }
    if (options.Count) {
      if (prefix) std::cout << file << ':';
      std::cout << matches << '\n';
    } else if ((options.List && matches) || (options.ListWithout && !matches)) {
      std::cout << file << '\n';
    }
  }
  return anyMatch ? 0 : 1;
}

int runBzdiff(const std::string &name, int argc, char **argv,
              int firstArgument) {
  bool quiet = false;
  std::vector<std::string> files;
  for (int index = firstArgument; index < argc; ++index) {
    const std::string_view argument(argv[index]);
    if (argument == "-q" || argument == "-s")
      quiet = true;
    else if (!argument.starts_with("-"))
      files.emplace_back(argument);
  }
  if (files.empty() || files.size() > 2) {
    std::cerr << "usage: " << name << " [diff_options] file [file]\n";
    return 2;
  }
  if (files.size() == 1) {
    if (!files[0].ends_with(".bz2")) {
      std::cerr << name << ": one-file form requires a .bz2 file\n";
      return 2;
    }
    files.push_back(files[0].substr(0, files[0].size() - 4));
  }
  std::vector<char> left;
  std::vector<char> right;
  if (!loadExpanded(files[0], left) || !loadExpanded(files[1], right)) {
    std::cerr << name << ": cannot read input\n";
    return 2;
  }
  if (left == right)
    return 0;
  if (name == "bzcmp" || quiet) {
    const auto mismatch =
        std::mismatch(left.begin(), left.end(), right.begin(), right.end());
    const std::size_t offset = static_cast<std::size_t>(mismatch.first - left.begin());
    std::cout << files[0] << ' ' << files[1] << " differ: byte "
              << offset + 1 << '\n';
    return 1;
  }
  std::cout << "--- " << files[0] << "\n+++ " << files[1]
            << "\n@@ -1 +1 @@\n-";
  std::cout.write(left.data(), static_cast<std::streamsize>(left.size()));
  if (left.empty() || left.back() != '\n') std::cout << '\n';
  std::cout << '+';
  std::cout.write(right.data(), static_cast<std::streamsize>(right.size()));
  if (right.empty() || right.back() != '\n') std::cout << '\n';
  return 1;
}

void printCurlHelp() {
  std::cout << "Usage: curl-config [OPTION]\n"
               "  --built-shared --ca --cc --cflags --checkfor VERSION\n"
               "  --features --libs --prefix --protocols --ssl-backends\n"
               "  --static-libs --version --vernum --help\n";
}

int runCurlConfig(int argc, char **argv, int firstArgument) {
  if (firstArgument == argc) {
    printCurlHelp();
    return 1;
  }
  for (int index = firstArgument; index < argc; ++index) {
    const std::string_view option(argv[index]);
    if (option == "--built-shared") std::cout << "no\n";
    else if (option == "--ca") std::cout << "/etc/ssl/cert.pem\n";
    else if (option == "--cc") std::cout << "clang\n";
    else if (option == "--prefix") std::cout << "/__llvm/deps\n";
    else if (option == "--version") std::cout << "libcurl 8.22.0\n";
    else if (option == "--vernum") std::cout << "081600\n";
    else if (option == "--ssl-backends") std::cout << "OpenSSL\n";
    else if (option == "--cflags")
      std::cout << "-DCURL_STATICLIB -I/__llvm/deps/include\n";
    else if (option == "--libs")
      std::cout << "-L/__llvm/deps/lib -lcurl -lldap -lssl -lcrypto -lzstd "
                   "-lbrotlidec -lbrotlicommon -lz\n";
    else if (option == "--static-libs")
      std::cout << "/__llvm/deps/lib/libcurl.a -L/__llvm/deps/lib -lldap "
                   "-lssl -lcrypto -lzstd -lbrotlidec -lbrotlicommon -lz\n";
    else if (option == "--configure")
      std::cout << "--prefix=/__llvm/deps --disable-shared --enable-static "
                   "--with-openssl --with-zlib --with-brotli --with-zstd\n";
    else if (option == "--features" || option == "--feature")
      std::cout << "alt-svc\nAsynchDNS\nbrotli\nHSTS\nHTTPS-proxy\nIPv6\n"
                   "Largefile\nlibz\nSSL\nthreadsafe\nUnixSockets\nzstd\n";
    else if (option == "--protocols")
      std::cout << "DICT\nFILE\nFTP\nFTPS\nGOPHER\nGOPHERS\nHTTP\nHTTPS\n"
                   "IMAP\nIMAPS\nIPFS\nIPNS\nLDAP\nLDAPS\nMQTT\nMQTTS\n"
                   "POP3\nPOP3S\nRTSP\nSMTP\nSMTPS\nTELNET\nTFTP\nWS\nWSS\n";
    else if (option == "--checkfor" && index + 1 < argc) {
      unsigned requested[3]{};
      std::string_view version(argv[++index]);
      for (unsigned part = 0; part != 3 && !version.empty(); ++part) {
        const std::size_t dot = version.find('.');
        const std::string_view value = version.substr(0, dot);
        std::from_chars(value.data(), value.data() + value.size(), requested[part]);
        version = dot == std::string_view::npos ? std::string_view{} : version.substr(dot + 1);
      }
      if (requested[0] > 8 || (requested[0] == 8 && requested[1] > 22) ||
          (requested[0] == 8 && requested[1] == 22 && requested[2] > 0)) {
        std::cerr << "requested version is newer than existing 8.22.0\n";
        return 1;
      }
    } else if (option == "--help") {
      printCurlHelp();
    } else {
      std::cerr << "curl-config: unknown option: " << option << '\n';
      return 1;
    }
  }
  return 0;
}

int runXmlConfig(int argc, char **argv, int firstArgument) {
  std::string prefix = "/__llvm/deps";
  std::string execPrefix = prefix;
  std::string cflags;
  std::string libs;
  if (firstArgument == argc) {
    std::cerr << "Usage: xml2-config [OPTION]\n";
    return 1;
  }
  for (int index = firstArgument; index < argc; ++index) {
    std::string_view option(argv[index]);
    if (option.starts_with("--prefix=")) prefix = option.substr(9);
    else if (option == "--prefix") std::cout << prefix << '\n';
    else if (option.starts_with("--exec-prefix=")) execPrefix = option.substr(14);
    else if (option == "--exec-prefix") std::cout << execPrefix << '\n';
    else if (option == "--version") std::cout << "2.15.4\n";
    else if (option == "--modules") std::cout << "1\n";
    else if (option == "--cflags") cflags = "-I" + prefix + "/include/libxml2";
    else if (option == "--libtool-libs")
      std::cout << execPrefix << "/lib/libxml2.la\n";
    else if (option == "--libs") {
      if (index + 1 < argc && std::string_view(argv[index + 1]) == "--dynamic")
        ++index;
      libs = "-L" + execPrefix + "/lib -lxml2 -lz -liconv";
    } else if (option == "--help") {
      std::cout << "Usage: xml2-config [--prefix] [--exec-prefix] [--libs] "
                   "[--cflags] [--modules] [--version]\n";
    } else {
      std::cerr << "xml2-config: unknown option: " << option << '\n';
      return 1;
    }
  }
  if (!cflags.empty() || !libs.empty())
    std::cout << cflags << (cflags.empty() || libs.empty() ? "" : " ")
              << libs << '\n';
  return 0;
}

int runSetXcodeAnalyzer(int argc, char **argv, int firstArgument) {
  std::string checker;
  bool useXcodeClang = false;
  for (int index = firstArgument; index < argc; ++index) {
    const std::string_view argument(argv[index]);
    if (argument == "-h" || argument == "--help") {
      std::cout << "usage: set-xcode-analyzer [--use-xcode-clang | "
                   "--use-checker-build PATH]\n";
      return 0;
    }
    if (argument == "--use-xcode-clang")
      useXcodeClang = true;
    else if (argument == "--use-checker-build" && index + 1 < argc)
      checker = argv[++index];
    else {
      std::cerr << "set-xcode-analyzer: unknown or incomplete option: "
                << argument << '\n';
      return 2;
    }
  }
  if (!useXcodeClang && checker.empty()) {
    std::cerr << "set-xcode-analyzer: select --use-xcode-clang or "
                 "--use-checker-build PATH\n";
    return 2;
  }
  if (!checker.empty() && !checker.ends_with("/clang"))
    checker = (std::filesystem::path(checker) / "bin" / "clang").string();
  if (useXcodeClang)
    checker = "$(CLANG)";

  std::vector<std::filesystem::path> candidates;
  if (const char *developer = std::getenv("DEVELOPER_DIR");
      developer && *developer)
    candidates.emplace_back(developer);
  candidates.emplace_back("/Applications/Xcode-beta.app/Contents/Developer");
  candidates.emplace_back("/Applications/Xcode.app/Contents/Developer");
  std::filesystem::path developer;
  for (const auto &candidate : candidates)
    if (std::filesystem::is_directory(candidate)) {
      developer = candidate;
      break;
    }
  if (developer.empty()) {
    std::cerr << "set-xcode-analyzer: Xcode developer directory not found\n";
    return 1;
  }
  const std::filesystem::path root = developer.parent_path();
  std::size_t changed = 0;
  std::error_code error;
  for (std::filesystem::recursive_directory_iterator iterator(
           root, std::filesystem::directory_options::skip_permission_denied,
           error), end;
       iterator != end; iterator.increment(error)) {
    if (error) {
      error.clear();
      continue;
    }
    if (!iterator->is_regular_file(error))
      continue;
    const std::string filename = iterator->path().filename().string();
    if (!filename.starts_with("Clang LLVM") || !filename.ends_with(".xcspec"))
      continue;
    std::ifstream input(iterator->path(), std::ios::binary);
    std::string contents((std::istreambuf_iterator<char>(input)), {});
    const std::size_t analyzer = contents.find("Static Analyzer");
    if (analyzer == std::string::npos)
      continue;
    std::string replacement = checker;
    if (useXcodeClang && contents.find("CLANG_ANALYZER_EXEC") != std::string::npos)
      replacement = "$(CLANG_ANALYZER_EXEC)";
    const std::regex execPath(R"((ExecPath\s*=\s*")[^"]*(";))");
    std::smatch match;
    std::string tail = contents.substr(analyzer);
    if (!std::regex_search(tail, match, execPath))
      continue;
    tail.replace(static_cast<std::size_t>(match.position(0)),
                 static_cast<std::size_t>(match.length(0)),
                 match.str(1) + replacement + match.str(2));
    contents.replace(analyzer, std::string::npos, tail);
    std::ofstream output(iterator->path(), std::ios::binary | std::ios::trunc);
    output.write(contents.data(), static_cast<std::streamsize>(contents.size()));
    if (!output) {
      std::cerr << "set-xcode-analyzer: cannot update " << iterator->path()
                << '\n';
      return 1;
    }
    std::cout << "updated " << iterator->path() << '\n';
    ++changed;
  }
  if (!changed) {
    std::cerr << "set-xcode-analyzer: no Clang LLVM xcspec was found\n";
    return 1;
  }
  return 0;
}

} // namespace

bool isNativeWrapper(const std::string &name) {
  return name == "bzmore" || name == "bzless" || name == "bzgrep" ||
         name == "bzegrep" || name == "bzfgrep" || name == "bzdiff" ||
         name == "bzcmp" || name == "curl-config" || name == "xml2-config" ||
         name == "set-xcode-analyzer";
}

int runNativeWrapper(const std::string &name, int argc, char **argv,
                     int firstArgument) {
  if (name == "bzmore" || name == "bzless")
    return writeExpanded(name, argc, argv, firstArgument);
  if (name == "bzgrep" || name == "bzegrep" || name == "bzfgrep")
    return runBzgrep(name, argc, argv, firstArgument);
  if (name == "bzdiff" || name == "bzcmp")
    return runBzdiff(name, argc, argv, firstArgument);
  if (name == "curl-config")
    return runCurlConfig(argc, argv, firstArgument);
  if (name == "xml2-config")
    return runXmlConfig(argc, argv, firstArgument);
  if (name == "set-xcode-analyzer")
    return runSetXcodeAnalyzer(argc, argv, firstArgument);
  return -1;
}
