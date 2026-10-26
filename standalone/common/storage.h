// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#ifndef BLINK_STANDALONE_COMMON_STORAGE_H_
#define BLINK_STANDALONE_COMMON_STORAGE_H_

#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <span>
#include <utility>
#include <vector>

#include "standalone/include/blink_storage.h"

namespace blink_standalone {

inline constexpr size_t kStorageAreaQuota = 10 * 1024 * 1024;
inline constexpr size_t kStorageSnapshotLimit = 64 * 1024 * 1024;
// At most one empty key plus one entry per minimum two-byte nonempty key.
inline constexpr size_t kStorageEntryLimit = kStorageAreaQuota / 2 + 1;
using StorageBytes = std::vector<uint8_t>;
using StorageEntries = std::map<StorageBytes, StorageBytes>;

inline be_status ValidateStorageClient(const be_storage_client* client) {
  if (!client)
    return BE_OK;
  if (client->struct_size < sizeof(be_storage_client))
    return BE_INVALID_ARGUMENT;
  if (client->abi_version != BE_ABI_VERSION)
    return BE_ABI_MISMATCH;
  if (client->mode > BE_STORAGE_DISABLED || client->reserved)
    return BE_INVALID_ARGUMENT;
  if (client->mode == BE_STORAGE_HOST_PERSISTENT)
    return client->load && client->save ? BE_OK : BE_INVALID_ARGUMENT;
  return client->load || client->save ? BE_INVALID_ARGUMENT : BE_OK;
}

inline std::optional<size_t> StorageUtf8Units(std::span<const uint8_t> bytes) {
  size_t units = 0;
  for (size_t i = 0; i < bytes.size();) {
    const uint8_t lead = bytes[i++];
    uint32_t scalar = lead;
    uint32_t minimum = 0;
    size_t extra = 0;
    if (lead <= 0x7f) {
      ++units;
      continue;
    }
    if (lead >= 0xc2 && lead <= 0xdf) {
      scalar &= 0x1f;
      minimum = 0x80;
      extra = 1;
    } else if (lead >= 0xe0 && lead <= 0xef) {
      scalar &= 0x0f;
      minimum = 0x800;
      extra = 2;
    } else if (lead >= 0xf0 && lead <= 0xf4) {
      scalar &= 0x07;
      minimum = 0x10000;
      extra = 3;
    } else {
      return std::nullopt;
    }
    if (extra > bytes.size() - i)
      return std::nullopt;
    while (extra--) {
      const uint8_t next = bytes[i++];
      if ((next & 0xc0) != 0x80)
        return std::nullopt;
      scalar = (scalar << 6) | (next & 0x3f);
    }
    if (scalar < minimum || scalar > 0x10ffff ||
        (scalar >= 0xd800 && scalar <= 0xdfff))
      return std::nullopt;
    units += scalar > 0xffff ? 2 : 1;
  }
  return units;
}

// Mirror the pinned CachedStorageArea wire format and StorageAreaMap's UTF-16
// quota accounting. Bytes remain opaque to the backend; lone UTF-16 surrogates
// must survive localStorage snapshots unchanged.
inline std::optional<size_t> StorageStringQuota(std::span<const uint8_t> bytes,
                                                bool session,
                                                bool key) {
  if (session) {
    if (!key)
      return bytes.size() % 2 ? std::nullopt
                              : std::optional<size_t>(bytes.size());
    auto units = StorageUtf8Units(bytes);
    return units ? std::optional<size_t>(*units * 2) : std::nullopt;
  }
  if (bytes.empty())
    return std::nullopt;
  if (bytes.front() == 1)
    return (bytes.size() - 1) * 2;
  if (bytes.front() == 0 && (bytes.size() - 1) % 2 == 0) {
    // Blink always writes all-Latin1 strings in format 1. Reject alternative
    // encodings of the same logical key in a host snapshot.
    bool non_latin1 = false;
    for (size_t i = 1; i < bytes.size(); i += 2) {
      const auto unit =
          std::bit_cast<uint16_t>(std::array<uint8_t, 2>{bytes[i], bytes[i + 1]});
      non_latin1 |= unit > 255;
    }
    return non_latin1 ? std::optional<size_t>(bytes.size() - 1) : std::nullopt;
  }
  return std::nullopt;
}

class StorageData {
 public:
  explicit StorageData(bool session = false);
  StorageData(const StorageData&);
  StorageData(StorageData&&) noexcept;
  StorageData& operator=(const StorageData&);
  StorageData& operator=(StorageData&&) noexcept;
  ~StorageData();
  const StorageEntries& entries() const { return entries_; }
  size_t quota() const { return quota_; }
  bool empty() const { return entries_.empty(); }
  std::optional<StorageBytes> Get(const StorageBytes& key) const {
    const auto found = entries_.find(key);
    return found == entries_.end() ? std::nullopt
                                   : std::optional<StorageBytes>(found->second);
  }
  bool Put(const StorageBytes& key, const StorageBytes& value) {
    if (key.size() > kStorageAreaQuota + 1 ||
        value.size() > kStorageAreaQuota + 1)
      return false;
    const auto key_quota = StorageStringQuota(key, session_, true);
    const auto value_quota = StorageStringQuota(value, session_, false);
    if (!key_quota || !value_quota || *key_quota > kStorageAreaQuota ||
        *value_quota > kStorageAreaQuota - *key_quota)
      return false;
    const auto found = entries_.find(key);
    const size_t before =
        found == entries_.end()
            ? 0
            : *key_quota + *StorageStringQuota(found->second, session_, false);
    const size_t after = *key_quota + *value_quota;
    if ((found == entries_.end() && entries_.size() >= kStorageEntryLimit) ||
        (after > before && after - before > kStorageAreaQuota - quota_))
      return false;
    entries_[key] = value;
    quota_ = quota_ - before + after;
    return true;
  }
  void Delete(const StorageBytes& key) {
    const auto found = entries_.find(key);
    if (found == entries_.end())
      return;
    quota_ -= *StorageStringQuota(key, session_, true) +
              *StorageStringQuota(found->second, session_, false);
    entries_.erase(found);
  }
  void Clear() {
    entries_.clear();
    quota_ = 0;
  }

