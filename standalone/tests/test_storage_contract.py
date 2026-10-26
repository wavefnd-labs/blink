"""Run native dependency-free storage boundary/snapshot programs, not Blink.

The actual DOM local/sessionStorage and Mojo provider require a complete engine
build and separate native smoke assertions. These tests do not supply that
evidence or fabricate an engine receipt.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class StorageContractTests(unittest.TestCase):
    def program(self, body, extra_source=None):
        compiler = shutil.which('c++')
        if compiler is None:
            self.skipTest('C++ compiler unavailable')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'storage.cc'
            executable = Path(directory) / 'storage'
            source.write_text(r'''
#include <cassert>
#include <cstring>
#include <limits>
#include <type_traits>
#include "standalone/common/storage.h"
using namespace blink_standalone;
StorageBytes Latin(const char* value) {
  StorageBytes bytes{1};
  while (*value) bytes.push_back(static_cast<uint8_t>(*value++));
  return bytes;
}
StorageBytes Utf16(std::initializer_list<uint16_t> units, bool prefix = true) {
  StorageBytes bytes(prefix ? 1 : 0, 0);
  for (uint16_t unit : units) {
    uint8_t raw[2];
    std::memcpy(raw, &unit, 2);
    bytes.insert(bytes.end(), raw, raw + 2);
  }
  return bytes;
}
int main() {
''' + body + '\n}\n')
            sources = [str(source)]
            if extra_source is not None:
                other = Path(directory) / 'other.cc'
                other.write_text(extra_source)
                sources.append(str(other))
            built = subprocess.run([compiler, '-std=c++20', '-Wall', '-Wextra', '-Werror',
                                    '-Wconversion', '-Wsign-conversion', '-I', str(ROOT),
                                    *sources, '-o', str(executable)],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            executed = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)

    def test_additive_header_is_valid_c11(self):
        compiler = shutil.which('cc')
        if compiler is None:
            self.skipTest('C compiler unavailable')
        source = '#include "standalone/include/blink_storage.h"\n' \
                 '_Static_assert(BE_ABI_VERSION == 1, "ABI version");\n' \
                 'int main(void) { be_storage_client client = {0}; return client.reserved; }\n'
        checked = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                  '-I', str(ROOT), '-x', 'c', '-fsyntax-only', '-'],
                                 input=source, capture_output=True, text=True, timeout=30)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_client_requires_explicit_persistence_pair_and_valid_abi(self):
        self.program(r'''
  be_storage_client client{};
  assert(ValidateStorageClient(nullptr) == BE_OK);
  assert(ValidateStorageClient(&client) == BE_INVALID_ARGUMENT);
  client.struct_size = sizeof(client);
  assert(ValidateStorageClient(&client) == BE_ABI_MISMATCH);
  client.abi_version = BE_ABI_VERSION;
  assert(ValidateStorageClient(&client) == BE_OK);
  client.mode = BE_STORAGE_HOST_PERSISTENT;
  assert(ValidateStorageClient(&client) == BE_INVALID_ARGUMENT);
  client.load = [](void*, const char*, be_storage_snapshot*) { return BE_OK; };
  assert(ValidateStorageClient(&client) == BE_INVALID_ARGUMENT);
  client.save = [](void*, const char*, const uint8_t*, size_t) { return BE_OK; };
  assert(ValidateStorageClient(&client) == BE_OK);
  client.reserved = 1;
  assert(ValidateStorageClient(&client) == BE_INVALID_ARGUMENT);
  client.reserved = 0;
  client.mode = BE_STORAGE_MEMORY;
  assert(ValidateStorageClient(&client) == BE_INVALID_ARGUMENT);
  client.load = nullptr;
  client.save = nullptr;
  client.mode = BE_STORAGE_DISABLED;
  assert(ValidateStorageClient(&client) == BE_OK);
  client.mode = 3;
  assert(ValidateStorageClient(&client) == BE_INVALID_ARGUMENT);
''')

    def test_quota_counts_utf16_not_latin1_wire_bytes_and_shrinks(self):
        self.program(r'''
  StorageData area;
  const auto key = Latin("k");
  StorageBytes limit(kStorageAreaQuota / 2, 'a');
  limit.front() = 1;
  assert(area.Put(key, limit));
  assert(area.quota() == kStorageAreaQuota);
  const auto snapshot = area.entries();
  assert(!area.Put(Latin("next"), Latin("x")));
  assert(area.entries() == snapshot);
  assert(area.Put(key, Latin("x")));
  assert(area.quota() == 4);
  assert(area.Put(Latin(""), Latin("")));
  assert(area.quota() == 4);
  area.Delete(key);
  assert(area.quota() == 0 && !area.empty());
  area.Clear();
  assert(area.empty() && area.quota() == 0);
''')

    def test_snapshot_preserves_unicode_nuls_and_lone_surrogates(self):
        self.program(r'''
  StorageData area;
  const StorageBytes embedded_nul{1, 'a', 0, 'b'};
  const auto unicode = Utf16({0xd55c, 0xd83d, 0xde00, 0xd800, 0});
  assert(area.Put(embedded_nul, unicode));
  assert(area.Put(Utf16({0xd55c}), Latin("")));
  auto encoded = EncodeStorageSnapshot(area);
  assert(encoded);
  StorageData restored;
  assert(DecodeStorageSnapshot(*encoded, restored));
  assert(restored.entries() == area.entries());
  assert(restored.quota() == area.quota());
  assert(restored.Get(embedded_nul) == unicode);
  assert(!restored.Put(StorageBytes{}, Latin("x")));
  assert(!restored.Put(Latin("x"), StorageBytes{3, 1}));
  assert(!restored.Put(Utf16({'a'}), Latin("x")));
  assert(!restored.Put(StorageBytes{0, 0, 1, 2}, Latin("x")));
''')

    def test_malformed_snapshots_are_rejected_without_partial_replacement(self):
        self.program(r'''
  StorageData area;
  assert(area.Put(Latin("before"), Latin("kept")));
  const auto original = area.entries();
  auto valid = EncodeStorageSnapshot(area);
  assert(valid);
  for (size_t length = 1; length < valid->size(); ++length) {
    assert(!DecodeStorageSnapshot(std::span(*valid).first(length), area));
    assert(area.entries() == original);
  }
  auto trailing = *valid;
  trailing.push_back(0);
  assert(!DecodeStorageSnapshot(trailing, area));
  auto unknown_version = *valid;
  unknown_version[6] = '2';
  assert(!DecodeStorageSnapshot(unknown_version, area));
  auto opposite_endian = *valid;
  opposite_endian[7] = opposite_endian[7] == 'L' ? 'B' : 'L';
  assert(!DecodeStorageSnapshot(opposite_endian, area));
  auto overflowing_length = *valid;
  overflowing_length[12] = overflowing_length[13] =
      overflowing_length[14] = overflowing_length[15] = 255;
  assert(!DecodeStorageSnapshot(overflowing_length, area));
  auto duplicate = *valid;
  duplicate[8] = 2;
  duplicate.insert(duplicate.end(), valid->begin() + 12, valid->end());
  assert(!DecodeStorageSnapshot(duplicate, area));
  assert(area.entries() == original);
  assert(DecodeStorageSnapshot({}, area));
  assert(area.empty());
''')

    def test_session_wire_key_utf8_value_utf16_uses_same_web_quota(self):
        self.program(r'''
  StorageData session(true);
  const StorageBytes key{0xed, 0x95, 0x9c, 0xf0, 0x9f, 0x98, 0x80};
  assert(session.Put(key, Utf16({0xd55c, 0xd83d, 0xde00}, false)));
  assert(session.quota() == 12);
  const auto kept = session.entries();
  assert(!session.Put(StorageBytes{0xc0, 0xaf}, StorageBytes{}));
  assert(!session.Put(StorageBytes{0xed, 0xa0, 0x80}, StorageBytes{}));
  assert(!session.Put(StorageBytes{0xf4, 0x90, 0x80, 0x80}, StorageBytes{}));
  assert(!session.Put(StorageBytes{'a'}, StorageBytes{1}));
  assert(session.entries() == kept);
  assert(session.Put({}, {}));
  assert(session.quota() == 12);
  session.Delete(key);
  assert(session.quota() == 0);
''')

    def test_store_objects_do_not_share_local_or_session_bytes(self):
        self.program(r'''
  StorageData first_origin, second_origin;
  assert(first_origin.Put(Latin("token"), Latin("first")));
  assert(!second_origin.Get(Latin("token")));
  StorageData first_page(true), second_page(true);
  assert(first_page.Put(StorageBytes{'a'}, Utf16({'v'}, false)));
  assert(!second_page.Get(StorageBytes{'a'}));
  StorageData cloned = first_page;
  assert(cloned.Put(StorageBytes{'a'}, Utf16({'b'}, false)));
  assert(first_page.Get(StorageBytes{'a'}) == Utf16({'v'}, false));
''')

    def test_native_endian_utf16_threshold_and_odd_lengths(self):
        self.program(r'''
  assert(!StorageStringQuota(Utf16({0, 0xff}), false, false));
  assert(StorageStringQuota(Utf16({0x100}), false, false) == 2);
  assert(StorageStringQuota(Utf16({0xff, 0x100, 0}), false, false) == 6);
  assert(StorageStringQuota(Utf16({0xd800}), false, false) == 2);
  auto truncated = Utf16({0xff, 0x100});
  truncated.pop_back();
  assert(!StorageStringQuota(truncated, false, false));
''')

    def test_copy_move_and_assignment_keep_session_mode_across_translation_units(self):
        self.program(r'''
  static_assert(std::is_nothrow_move_constructible_v<StorageData>);
  static_assert(std::is_nothrow_move_assignable_v<StorageData>);
  StorageData CopyStorageData(const StorageData&);
  StorageData original(true);
  assert(original.Put(StorageBytes{'a'}, Utf16({'v'}, false)));
  StorageData copied = CopyStorageData(original);
  StorageData moved(std::move(copied));
  StorageData copy_assigned;
  copy_assigned = moved;
  StorageData move_assigned;
  move_assigned = std::move(copy_assigned);
  assert(move_assigned.entries() == original.entries());
  assert(move_assigned.quota() == original.quota());
  assert(move_assigned.Put(StorageBytes{'b'}, Utf16({'w'}, false)));
  assert(move_assigned.quota() == 8);
  assert(!original.Get(StorageBytes{'b'}));
  assert(!move_assigned.Put(StorageBytes{'c'}, StorageBytes{0}));
''', extra_source=r'''
#include "standalone/common/storage.h"
using namespace blink_standalone;
StorageData CopyStorageData(const StorageData& source) {
  return source;
}
''')


if __name__ == '__main__':
    unittest.main()
