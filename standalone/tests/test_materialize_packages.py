import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

TOOL = Path(__file__).resolve().parents[1] / 'tools/materialize_packages.py'
spec = importlib.util.spec_from_file_location('materialize_packages', TOOL)
packages = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packages)


class PackageMaterializationTests(unittest.TestCase):
    def test_preflight_uses_only_version_commands_and_reports_optional_cargo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / 'graph'
            source = work / 'src'
            source.mkdir(parents=True)
            view = root / 'view'
            (view / 'out/Engine').mkdir(parents=True)
            (view / 'third_party').symlink_to(source / 'third_party', target_is_directory=True)
            (view / 'buildtools').symlink_to(source / 'buildtools', target_is_directory=True)
            engine = root / 'standalone/work/engine'
            engine.mkdir(parents=True)
            (engine / 'current.json').write_text(json.dumps({'view': str(view)}))
            (view / 'out/Engine/build.ninja').write_text('command = ' + str(work / 'tools/gn') + ' --regeneration gen .')
            needles = ['../../third_party/llvm-build/Release+Asserts/bin/clang',
                       '../../third_party/llvm-build/Release+Asserts/bin/clang++',
                       '../../third_party/rust-toolchain/bin/rustc',
                       '../../third_party/node/linux/node-linux-x64/bin/node',
                       '../../third_party/gperf/cipd/bin/gperf',
                       '../../third_party/devtools-frontend/src/third_party/esbuild/esbuild',
                       '../../third_party/dawn/tools/generate-sources-gn.py',
                       '../../buildtools/third_party/mold/cipd/mold', '/usr/bin/python3']
            (view / 'out/Engine/toolchain.ninja').write_text('\n'.join(needles))
            lock = root / 'lock.json'
            lock.write_text(json.dumps({'chromium_revision': 'fixed', 'profile': {
                'name': 'linux', 'script_executable': '/usr/bin/python3', 'gn_sha256': '0' * 64}}))
            report_path = root / 'report.json'

            def probe(command, **kwargs):
                self.assertEqual(command[1:], ['version'] if command[0].endswith('/bin/go') else ['--version'])
                return subprocess.CompletedProcess(command, 127 if command[0].endswith('/cargo') else 0,
                                                   stdout='version', stderr='missing library' if command[0].endswith('/cargo') else '')
            with mock.patch.multiple(packages, ROOT=root, WORK=work, LOCK=lock, TOOLS_REPORT=report_path):
                with mock.patch.object(packages, 'sha256', return_value='0' * 64):
                    with mock.patch.object(packages.subprocess, 'run', side_effect=probe):
                        report = packages.check_tools()
            self.assertFalse(report['all_tools_executable'])
            self.assertTrue(report['required_tools_executable'])
            self.assertEqual(report['optional_tools_unavailable'], ['cargo'])
            self.assertTrue(report['ninja_paths_verified'])
            self.assertEqual(report['tools']['go']['command'][1:], ['version'])
            self.assertTrue(report['ninja_tool_references']['go']['resolves_to_probed_tool'])
            self.assertFalse(report['compiled'])

    def test_nested_package_preserves_parent_pin_and_original_deps_without_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / 'standalone/tools'
            tools.mkdir(parents=True)
            (tools / 'dependencies.py').symlink_to(TOOL.parent / 'dependencies.py')
            parent_name = 'third_party/devtools/src'
            revision = 'a' * 40
            original = b"use_relative_paths = True\nvars = {}\ndeps = {'third_party/esbuild': {'dep_type': 'cipd', 'condition': 'non_git_source', 'packages': [{'package': 'infra/esbuild/${{platform}}', 'version': 'version:fixed'}]}}\nhooks = [{'action': ['must-never-run']}]\n"
            preserved = 'standalone/upstream/devtools.DEPS'
            (root / preserved).parent.mkdir(parents=True)
            (root / preserved).write_bytes(original)
            deps_hash = hashlib.sha256(original).hexdigest()
            parent = {'dep_type': 'git', 'repository': 'https://chromium.googlesource.com/devtools/devtools-frontend',
                      'revision': revision}
            baseline = {'chromium_revision': 'b' * 40, 'profile': {'name': 'linux-x64'},
                        'dependencies': {parent_name: parent},
                        'preserved_upstream_inputs': {preserved: {'revision': revision, 'original_path': 'DEPS', 'sha256': deps_hash}}}
            baseline_bytes = json.dumps(baseline).encode()
            destination = parent_name + '/third_party/esbuild'
            entry = {'dep_type': 'cipd', 'condition': 'non_git_source', 'selected_for_profile': True,
                     'parent_repository': parent_name, 'parent_deps_key': 'third_party/esbuild',
                     'packages': [{'package': 'infra/esbuild/${{platform}}', 'version': 'version:fixed',
                                   'resolved_package': 'infra/esbuild/linux-amd64',
                                   'instance': {'hashAlgo': 'SHA256', 'hexDigest': 'c' * 64}}]}
            nested = {'schema': 1, 'chromium_revision': baseline['chromium_revision'],
                      'root_lock_sha256': hashlib.sha256(baseline_bytes).hexdigest(), 'profile': 'linux-x64',
                      'hooks_executed': False, 'parents': {parent_name: {'repository': parent['repository'],
                          'revision': revision, 'deps_file': preserved, 'deps_sha256': deps_hash}},
                      'dependencies': {destination: entry}}
            nested_path = root / 'nested.json'
            nested_path.write_text(json.dumps(nested))
            with mock.patch.object(packages, 'ROOT', root):
                entries, provenance = packages.selected_packages(baseline, baseline_bytes, nested_path)
                self.assertEqual(entries[destination]['packages'][0]['instance']['hexDigest'], 'c' * 64)
                self.assertEqual(provenance['parents'][parent_name]['revision'], revision)
                self.assertEqual(baseline_bytes, json.dumps(baseline).encode())
                entry['packages'][0]['version'] = 'version:locally-changed'
                nested_path.write_text(json.dumps(nested))
                with self.assertRaisesRegex(ValueError, 'differs from DEPS'):
                    packages.selected_packages(baseline, baseline_bytes, nested_path)
                entry['packages'][0]['version'] = 'version:fixed'
                nested_path.write_text(json.dumps(nested))
                (root / preserved).write_bytes(original + b'# changed')
                with self.assertRaisesRegex(ValueError, 'DEPS bytes changed'):
                    packages.selected_packages(baseline, baseline_bytes, nested_path)
                (root / preserved).write_bytes(original)
                nested['parents'][parent_name]['revision'] = 'd' * 40
                nested_path.write_text(json.dumps(nested))
                with self.assertRaisesRegex(ValueError, 'Git pin differs'):
                    packages.selected_packages(baseline, baseline_bytes, nested_path)

    def test_cipd_instance_uses_locked_sha256(self):
        instance = {'hashAlgo': 'SHA256', 'hexDigest':
                    'c3d42a67ea0fdc6a2f03ece339ac18cdbe7271afade3b19b4fe7a33dad014266'}
        self.assertEqual(packages.cipd_instance_id(instance),
                         'w9QqZ-oP3GovA-zjOawYzb5yca-t47GbT-ejPa0BQmYC')
        with self.assertRaises(ValueError):
            packages.cipd_instance_id({'hashAlgo': 'SHA1', 'hexDigest': '00'})

    def test_named_gcs_archive_is_retained_and_unpacked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'archive'
            with tarfile.open(archive, 'w:gz') as tar:
                entry = tarfile.TarInfo('node-linux-x64/bin/node')
                entry.mode, entry.size = 0o755, 4
                tar.addfile(entry, io.BytesIO(b'node'))
            stage = root / 'stage'
            stage.mkdir()
            self.assertEqual(packages.unpack(archive, stage, 'node-linux-x64.tar.gz'), 'tar')
            self.assertEqual((stage / 'node-linux-x64.tar.gz').read_bytes(), archive.read_bytes())
            self.assertTrue((stage / 'node-linux-x64/bin/node').stat().st_mode & 0o111)

    def test_zip_retains_executable_mode_and_skips_cipd_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'archive'
            with zipfile.ZipFile(archive, 'w') as zipped:
                entry = zipfile.ZipInfo('ninja')
                entry.external_attr = (stat.S_IFREG | 0o500) << 16
                zipped.writestr(entry, b'ninja')
                zipped.writestr('.cipdpkg/manifest.json', '{}')
            stage = root / 'stage'
            stage.mkdir()
            packages.unpack_zip(archive, stage, cipd=True)
            self.assertEqual((stage / 'ninja').stat().st_mode & 0o777, 0o755)
            self.assertFalse((stage / '.cipdpkg').exists())

    def test_tar_explicit_root_directory_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, stage = root / 'archive', root / 'stage'
            stage.mkdir()
            with tarfile.open(archive, 'w') as tar:
                directory = tarfile.TarInfo('.')
                directory.type = tarfile.DIRTYPE
                tar.addfile(directory)
                entry = tarfile.TarInfo(r'./systemd/service\x2descape')
                entry.size = 1
                tar.addfile(entry, io.BytesIO(b'h'))
            packages.unpack(archive, stage)
            self.assertEqual((stage / r'systemd/service\x2descape').read_bytes(), b'h')

    def test_cipd_manifest_version_file_records_immutable_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, stage = root / 'archive', root / 'stage'
            stage.mkdir()
            package = {'resolved_package': 'infra/tool/linux-amd64',
                       'instance': {'hashAlgo': 'SHA256', 'hexDigest': '00' * 32}}
            with zipfile.ZipFile(archive, 'w') as zipped:
                zipped.writestr('.cipdpkg/manifest.json', json.dumps({
                    'package_name': package['resolved_package'],
                    'version_file': '.versions/tool.cipd_version'}))
            packages.write_cipd_version(archive, stage, package)
            version = json.loads((stage / '.versions/tool.cipd_version').read_text())
            self.assertEqual(version['instance_id'], packages.cipd_instance_id(package['instance']))
            self.assertEqual(version['package_name'], package['resolved_package'])

    def test_zip_and_tar_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'archive'
            stage = root / 'stage'
            stage.mkdir()
            with zipfile.ZipFile(archive, 'w') as zipped:
                zipped.writestr('../escaped', b'bad')
            with self.assertRaises(ValueError):
                packages.unpack(archive, stage)
            with tarfile.open(archive, 'w') as tar:
                entry = tarfile.TarInfo('../escaped')
                tar.addfile(entry)
            with self.assertRaises(ValueError):
                packages.unpack(archive, stage)
            self.assertFalse((root / 'escaped').exists())

    def test_install_is_idempotent_and_refuses_changed_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, destination = root / 'stage', root / 'destination'
            stage.mkdir()
            (stage / 'file').write_bytes(b'upstream')
            first = packages.install(stage, destination)
            self.assertEqual(first, packages.install(stage, destination))
            (destination / 'file').write_bytes(b'local change')
            with self.assertRaisesRegex(ValueError, 'differs'):
                packages.install(stage, destination)
            self.assertEqual((destination / 'file').read_bytes(), b'local change')

    def test_install_refuses_destination_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, destination, outside = root / 'stage', root / 'destination', root / 'outside'
            (stage / 'dir').mkdir(parents=True)
            (stage / 'dir/file').write_bytes(b'upstream')
            destination.mkdir()
            outside.mkdir()
            (destination / 'dir').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                packages.install(stage, destination)
            self.assertFalse((outside / 'file').exists())

    def test_install_refuses_destination_root_and_ancestor_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, outside, boundary = root / 'stage', root / 'outside', root / 'workspace'
            stage.mkdir()
            (stage / 'file').write_bytes(b'upstream')
            outside.mkdir()
            boundary.mkdir()
            (boundary / 'alias').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                packages.install(stage, boundary / 'alias', boundary)
            with self.assertRaises(ValueError):
                packages.install(stage, boundary / 'alias/nested', boundary)
            self.assertFalse((outside / 'file').exists())
            self.assertFalse((outside / 'nested').exists())

    def test_cached_download_checksum_is_always_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            digest = hashlib.sha256(b'original').hexdigest()
            (cache / digest).write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                packages.fetch(digest, lambda: 'unused', cache)

    def test_cached_raw_gcs_recovers_executable_metadata_with_immutable_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            data = b'raw executable'
            digest = hashlib.sha256(data).hexdigest()
            (work / 'packages').mkdir()
            (work / 'packages' / digest).write_bytes(data)
            selected = {'object_name': 'tool', 'generation': 42, 'sha256sum': digest,
                        'size_bytes': len(data), 'output_file': 'tool'}
            entry = {'dep_type': 'gcs', 'bucket': 'official',
                     'objects': [selected], 'selected_objects': [selected]}
            response = io.BytesIO()
            response.headers = {'X-Goog-Meta-Executable': '1'}
            with mock.patch.object(packages.urllib.request, 'urlopen', return_value=response) as request:
                packages.materialize('build/tool', entry, work)
            head = request.call_args.args[0]
            self.assertEqual(head.method, 'HEAD')
            self.assertTrue(head.full_url.endswith('?generation=42'))
            self.assertTrue((work / 'src/build/tool/tool').stat().st_mode & 0o111)
            self.assertTrue((work / 'packages' / (digest + '.headers.json')).exists())

    def test_gcs_materialization_only_uses_frozen_selected_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            data = b'raw executable'
            digest = hashlib.sha256(data).hexdigest()
            (work / 'packages').mkdir()
            (work / 'packages' / digest).write_bytes(data)
            (work / 'packages' / (digest + '.headers.json')).write_text(
                json.dumps({'x-goog-meta-executable': '1'}))
            selected = {'object_name': 'linux', 'generation': 42, 'sha256sum': digest,
                        'size_bytes': len(data), 'output_file': 'tool'}
            entry = {'dep_type': 'gcs', 'bucket': 'official',
                     'objects': [selected, {'object_name': 'mac'}], 'selected_objects': [selected]}
            _, report = packages.materialize('build/tool', entry, work)
            self.assertEqual(len(report['artifacts']), 1)
            self.assertEqual((work / 'src/build/tool/tool').read_bytes(), data)
            self.assertTrue((work / 'src/build/tool/tool').stat().st_mode & 0o111)


if __name__ == '__main__':
    unittest.main()
