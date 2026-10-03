#include "pack-format.h"

#include <zstd.h>

#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <unordered_map>
#include <vector>

#include <sys/stat.h>

namespace fs = std::filesystem;

namespace {

std::uint64_t hashBytes(std::uint64_t hash, const void *data, std::size_t size) {
  const auto *bytes = static_cast<const unsigned char *>(data);
  for (std::size_t index = 0; index < size; ++index) {
    hash ^= bytes[index];
    hash *= 1099511628211ULL;
  }
  return hash;
}

template <typename T>
std::uint64_t hashValue(std::uint64_t hash, const T &value) {
  return hashBytes(hash, &value, sizeof(value));
}

bool writeAll(std::ofstream &output, const void *data, std::size_t size) {
  output.write(static_cast<const char *>(data), static_cast<std::streamsize>(size));
  return output.good();
}

bool sameFileContents(const fs::path &path, const std::vector<char> &bytes) {
  std::ifstream candidate(path, std::ios::binary);
  if (!candidate)
    return false;
  std::vector<char> buffer(64 * 1024);
  for (std::size_t offset = 0; offset < bytes.size();) {
    const std::size_t amount = std::min(buffer.size(), bytes.size() - offset);
    candidate.read(buffer.data(), static_cast<std::streamsize>(amount));
    if (candidate.gcount() != static_cast<std::streamsize>(amount) ||
        std::memcmp(buffer.data(), bytes.data() + offset, amount) != 0)
      return false;
    offset += amount;
  }
  return candidate.peek() == std::char_traits<char>::eof();
}

struct PendingEntry {
  fs::path AbsolutePath;
  std::string RelativePath;
  llvm_pack::EntryType Type;
  std::uint32_t Mode;
};

} // namespace