 private:
  bool session_;
  size_t quota_ = 0;
  StorageEntries entries_;
};

inline StorageData::StorageData(bool session) : session_(session) {}
inline StorageData::StorageData(const StorageData&) = default;
inline StorageData::StorageData(StorageData&&) noexcept = default;
inline StorageData& StorageData::operator=(const StorageData&) = default;
inline StorageData& StorageData::operator=(StorageData&&) noexcept = default;
inline StorageData::~StorageData() = default;

inline constexpr std::array<uint8_t, 8> kStorageSnapshotMagic = {
    'B',
    'E',
    'S',
    'T',
    'O',
    'R',
    '1',
    std::endian::native == std::endian::little ? uint8_t('L') : uint8_t('B')};

inline void StorageAppend32(StorageBytes& output, size_t value) {
  for (unsigned shift = 0; shift < 32; shift += 8)
    output.push_back(static_cast<uint8_t>((value >> shift) & 255));
}
inline bool StorageRead32(std::span<const uint8_t> input,
                          size_t& offset,
                          size_t& value) {
  if (offset > input.size() || input.size() - offset < 4)
    return false;
  value = 0;
  for (unsigned shift = 0; shift < 32; shift += 8)
    value |= size_t(input[offset++]) << shift;
  return true;
}
inline std::optional<StorageBytes> EncodeStorageSnapshot(
    const StorageData& data) {
  StorageBytes output(kStorageSnapshotMagic.begin(),
                      kStorageSnapshotMagic.end());
  StorageAppend32(output, data.entries().size());
  for (const auto& [key, value] : data.entries()) {
    if (key.size() > kStorageSnapshotLimit - output.size() ||
        value.size() > kStorageSnapshotLimit - output.size() - key.size() ||
        8 > kStorageSnapshotLimit - output.size() - key.size() - value.size())
      return std::nullopt;
    StorageAppend32(output, key.size());
    StorageAppend32(output, value.size());
    output.insert(output.end(), key.begin(), key.end());
    output.insert(output.end(), value.begin(), value.end());
  }
  return output;
}
inline bool DecodeStorageSnapshot(std::span<const uint8_t> input,
                                  StorageData& destination) {
  StorageData candidate;
  if (input.empty()) {
    destination = std::move(candidate);
    return true;
  }
  if (input.size() > kStorageSnapshotLimit || input.size() < 12 ||
      !std::equal(kStorageSnapshotMagic.begin(), kStorageSnapshotMagic.end(),
                  input.begin()))
    return false;
  size_t offset = 8;
  size_t count = 0;
  if (!StorageRead32(input, offset, count) || count > kStorageEntryLimit ||
      count > (input.size() - offset) / 8)
    return false;
  for (size_t i = 0; i < count; ++i) {
    size_t key_size = 0, value_size = 0;
    if (!StorageRead32(input, offset, key_size) ||
        !StorageRead32(input, offset, value_size) ||
        key_size > input.size() - offset ||
        value_size > input.size() - offset - key_size)
      return false;
    auto key_bytes = input.subspan(offset, key_size);
    StorageBytes key(key_bytes.begin(), key_bytes.end());
    offset += key_size;
    auto value_bytes = input.subspan(offset, value_size);
    StorageBytes value(value_bytes.begin(), value_bytes.end());
    offset += value_size;
    if (candidate.Get(key).has_value() || !candidate.Put(key, value))
      return false;
  }
  if (offset != input.size())
    return false;
  destination = std::move(candidate);
  return true;
}

}  // namespace blink_standalone
#endif
