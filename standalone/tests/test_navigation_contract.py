"""Exercise navigation policy/history arithmetic without compiling Blink."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class NavigationContractTests(unittest.TestCase):
    def test_history_branching_replacement_capacity_and_signed_offsets(self):
        self.run_contract(r'''
  SessionHistory<std::string> h;
  assert(h.index() == -1 && h.size() == 0 && h.Target(0) == -1);
  h.Commit("first", false); h.Commit("second", false);
  assert(h.Target(-1) == 0 && h.Target(1) == -1);
  assert(h.Target(INT32_MIN) == -1 && h.Target(INT32_MAX) == -1);
  h.Commit("first-state", false, 0);
  assert(h.index() == 0 && h.size() == 2);
  h.Commit("replace-first", true);
  assert(h.index() == 0 && *h.Current() == "replace-first" && h.size() == 2);
  h.Commit("branch", false);
  assert(h.size() == 2 && *h.Current() == "branch" && *h.At(0) == "replace-first");
  for (unsigned i = 0; i < 100; ++i) h.Commit(std::to_string(i), false);
  assert(h.size() == h.kCapacity && h.index() == 63 && *h.At(0) == "36");
  h.Clear(); assert(h.size() == 0 && h.index() == -1);
''')

    def test_redirect_rules_and_response_header_validation(self):
        self.run_contract(r'''
  assert(RedirectStatus(301) && RedirectStatus(308));
  assert(!RedirectStatus(304) && !RedirectStatus(200));
  assert(RedirectBecomesGet(302, "POST") && RedirectBecomesGet(303, "PUT"));
  assert(!RedirectBecomesGet(307, "POST") && !RedirectBecomesGet(308, "PUT"));
  assert(!RedirectBecomesGet(303, "HEAD"));
  const auto post = "Content-Type: application/x-www-form-urlencoded\r\nAuthorization: token\r\nCookie: a=1\r\nAccept: text/html\r\n";
  std::string filtered;
  assert(RedirectRequestHeaders(post, false, true, filtered));
  assert(filtered.find("Content-Type:") != std::string::npos);
  assert(filtered.find("Authorization:") == std::string::npos && filtered.find("Cookie:") == std::string::npos);
  assert(RedirectRequestHeaders(post, true, false, filtered));
  assert(filtered.find("Content-Type:") == std::string::npos && filtered.find("Authorization:") != std::string::npos);
  assert(UnsupportedNavigationPolicy("Permissions-Policy"));
  assert(UnsupportedNavigationPolicy("Cross-Origin-Embedder-Policy"));
  assert(!UnsupportedNavigationPolicy("Content-Security-Policy"));
  std::vector<std::pair<std::string,std::string>> fields;
  assert(ParseNavigationHeaders("Content-Type: text/html\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\n", fields));
  assert(fields.size() == 3 && fields[1].second == "a=1");
  assert(ParseNavigationHeaders("", fields) && fields.empty());
  assert(!ParseNavigationHeaders("bad name: value", fields));
  assert(!ParseNavigationHeaders("A: value\nInjected: yes", fields));
  assert(!ParseNavigationHeaders(std::string("A: a\0b", 6), fields));
  assert(!ParseNavigationHeaders("A: a\r\n folded", fields));
  assert(!ParseNavigationHeaders(std::string(kMaxNavigationHeaders + 1, 'x'), fields));
  assert(HeaderToken("POST") && !HeaderToken("") && !HeaderToken("GET\n"));
''')

    def run_contract(self, body):
        compiler = shutil.which('c++')
        if not compiler:
            self.skipTest('C++ compiler unavailable')
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'navigation.cc'
            path.write_text('#include "standalone/common/navigation.h"\n'
                            '#include <cassert>\n#include <climits>\n'
                            'using namespace blink_standalone;\nint main(){\n' + body + '\n}\n')
            output = pathlib.Path(directory) / 'navigation'
            result = subprocess.run([compiler, '-std=c++20', '-Wall', '-Wextra',
                                     '-Wconversion', '-Werror', '-I', str(ROOT),
                                     str(path), '-o', str(output)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(output)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