int main(int argc, char **argv) {
  if (argc != 3) {
    std::cerr << "usage: llvm-pack <root> <output>\n";
    return 2;
  }

  const fs::path root = fs::canonical(argv[1]);
  std::vector<PendingEntry> entries;

  std::error_code error;
  for (fs::recursive_directory_iterator iterator{
           root, fs::directory_options::skip_permission_denied, error},
       end;
       iterator != end; iterator.increment(error)) {
    if (error) {
      std::cerr << "llvm-pack: traversal failed: " << error.message() << '\n';
      return 1;
    }

    const fs::file_status status = iterator->symlink_status(error);
    if (error) {
      std::cerr << "llvm-pack: stat failed: " << error.message() << '\n';
      return 1;
    }

    llvm_pack::EntryType type;
    if (fs::is_symlink(status))
      type = llvm_pack::EntryType::Symlink;
    else if (fs::is_directory(status))
      type = llvm_pack::EntryType::Directory;
    else if (fs::is_regular_file(status))
      type = llvm_pack::EntryType::File;
    else
      continue;

    std::string relative =
        iterator->path().lexically_relative(root).generic_string();
    if (relative.empty() || relative.size() > UINT16_MAX) {
      std::cerr << "llvm-pack: invalid path: " << relative << '\n';
      return 1;
    }

    struct stat info {};
    if (::lstat(iterator->path().c_str(), &info) != 0) {
      std::cerr << "llvm-pack: lstat failed for " << relative << ": "
                << std::strerror(errno) << '\n';
      return 1;
    }

    entries.push_back({iterator->path(), std::move(relative), type,
                       static_cast<std::uint32_t>(info.st_mode & 07777)});
  }

  std::ranges::sort(entries, {}, &PendingEntry::RelativePath);

  std::ofstream output(argv[2], std::ios::binary | std::ios::trunc);
  if (!output) {
    std::cerr << "llvm-pack: cannot create " << argv[2] << '\n';
    return 1;
  }

  llvm_pack::Header header{};
  std::memcpy(header.MagicBytes, llvm_pack::Magic, sizeof(header.MagicBytes));
  header.VersionNumber = llvm_pack::Version;
  header.EntryCount = static_cast<std::uint32_t>(entries.size());
  const std::uint64_t tableSize = sizeof(header) +
      entries.size() * sizeof(llvm_pack::EntryHeader) +
      std::ranges::fold_left(entries, std::uint64_t{0},
          [](std::uint64_t total, const PendingEntry &entry) {
            return total + entry.RelativePath.size();
          });
  output.seekp(static_cast<std::streamoff>(tableSize));
  if (!output)
    return 1;

  std::uint64_t contentHash = 1469598103934665603ULL;
  std::vector<char> input;
  std::vector<char> compressed;

  struct TableEntry {
    llvm_pack::EntryHeader Header;
    std::string Path;
  };
  std::vector<TableEntry> table;
  table.reserve(entries.size());
  struct SharedData {
    fs::path Path;
    std::uint64_t OriginalSize;
    std::uint64_t StoredSize;
    std::uint64_t DataOffset;
  };
  std::unordered_map<std::uint64_t, std::vector<SharedData>> sharedData;
  std::uint64_t reusedBytes = 0;
  std::size_t reusedFiles = 0;

  for (const PendingEntry &entry : entries) {
    input.clear();

    if (entry.Type == llvm_pack::EntryType::File) {
      std::ifstream file(entry.AbsolutePath, std::ios::binary | std::ios::ate);
      if (!file) {
        std::cerr << "llvm-pack: cannot read " << entry.RelativePath << '\n';
        return 1;
      }
      const auto size = file.tellg();
      if (size < 0)
        return 1;
      input.resize(static_cast<std::size_t>(size));
      file.seekg(0);
      file.read(input.data(), size);
      if (!file)
        return 1;
    } else if (entry.Type == llvm_pack::EntryType::Symlink) {
      const std::string target = fs::read_symlink(entry.AbsolutePath, error).generic_string();
      if (error) {
        std::cerr << "llvm-pack: readlink failed for " << entry.RelativePath
                  << ": " << error.message() << '\n';
        return 1;
      }
      input.assign(target.begin(), target.end());
    }

    std::uint64_t dataKey = 0;
    const SharedData *reuse = nullptr;
    if (entry.Type == llvm_pack::EntryType::File && !input.empty()) {
      dataKey = hashValue(hashBytes(14695981039346656037ULL, input.data(),
                                    input.size()), input.size());
      if (const auto found = sharedData.find(dataKey);
          found != sharedData.end()) {
        for (const SharedData &candidate : found->second) {
          if (candidate.OriginalSize == input.size() &&
              sameFileContents(candidate.Path, input)) {
            reuse = &candidate;
            break;
          }
        }
      }
    }

    compressed.clear();
    if (!input.empty() && !reuse) {
      compressed.resize(ZSTD_compressBound(input.size()));
      const std::size_t result = ZSTD_compress(
          compressed.data(), compressed.size(), input.data(), input.size(), 9);
      if (ZSTD_isError(result)) {
        std::cerr << "llvm-pack: compression failed for " << entry.RelativePath
                  << ": " << ZSTD_getErrorName(result) << '\n';
        return 1;
      }
      compressed.resize(result);
    }

    llvm_pack::EntryHeader entryHeader{
        static_cast<std::uint16_t>(entry.RelativePath.size()), entry.Type, 0,
        entry.Mode, static_cast<std::uint64_t>(input.size()),
        reuse ? reuse->StoredSize : static_cast<std::uint64_t>(compressed.size()),
        reuse ? reuse->DataOffset : static_cast<std::uint64_t>(output.tellp())};

    contentHash = hashBytes(contentHash, entry.RelativePath.data(),
                            entry.RelativePath.size());
    contentHash = hashValue(contentHash, entryHeader.Type);
    contentHash = hashValue(contentHash, entryHeader.Mode);
    contentHash = hashValue(contentHash, entryHeader.OriginalSize);
    if (!input.empty())
      contentHash = hashBytes(contentHash, input.data(), input.size());

    table.push_back({entryHeader, entry.RelativePath});
    if (!compressed.empty() &&
        !writeAll(output, compressed.data(), compressed.size())) {
      std::cerr << "llvm-pack: write failed\n";
      return 1;
    }
    if (reuse) {
      ++reusedFiles;
      reusedBytes += reuse->StoredSize;
    } else if (entry.Type == llvm_pack::EntryType::File && !input.empty()) {
      sharedData[dataKey].push_back({entry.AbsolutePath,
                                     entryHeader.OriginalSize,
                                     entryHeader.StoredSize,
                                     entryHeader.DataOffset});
    }
  }

  header.ContentHash = contentHash;
  output.seekp(0);
  if (!writeAll(output, &header, sizeof(header)))
    return 1;
  for (const TableEntry &entry : table)
    if (!writeAll(output, &entry.Header, sizeof(entry.Header)) ||
        !writeAll(output, entry.Path.data(), entry.Path.size()))
      return 1;

  std::cout << entries.size() << " entries, content hash " << std::hex
            << contentHash << std::dec << ", reused " << reusedFiles
            << " files (" << reusedBytes << " packed bytes)\n";
  return 0;
}
