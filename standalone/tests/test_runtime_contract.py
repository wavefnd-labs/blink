"""Compile the public ABI and arithmetic guards without building Blink.

These are contract checks, not evidence that the native backend links/renders.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class RuntimeContractTests(unittest.TestCase):
    def compile(self, compiler, source, suffix, flags, run=False):
        if not shutil.which(compiler):
            self.skipTest(f"{compiler} unavailable")
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / ("contract" + suffix)
            path.write_text(source)
            output = pathlib.Path(directory) / "contract"
            command = [compiler, *flags, "-Wall", "-Wextra", "-Werror",
                       "-I", str(ROOT), str(path)]
            command += ["-o", str(output)] if run else ["-fsyntax-only"]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            if run:
                result = subprocess.run([str(output)], capture_output=True,
                                        text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_header_is_c11_without_chromium_headers(self):
        self.compile("cc", r'''
#include "standalone/include/blink_engine.h"
#include <stddef.h>
_Static_assert(BE_ABI_VERSION == 1, "ABI version");
_Static_assert(offsetof(be_engine_config, struct_size) == 0, "size first");
_Static_assert(offsetof(be_engine_config, abi_version) == 4, "version second");
_Static_assert(offsetof(be_resource_response, struct_size) == 0, "response size");
_Static_assert(BE_OK == 0 && BE_WRONG_THREAD != BE_INVALID_STATE, "statuses");
be_status (*create_function)(const be_engine_config*, be_engine**) = be_engine_create;
be_status (*paint_function)(be_page*, uint8_t*, size_t, size_t) = be_page_paint_rgba;
be_status (*script_function)(be_page*, const char*, size_t, const char*, be_script_result*) = be_page_evaluate_script;
be_status (*input_function)(be_page*, const be_input_event*, uint32_t*) = be_page_dispatch_input;
be_status (*navigate_function)(be_page*, const char*) = be_page_navigate;
#if UINTPTR_MAX == UINT64_MAX
_Static_assert(sizeof(be_engine_config) == 72, "original ABI 1 config layout");
_Static_assert(sizeof(be_resource_response) == 56, "original response layout");
_Static_assert(sizeof(be_page_config) == 16, "original page config layout");
#endif
int main(void) { be_page_config c = {sizeof(c), BE_ABI_VERSION, 64, 64};
  return c.width != 64; }
''', ".c", ["-std=c11"])

    def test_header_is_cpp20_and_structs_remain_plain_data(self):
        self.compile("c++", r'''
#include "standalone/include/blink_engine.h"
#include <type_traits>
static_assert(std::is_standard_layout_v<be_engine_config>);
static_assert(std::is_trivially_copyable_v<be_resource_response>);
static_assert(std::is_standard_layout_v<be_page_config>);
static_assert(std::is_standard_layout_v<be_script_result>);
static_assert(std::is_trivially_copyable_v<be_input_event>);
static_assert(std::is_standard_layout_v<be_navigation_client>);
static_assert(std::is_standard_layout_v<be_navigation_response>);
static_assert((BE_CAP_LOCAL_HTML & BE_CAP_SOFTWARE_RGBA) == 0);
static_assert((BE_CAP_HOST_RESOURCES & BE_CAP_TASK_PUMP) == 0);
''', ".cc", ["-std=c++20"])

    def test_shared_library_export_policy_hides_host_allocator_symbols(self):
        if not shutil.which('cc') or not shutil.which('nm'):
            self.skipTest('cc/nm unavailable')
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / 'boundary.c'
            source.write_text('''#include <stddef.h>
void *malloc(size_t n) { (void)n; return 0; }
int dependency_detail(void) { return 7; }
int be_boundary_probe(void) { return 11; }
''')
            library = pathlib.Path(directory) / 'boundary.so'
            subprocess.run(['cc', '-shared', '-fPIC', str(source),
                '-Wl,--version-script=' + str(ROOT / 'standalone/engine/exports.map'),
                '-o', str(library)], capture_output=True, text=True, check=True)
            symbols = subprocess.check_output(['nm', '-D', '--defined-only', str(library)], text=True)
            self.assertEqual([line.split()[-1] for line in symbols.splitlines()], ['be_boundary_probe'])

    def test_dimensions_and_pixel_size_reject_overflow(self):
        self.compile("c++", r'''
#include "standalone/common/limits.h"
#include <cassert>
#include <limits>
using namespace blink_standalone;
int main() {
  assert(ValidSize(1, 1));
  assert(!ValidSize(0, 1));
  assert(!ValidSize(1, 0));
  assert(ValidSize(kMaxDimension, kMaxDimension));
  assert(!ValidSize(kMaxDimension + 1, 1));
  assert(ValidPixels(8, 4, 128, 32));
  assert(ValidPixels(8, 4, 160, 40));
  assert(!ValidPixels(8, 4, 127, 32));
  assert(!ValidPixels(8, 4, 128, 31));
  const auto maximum = std::numeric_limits<size_t>::max();
  assert(!ValidPixels(8, 4, maximum, maximum));
  assert(!ValidPixels(8, 4, maximum, maximum / 2));
}
''', ".cc", ["-std=c++20"], run=True)


if __name__ == "__main__":
    unittest.main()
