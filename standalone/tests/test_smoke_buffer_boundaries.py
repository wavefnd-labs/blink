"""Exercise only the harness's argument and storage-copy boundaries.

Unused engine functions are discarded during this small native helper test;
this does not link Blink or supply engine rendering/runtime evidence.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SmokeBufferBoundaryTests(unittest.TestCase):
    def test_empty_arguments_terminated_literals_and_owned_storage_copies(self):
        compiler = shutil.which('c++')
        if compiler is None:
            self.skipTest('C++ compiler unavailable')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'smoke_buffers.cc'
            executable = Path(directory) / 'smoke_buffers'
            source.write_text(r'''
#include <cassert>
#define main BlinkEmbeddingSmokeMain
#include "standalone/engine/smoke.cc"
#undef main
int main() {
  assert(Argument({}, "icu-data").empty());
  char program[] = "--icu-data=program-name";
  char unrelated[] = "--not-icu-data=ignored";
  char first[] = "--icu-data=/한글/first.dat";
  char duplicate[] = "--icu-data=/second.dat";
  std::array<char*, 4> arguments{program, unrelated, first, duplicate};
  assert(Argument(arguments, "icu-data") == "/한글/first.dat");
  assert(Argument(arguments, "blink-resources").empty());
  be_input_event input{};
  SetLiteral(input.dom_code, "KeyA");
  SetLiteral(input.dom_key, "a");
  SetLiteral(input.text_utf8, "한A");
  assert(std::string_view(input.dom_code) == "KeyA");
  assert(std::string_view(input.dom_key) == "a");
  assert(std::string_view(input.text_utf8) == "한A");

  StorageContext storage;
  std::array<uint8_t, 4> bytes{0, 128, 255, 0};
  assert(SaveStorage(&storage, "snapshot", bytes.data(), bytes.size()) == BE_OK);
  bytes.fill(1);
  assert((storage.snapshots.at("snapshot") == std::vector<uint8_t>{0, 128, 255, 0}));
  assert(SaveStorage(&storage, "snapshot", nullptr, 0) == BE_OK);
  assert(storage.snapshots.at("snapshot").empty());
  assert(SaveStorage(&storage, "invalid", nullptr, 1) == BE_INVALID_ARGUMENT);
  assert(!storage.snapshots.contains("invalid"));
  storage.fail_save = true;
  assert(SaveStorage(&storage, "failed", bytes.data(), bytes.size()) == BE_RESOURCE_ERROR);
  assert(!storage.snapshots.contains("failed"));
}
''')
            built = subprocess.run(
                [compiler, '-std=c++23', '-O0', '-Wall', '-Wextra', '-Werror',
                 '-ffunction-sections', '-fdata-sections', '-Wl,--gc-sections',
                 '-I', str(ROOT), str(source), '-o', str(executable)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            executed = subprocess.run([str(executable)], capture_output=True,
                                      text=True, timeout=10)
            self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)


if __name__ == '__main__':
    unittest.main()
