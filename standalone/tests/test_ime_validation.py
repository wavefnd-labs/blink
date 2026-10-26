from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ImeValidationTests(unittest.TestCase):
    def test_span_boundary_passes_clang_unsafe_buffer_diagnostics(self):
        pinned = ROOT / 'standalone/work/graph/src/third_party/llvm-build/Release+Asserts/bin/clang++'
        compiler = str(pinned) if pinned.is_file() else shutil.which('clang++')
        if not compiler:
            self.skipTest('Clang compiler unavailable')
        source = '''#include "standalone/common/ime_validation.h"
be_status Validate(const be_ime_composition& composition) {
  return blink_standalone::ValidateImeComposition(composition);
}
'''
        checked = subprocess.run([compiler, '-std=c++20', '-Wall', '-Wextra', '-Werror',
                                  '-Wunsafe-buffer-usage', '-fsyntax-only', '-I', str(ROOT),
                                  '-x', 'c++', '-'], input=source, text=True,
                                 capture_output=True, timeout=30)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_c11_additive_header_is_self_contained(self):
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('C compiler unavailable')
        source = '''#include "standalone/include/blink_ime.h"
_Static_assert(BE_ABI_VERSION == 1u, "ABI remains additive");
be_status (*query)(be_page*, be_ime_state*, char*, size_t, size_t*) =
    &be_page_ime_get_state;
be_status (*commit)(be_page*, const char*, size_t, int32_t) =
    &be_page_ime_commit_text;
'''
        subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-fsyntax-only', '-I', str(ROOT), '-x', 'c', '-'],
                       input=source, text=True, capture_output=True, check=True)

    def test_utf16_boundaries_composition_segments_and_commit_caret(self):
        compiler = shutil.which('c++')
        if not compiler:
            self.skipTest('C++ compiler unavailable')
        source = r'''
#include "standalone/common/ime_validation.h"
#include <cassert>
#include <climits>
#include <string>
using namespace blink_standalone;
int main() {
  std::vector<uint8_t> boundaries;
  const std::string text = "A\xF0\x9F\x98\x80\xED\x95\x9C"; // A, emoji, 한
  assert(DecodeImeUtf8(text, boundaries));
  assert((boundaries == std::vector<uint8_t>{1, 1, 0, 1, 1}));
  for (const std::string& invalid : {std::string("\x80"), std::string("\xC0\xAF"),
       std::string("\xED\xA0\x80"), std::string("\xF4\x90\x80\x80"),
       std::string("\xF0\x9F\x98"), std::string("\xE2\x28\xA1")})
    assert(!DecodeImeUtf8(invalid, boundaries));
  assert(DecodeImeUtf8(std::string_view("A\0B", 3), boundaries));
  assert(boundaries.size() == 4);
  assert(DecodeImeUtf8(std::string(kMaxImeTextBytes, 'x'), boundaries));
  assert(!DecodeImeUtf8(std::string(kMaxImeTextBytes + 1, 'x'), boundaries));

  be_ime_span spans[] = {{0, 1, BE_IME_THICKNESS_THIN, BE_IME_UNDERLINE_SOLID},
                         {1, 3, BE_IME_THICKNESS_THICK, BE_IME_UNDERLINE_DOT}};
  be_ime_composition value{};
  value.struct_size = sizeof(value);
  value.abi_version = BE_ABI_VERSION;
  value.text_utf8 = text.data();
  value.text_size = text.size();
  value.selection_start_utf16 = 3;
  value.selection_end_utf16 = 4;
  value.spans = spans;
  value.span_count = 2;
  assert(ValidateImeComposition(value) == BE_OK);
  value.selection_start_utf16 = 2;
  assert(ValidateImeComposition(value) == BE_INVALID_ARGUMENT);
  value.selection_start_utf16 = 3;
  spans[1].start_utf16 = 0;
  assert(ValidateImeComposition(value) == BE_INVALID_ARGUMENT);
  spans[1].start_utf16 = 2;
  assert(ValidateImeComposition(value) == BE_INVALID_ARGUMENT);
  spans[1].start_utf16 = 1;
  spans[1].end_utf16 = 5;
  assert(ValidateImeComposition(value) == BE_INVALID_ARGUMENT);
  spans[1].end_utf16 = 3;
  spans[1].thickness = 3;
  assert(ValidateImeComposition(value) == BE_INVALID_ARGUMENT);
  spans[1].thickness = 2;
  spans[1].underline_style = 5;
  assert(ValidateImeComposition(value) == BE_INVALID_ARGUMENT);
  spans[1].underline_style = BE_IME_UNDERLINE_SOLID;
  value.span_count = kMaxImeSpans + 1;
  assert(ValidateImeComposition(value) == BE_INVALID_ARGUMENT);
  value.span_count = 2;
  value.spans = nullptr;
  assert(ValidateImeComposition(value) == BE_INVALID_ARGUMENT);
  value.span_count = 0;
  value.text_utf8 = nullptr;
  assert(ValidateImeComposition(value) == BE_INVALID_ARGUMENT);
  value.text_size = 0;
  value.selection_start_utf16 = value.selection_end_utf16 = 0;
  assert(ValidateImeComposition(value) == BE_OK);
  value.abi_version++;
  assert(ValidateImeComposition(value) == BE_ABI_MISMATCH);
  value.abi_version = BE_ABI_VERSION;
  value.struct_size--;
  assert(ValidateImeComposition(value) == BE_INVALID_ARGUMENT);

  assert(ValidateImeCommit(text, 0) == BE_OK);
  assert(ValidateImeCommit(text, -1) == BE_OK);
  assert(ValidateImeCommit(text, -2) == BE_INVALID_ARGUMENT); // split emoji
  assert(ValidateImeCommit(text, -3) == BE_OK);
  assert(ValidateImeCommit(text, -4) == BE_OK);
  assert(ValidateImeCommit(text, -5) == BE_INVALID_ARGUMENT);
  assert(ValidateImeCommit(text, INT_MIN) == BE_INVALID_ARGUMENT);
  assert(ValidateImeCommit(text, 1) == BE_INVALID_ARGUMENT);
  assert(ValidateImeCommit({}, 0) == BE_OK);
  assert(ValidateImeCommit({}, -1) == BE_INVALID_ARGUMENT);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / 'ime.cc'
            binary_path = Path(directory) / 'ime'
            source_path.write_text(source)
            result = subprocess.run([compiler, '-std=c++20', '-Wall', '-Wextra', '-Werror',
                                     '-I', str(ROOT), str(source_path), '-o', str(binary_path)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(binary_path)], capture_output=True, check=True)


if __name__ == '__main__':
    unittest.main()
