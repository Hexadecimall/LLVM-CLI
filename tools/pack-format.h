#pragma once

#include <cstdint>

namespace llvm_pack {

inline constexpr char Magic[8] = {'L', 'L', 'V', 'M', 'P', 'K', '0', '2'};
inline constexpr std::uint32_t Version = 2;

enum class EntryType : std::uint8_t {
  Directory = 1,
  File = 2,
  Symlink = 3,
};

#pragma pack(push, 1)
struct Header {
  char MagicBytes[8];
  std::uint32_t VersionNumber;
  std::uint32_t EntryCount;
  std::uint64_t ContentHash;
};

struct EntryHeader {
  std::uint16_t PathSize;
  EntryType Type;
  std::uint8_t Reserved;
  std::uint32_t Mode;
  std::uint64_t OriginalSize;
  std::uint64_t StoredSize;
  std::uint64_t DataOffset;
};
#pragma pack(pop)

static_assert(sizeof(Header) == 24);
static_assert(sizeof(EntryHeader) == 32);

} // namespace llvm_pack
