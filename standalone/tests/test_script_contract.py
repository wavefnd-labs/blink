"""Check the script-result boundary without compiling or mocking Blink/V8."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class ScriptContractTests(unittest.TestCase):
    def test_script_boundary_is_owned_bytes_and_forward_declared_frame(self):
        compiler = shutil.which("c++")
        if not compiler:
            self.skipTest("C++ compiler unavailable")
        source = r'''
#include "standalone/engine/script.h"
#include <string>
#include <string_view>
#include <type_traits>
using blink_standalone::ScriptResult;
using Function = ScriptResult (*)(blink::LocalFrame&, std::string_view,
                                 std::string_view);
static_assert(std::is_same_v<decltype(&blink_standalone::EvaluateScript), Function>);
static_assert(std::is_same_v<decltype(ScriptResult::utf8_result), std::string>);
static_assert(std::is_same_v<decltype(ScriptResult::status), be_status>);
static_assert(std::is_same_v<decltype(ScriptResult::exception), bool>);
static_assert(std::is_copy_constructible_v<ScriptResult>);
static_assert(std::is_move_constructible_v<ScriptResult>);
static_assert(BE_SCRIPT_EXCEPTION != BE_SCRIPT_ERROR);
static_assert(BE_SCRIPT_EXCEPTION != BE_NOT_SUPPORTED);
'''
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "script_contract.cc"
            path.write_text(source)
            result = subprocess.run(
                [compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                 "-I", str(ROOT), "-fsyntax-only", str(path)],
                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
