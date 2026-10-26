"""Temporary native-receipt fixtures only; never engine runtime evidence."""
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('runtime_profile_fixture',
    Path(__file__).with_name('test_runtime_checks.py'))
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)
runtime = fixture.runtime


class ProfileRuntimeIsolationTests(unittest.TestCase):
    setUp = fixture.RuntimeChecksTests.setUp
    write = fixture.RuntimeChecksTests.write
    flags = fixture.RuntimeChecksTests.flags
    build = fixture.RuntimeChecksTests.build
    result = fixture.RuntimeChecksTests.result

    def configuration(self):
        self.write('args.gn', b'is_debug = false\n')
        (self.view.parent / 'view.json').write_text('{"key":"fixture"}\n')

    def test_dev_receipt_requires_and_binds_profile_configuration(self):
        with self.assertRaisesRegex(ValueError, 'requires its GN arguments'):
            runtime.build_receipt(self.view, self.output, True, copy.deepcopy(self.state),
                                  root=self.root, build_profile='dev')
        self.configuration()
        receipt = runtime.build_receipt(self.view, self.output, True, copy.deepcopy(self.state),
                                        root=self.root, build_profile='dev')
        self.assertEqual(receipt['build_profile'], 'dev')
        self.assertEqual(set(receipt['build_configuration']),
                         {'args_gn_sha256', 'view_manifest_sha256'})
        runtime.verify_build(receipt, self.root, build_profile='dev')
        with self.assertRaisesRegex(ValueError, 'another build profile'):
            runtime.verify_build(receipt, self.root)

    def test_dev_smoke_rejection_cannot_write_release_or_dev_receipts(self):
        release = self.root / runtime.SMOKE_REPORT
        release.parent.mkdir(parents=True, exist_ok=True)
        release.write_text('preserved release runtime receipt\n')
        dev = self.root / 'standalone/out/dev/runtime.json'
        with self.assertRaisesRegex(ValueError, 'only the release profile'):
            runtime.smoke(root=self.root, report_path=dev, build_profile='dev',
                          build_report_path=self.root / 'absent-dev-build.json')
        self.assertEqual(release.read_text(), 'preserved release runtime receipt\n')
        self.assertFalse(dev.exists())

    def test_explicit_release_build_and_smoke_reports_stay_isolated(self):
        receipt = self.build()
        (self.root / runtime.BUILD_REPORT).unlink()
        build_path = self.root / 'standalone/out/isolated/release-build.json'
        report_path = self.root / 'standalone/out/isolated/release-smoke.json'
        runtime.automation.write_json(build_path, receipt)
        release = self.root / runtime.SMOKE_REPORT
        release.parent.mkdir(parents=True, exist_ok=True)
        release.write_text('preserved default receipt\n')
        with mock.patch.object(runtime.subprocess, 'run', return_value=self.result()):
            result = runtime.smoke(root=self.root, report_path=report_path,
                                   build_report_path=build_path)
        self.assertTrue(result['runtime_smoke_passed'])
        self.assertEqual(result['build_profile'], 'release')
        self.assertTrue(report_path.is_file())
        self.assertEqual(release.read_text(), 'preserved default receipt\n')

    def test_args_or_view_change_invalidates_matching_binaries(self):
        self.configuration()
        receipt = self.build()
        self.write('args.gn', b'is_debug = true\n')
        with self.assertRaisesRegex(ValueError, 'changed after compilation'):
            runtime.verify_build(receipt, self.root)
        self.write('args.gn', b'is_debug = false\n')
        (self.view.parent / 'view.json').write_text('{"key":"other-profile"}\n')
        with self.assertRaisesRegex(ValueError, 'changed after compilation'):
            runtime.verify_build(receipt, self.root)

    def dev_build(self):
        self.configuration()
        receipt = runtime.build_receipt(self.view, self.output, True, copy.deepcopy(self.state),
                                        root=self.root, build_profile='dev')
        receipt.update(exit_code=0, dry_run=False)
        path = self.root / 'standalone/reports/profiles/dev-cache' / runtime.BUILD_REPORT.name
        runtime.automation.write_json(path, receipt)
        return path

    def test_dev_check_uses_its_own_receipts_and_never_stages_an_sdk(self):
        self.dev_build()
        release = self.root / runtime.SMOKE_REPORT
        runtime.automation.write_json(release, {'preserved': 'release smoke'})
        release_check = self.root / runtime.CHECK_REPORT
        runtime.automation.write_json(release_check, {'preserved': 'release check'})
        with mock.patch.object(runtime.automation, 'stage_sdk') as stage:
            with mock.patch.object(runtime.subprocess, 'run', return_value=self.result()):
                result = runtime.check(root=self.root, build_profile='dev')
            stage.assert_not_called()
        self.assertTrue(result['runtime_check_passed'])
        self.assertTrue(result['native_smoke_passed'])
        self.assertTrue(result['check_only'])
        self.assertFalse(result['sdk_staged'])
        self.assertFalse(result['runtime_smoke_passed'])
        self.assertEqual(result['build_profile'], 'dev')
        self.assertTrue((self.root / 'standalone/reports/profiles/dev-cache' / runtime.CHECK_REPORT.name).is_file())
        self.assertEqual(runtime.automation.read_json(release), {'preserved': 'release smoke'})
        self.assertEqual(runtime.automation.read_json(release_check), {'preserved': 'release check'})
        self.assertFalse((self.root / 'standalone/out/sdk').exists())
        self.assertFalse(runtime.automation.release_plan(self.root, result)['publishable'])

    def test_release_check_does_not_replace_release_smoke_or_stage(self):
        self.build()
        release = self.root / runtime.SMOKE_REPORT
        runtime.automation.write_json(release, {'preserved': True})
        with mock.patch.object(runtime.automation, 'stage_sdk') as stage:
            with mock.patch.object(runtime.subprocess, 'run', return_value=self.result()):
                result = runtime.check(root=self.root)
            stage.assert_not_called()
        self.assertTrue(result['runtime_check_passed'])
        self.assertEqual(runtime.automation.read_json(release), {'preserved': True})

    def test_check_rejects_stale_sources_before_executing_child(self):
        self.dev_build()
        self.state['git_commit'] = 'e' * 40
        with mock.patch.object(runtime.subprocess, 'run') as child:
            with self.assertRaisesRegex(ValueError, 'stale'):
                runtime.check(root=self.root, build_profile='dev')
            child.assert_not_called()
        report = self.root / 'standalone/reports/profiles/dev-cache' / runtime.CHECK_REPORT.name
        self.assertFalse(runtime.automation.read_json(report)['runtime_check_passed'])

    def test_failed_child_or_pixels_cannot_leave_a_successful_check(self):
        self.dev_build()
        bad_pixels = fixture.passing_json()
        bad_pixels['pixel_assertions'] = False
        for result in [self.result(code=9), self.result(bad_pixels)]:
            with self.subTest(result=result):
                with mock.patch.object(runtime.subprocess, 'run', return_value=result):
                    with self.assertRaises(ValueError):
                        runtime.check(root=self.root, build_profile='dev')
                report = self.root / 'standalone/reports/profiles/dev-cache' / runtime.CHECK_REPORT.name
                self.assertFalse(runtime.automation.read_json(report)['runtime_check_passed'])

    def test_check_cannot_overwrite_a_build_or_release_smoke_receipt(self):
        build = self.dev_build()
        before = build.read_bytes()
        for path in [build, self.root / runtime.SMOKE_REPORT, self.root / runtime.BUILD_REPORT]:
            with mock.patch.object(runtime.subprocess, 'run') as child:
                with self.assertRaisesRegex(ValueError, 'cannot overwrite'):
                    runtime.check(root=self.root, report_path=path, build_profile='dev')
                child.assert_not_called()
        self.assertEqual(build.read_bytes(), before)

    def test_source_mutation_during_native_check_invalidates_success(self):
        self.dev_build()
        def mutate(*args, **kwargs):
            self.state['support_files']['changed'] = 'f' * 64
            return self.result()
        with mock.patch.object(runtime.subprocess, 'run', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'stale'):
                runtime.check(root=self.root, build_profile='dev')
        report = self.root / 'standalone/reports/profiles/dev-cache' / runtime.CHECK_REPORT.name
        self.assertFalse(runtime.automation.read_json(report)['runtime_check_passed'])


if __name__ == '__main__':
    unittest.main()
