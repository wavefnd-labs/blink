import copy
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('compiler_cache',
    Path(__file__).resolve().parents[1] / 'tools/compiler_cache.py')
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


class CompilerCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        patch = mock.patch.object(cache, 'WORK', self.directory / 'cache')
        patch.start()
        self.addCleanup(patch.stop)

    def fixture(self):
        config = copy.deepcopy(cache.load())
        files = {'ccache': b'fixture binary', 'LICENSE.md': b'upstream license',
                 'GPL-3.0.txt': b'upstream GPL'}
        config['files'] = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        archive = cache.WORK / 'downloads' / Path(config['archive_url']).name
        archive.parent.mkdir(parents=True)
        with tarfile.open(archive, 'w:xz') as tar:
            for name, data in files.items():
                member = tarfile.TarInfo(config['archive_root'] + '/' + name)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
        config['archive_size'] = archive.stat().st_size
        config['archive_sha256'] = cache.digest(archive)
        return config, files, archive

    def test_install_checks_archive_members_and_rechecks_existing_binary(self):
        config, files, _ = self.fixture()
        tool = cache.install(config)
        for name, data in files.items():
            self.assertEqual((tool / name).read_bytes(), data)
        self.assertTrue((tool / 'ccache').stat().st_mode & 0o111)
        (tool / 'ccache').write_bytes(b'changed local executable')
        with self.assertRaisesRegex(ValueError, 'installation changed'):
            cache.install(config)

    def test_rejects_archive_or_member_checksum_change(self):
        config, _, archive = self.fixture()
        bad = copy.deepcopy(config)
        bad['files']['ccache'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'member checksum'):
            cache.install(bad)
        self.assertFalse((cache.WORK / 'tools' / ('ccache-' + config['version'])).exists())
        archive.write_bytes(b'changed download')
        with self.assertRaisesRegex(ValueError, 'archive differs'):
            cache.install(config)

    def test_rejects_cache_symlink_and_shared_config(self):
        cache.WORK.mkdir()
        external = self.directory / 'external'
        external.mkdir()
        (cache.WORK / 'data').symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            cache.managed_directory(cache.WORK / 'data')
        original = external / 'source'
        original.write_text('pristine')
        alias = cache.WORK / 'ccache.conf'
        alias.hardlink_to(original)
        with self.assertRaisesRegex(ValueError, 'Shared'):
            cache.atomic_bytes(alias, b'new')
        self.assertEqual(original.read_text(), 'pristine')

    def test_config_is_private_and_cannot_relax_header_or_time_checks(self):
        config = cache.load()
        rendered = cache.configuration(config, cache.WORK / 'data').decode()
        self.assertIn('hard_link = false\n', rendered)
        self.assertIn('compiler_check = content\n', rendered)
        self.assertIn('direct_mode = true\n', rendered)
        self.assertIn('depend_mode = true\n', rendered)
        self.assertEqual(config['settings']['sloppiness'], 'modules')
        self.assertNotIn('base_dir =', rendered)
        self.assertNotIn('ignore_headers_in_manifest', rendered)
        with self.assertRaisesRegex(ValueError, 'configuration value'):
            cache.configuration(config, Path('cache\nsloppiness = time_macros'))

    def test_environment_ignores_global_cache_overrides(self):
        prepared = {'config': '/controlled/cache.conf'}
        base = {'PATH': '/tools', 'CCACHE_SLOPPINESS': 'time_macros,system_headers',
                'CCACHE_PREFIX': 'unknown-wrapper', 'CCACHE_DIR': '/other',
                'CCACHE_CONFIGPATH': '/global'}
        self.assertEqual(cache.environment(base, prepared),
                         {'PATH': '/tools', 'CCACHE_CONFIGPATH': '/controlled/cache.conf'})
        self.assertEqual(base['CCACHE_DIR'], '/other')

    def test_prepare_preserves_data_and_restores_only_generated_configuration(self):
        config, _, _ = self.fixture()
        data = cache.WORK / 'data'
        data.mkdir()
        entry = data / 'existing-cache-entry'
        entry.write_bytes(b'preserved cache')
        with mock.patch.object(cache, 'load', return_value=config):
            metadata = cache.prepare()
            Path(metadata['config']).write_text('sloppiness = unsafe-local-change\n')
            again = cache.prepare()
        self.assertEqual(entry.read_bytes(), b'preserved cache')
        self.assertEqual(again, metadata)
        self.assertEqual(cache.digest(metadata['config']), metadata['settings_sha256'])

    def test_paths_cannot_escape_workspace(self):
        for path in [self.directory / 'outside', cache.WORK / '..' / 'outside']:
            with self.assertRaisesRegex(ValueError, 'outside'):
                cache.managed_directory(path)

    def test_wrapper_tracks_system_headers_without_changing_other_arguments(self):
        args = ['clang++', '-MMD', '-MF', 'file with spaces.d', '-c', 'source.cc', '-o', 'output.o']
        self.assertEqual(cache.wrapper_command(args, {'binary': '/pinned/ccache'}),
                         ['/pinned/ccache', 'clang++', '-MD', '-MF', 'file with spaces.d',
                          '-c', 'source.cc', '-o', 'output.o'])
        self.assertEqual(args[1], '-MMD')

    def test_implicit_modules_and_unreviewed_response_files_bypass_cache(self):
        for args in [['clang++', '-fmodules', '-c', 'source.cc'],
                     ['clang++', '-fprebuilt-implicit-modules', '-c', 'source.cc'],
                     ['clang++', '@unexpanded.rsp']]:
            self.assertEqual(cache.wrapper_command(args, {'binary': '/pinned/ccache'}), args)
        explicit = ['clang++', '-fmodules', '-fno-implicit-module-maps',
                    '-fno-implicit-modules', '-MMD', '-c', 'source.cc']
        self.assertEqual(cache.wrapper_command(explicit, {'binary': '/pinned/ccache'})[0], '/pinned/ccache')


if __name__ == '__main__':
    unittest.main()
