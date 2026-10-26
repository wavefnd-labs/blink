"""Compile and execute only the dependency-free C input boundary validators.

These programs do not link Blink or exercise native input dispatch. The native
embedding smoke harness separately owns real DOM/editing/scroll assertions.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class InputValidationTests(unittest.TestCase):
    def test_fixed_abi_arrays_pass_clang_unsafe_buffer_diagnostics(self):
        pinned = ROOT / 'standalone/work/graph/src/third_party/llvm-build/Release+Asserts/bin/clang++'
        compiler = str(pinned) if pinned.is_file() else shutil.which('clang++')
        if not compiler:
            self.skipTest('Clang compiler unavailable')
        source = '''#include "standalone/common/input_validation.h"
be_status Validate(const be_input_event& input) {
  return blink_standalone::ValidateInputEvent(input);
}
'''
        checked = subprocess.run([compiler, '-std=c++20', '-Wall', '-Wextra', '-Werror',
                                  '-Wunsafe-buffer-usage', '-fsyntax-only', '-I', str(ROOT),
                                  '-x', 'c++', '-'], input=source, text=True,
                                 capture_output=True, timeout=30)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def program(self, body):
        compiler = shutil.which('c++')
        if compiler is None:
            self.skipTest('C++ compiler unavailable')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.cc'
            executable = Path(directory) / 'input'
            source.write_text('''
#include <cassert>
#include <cstring>
#include <limits>
#include "standalone/common/input_validation.h"
using namespace blink_standalone;
be_input_event Event(uint32_t type) {
  be_input_event event{};
  event.struct_size = sizeof(event);
  event.abi_version = BE_ABI_VERSION;
  event.type = type;
  return event;
}
int main() {
''' + body + '\n}\n')
            built = subprocess.run([compiler, '-std=c++20', '-Wall', '-Wextra', '-Werror',
                                    '-I', str(ROOT), str(source), '-o', str(executable)],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            executed = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)

    def test_unicode_scalar_boundaries_and_terminated_arrays(self):
        self.program(r'''
  size_t size = 0;
  char valid[] = "A\xed\x95\x9c\xf0\x9f\x98\x80\xf4\x8f\xbf\xbf";
  assert(BoundedInputUtf8(valid, &size));
  assert(size == sizeof(valid) - 1);
  char no_terminator[3] = {'a', 'b', 'c'};
  assert(!BoundedInputUtf8(no_terminator));
  char only_terminator[1] = {};
  assert(BoundedInputUtf8(only_terminator, &size) && size == 0);
  char only_lead[2] = {'\xe2', 0};
  assert(!BoundedInputUtf8(only_lead));
  const char* invalid[] = {"\xc0\xaf", "\xc1\x81", "\xed\xa0\x80",
                           "\xe0\x80\x80", "\xf0\x80\x80\x80", "\x80",
                           "\xf4\x90\x80\x80", "\xf5\x80\x80\x80", "\xe2\x82"};
  for (const char* data : invalid) {
    char buffer[16]{};
    std::strcpy(buffer, data);
    assert(!BoundedInputUtf8(buffer));
  }
''')

    def test_input_coordinate_limits_reject_nan_infinity_and_overflow(self):
        self.program(r'''
  auto event = Event(BE_INPUT_MOUSE_MOVE);
  event.x = -kMaxInputCoordinate;
  event.y = kMaxInputCoordinate;
  assert(ValidateInputEvent(event) == BE_OK);
  event.x = kMaxInputCoordinate + 1;
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event.x = std::numeric_limits<double>::quiet_NaN();
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event.x = std::numeric_limits<double>::infinity();
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event = Event(BE_INPUT_WHEEL);
  event.delta_y = 40;
  assert(ValidateInputEvent(event) == BE_OK);
  event.delta_x = std::numeric_limits<double>::max();
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
''')

    def test_mouse_transition_buttons_clicks_and_chorded_state(self):
        self.program(r'''
  auto event = Event(BE_INPUT_MOUSE_DOWN);
  event.button = BE_BUTTON_LEFT;
  event.buttons = BE_BUTTONS_LEFT | BE_BUTTONS_RIGHT;
  event.click_count = 1;
  assert(ValidateInputEvent(event) == BE_OK);
  event.type = BE_INPUT_MOUSE_UP;
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event.buttons = BE_BUTTONS_RIGHT;
  assert(ValidateInputEvent(event) == BE_OK);
  event.click_count = 4;
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event.click_count = 0;
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event = Event(BE_INPUT_MOUSE_MOVE);
  event.buttons = BE_BUTTONS_LEFT;
  assert(ValidateInputEvent(event) == BE_OK);
  event.button = BE_BUTTON_LEFT;
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event = Event(BE_INPUT_MOUSE_LEAVE);
  event.modifiers = BE_MOD_AUTO_REPEAT;
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event.modifiers = 0;
  event.buttons = 1u << 30;
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  assert(InputButtonMask(BE_BUTTON_BACK) == BE_BUTTONS_BACK);
  assert(InputButtonMask(BE_BUTTON_FORWARD) == BE_BUTTONS_FORWARD);
''')

    def test_key_fields_and_text_commit_are_distinct(self):
        self.program(r'''
  auto event = Event(BE_INPUT_KEY_DOWN);
  event.windows_key_code = 65;
  std::strcpy(event.dom_code, "KeyA");
  std::strcpy(event.dom_key, "a");
  assert(ValidateInputEvent(event) == BE_OK);
  std::strcpy(event.text_utf8, "a");
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event.text_utf8[0] = 0;
  event.windows_key_code = 256;
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event.windows_key_code = 65;
  event.native_key_code = std::numeric_limits<uint32_t>::max();
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event.native_key_code = 0;
  std::memset(event.dom_code, 'x', sizeof(event.dom_code));
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  std::strcpy(event.dom_code, "Key-A");
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  std::strcpy(event.dom_code, "KeyA");
  std::strcpy(event.dom_key, "\xed\xa0\x80");
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
''')

    def test_utf8_text_commit_accepts_korean_and_rejects_invalid_or_unbounded_input(self):
        self.program(r'''
  auto event = Event(BE_INPUT_TEXT);
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  std::strcpy(event.text_utf8, "\xed\x95\x9c\x41");
  assert(ValidateInputEvent(event) == BE_OK);
  std::memset(event.text_utf8, 'x', sizeof(event.text_utf8));
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event.text_utf8[sizeof(event.text_utf8) - 1] = 0;
  assert(ValidateInputEvent(event) == BE_OK);
  std::strcpy(event.text_utf8, "\xf4\x90\x80\x80");
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
''')

    def test_input_metadata_and_unknown_flags_fail_before_dispatch(self):
        self.program(r'''
  auto event = Event(BE_INPUT_MOUSE_MOVE);
  event.struct_size -= 1;
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event.struct_size = sizeof(event);
  event.abi_version += 1;
  assert(ValidateInputEvent(event) == BE_ABI_MISMATCH);
  event.abi_version = BE_ABI_VERSION;
  event.type = 255;
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
  event.type = BE_INPUT_MOUSE_MOVE;
  event.modifiers = 1u << 31;
  assert(ValidateInputEvent(event) == BE_INVALID_ARGUMENT);
''')


if __name__ == '__main__':
    unittest.main()
