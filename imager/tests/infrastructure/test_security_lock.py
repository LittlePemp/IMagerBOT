import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CONVERTER_PATH = Path(__file__).resolve().parents[2] / 'security' / 'lock_from_report.py'
SPEC = importlib.util.spec_from_file_location('imager_security_lock_converter', CONVERTER_PATH)
CONVERTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONVERTER)


def distribution(name='Example_Pkg', version='1.2.3', *, wheel=None, digest='a' * 64):
    wheel = wheel or f'{name.replace("-", "_")}-{version}-py3-none-any.whl'
    return {
        'download_info': {
            'url': f'https://files.pythonhosted.org/packages/private/report-origin/{wheel}',
            'archive_info': {'hashes': {'sha256': digest}},
        },
        'is_direct': False, 'is_yanked': False, 'requested': True,
        'metadata': {'metadata_version': '2.4', 'name': name, 'version': version,
                     'requires_python': '>=3.12'},
    }


def report(target='linux-x86_64', installs=None):
    windows = target == 'windows-amd64'
    return {
        'version': '1', 'pip_version': '26.3',
        'install': [distribution()] if installs is None else installs,
        'environment': {
            'implementation_name': 'cpython', 'implementation_version': '3.12.12',
            'os_name': 'nt' if windows else 'posix',
            'platform_machine': 'AMD64' if windows else 'x86_64',
            'platform_release': '6.1', 'platform_system': 'Windows' if windows else 'Linux',
            'platform_version': 'synthetic', 'platform_python_implementation': 'CPython',
            'python_full_version': '3.12.12', 'python_version': '3.12',
            'sys_platform': 'win32' if windows else 'linux',
        },
    }


def pins(value):
    return [line for line in value.splitlines() if line and not line.startswith('#')]


class TestSecurityLock(unittest.TestCase):
    def test_exact_normalized_pins_are_sorted_hashed_and_portable(self):
        original = report(installs=[
            distribution('Zeta.Tool', '2.0', digest='b' * 64), distribution(),
        ])
        before = copy.deepcopy(original)
        value = CONVERTER.build_lock(original, 'linux-x86_64')
        self.assertEqual(pins(value), [
            'example-pkg==1.2.3 --hash=sha256:' + 'a' * 64,
            'zeta-tool==2.0 --hash=sha256:' + 'b' * 64,
        ])
        self.assertEqual(original, before)
        for unsafe in ('https://', 'file://', 'private/report-origin', 'C:\\', 'D:\\'):
            self.assertNotIn(unsafe, value)

    def test_native_linux_and_windows_wheels_produce_separate_hash_pins(self):
        for target, platform, digest in (
            ('linux-x86_64', 'manylinux_2_28_x86_64', 'a' * 64),
            ('windows-amd64', 'win_amd64', 'b' * 64),
        ):
            with self.subTest(target=target):
                item = distribution('native_pkg', wheel=f'native_pkg-1.2.3-cp312-cp312-{platform}.whl',
                                    digest=digest)
                value = CONVERTER.build_lock(report(target, [item]), target)
                self.assertEqual(pins(value), ['native-pkg==1.2.3 --hash=sha256:' + digest])
                self.assertIn(target, value)

    def test_older_stable_abi_wheels_are_valid_for_cpython_312(self):
        item = distribution(wheel='Example_Pkg-1.2.3-cp37-abi3-manylinux_2_17_x86_64.whl')
        self.assertEqual(len(pins(CONVERTER.build_lock(report(installs=[item]), 'linux-x86_64'))), 1)

    def test_dependency_markers_remain_resolver_responsibility(self):
        # The report format does not record --ignore-installed. Completeness
        # is attested at the CLI and proved by a clean hash-enforced install,
        # not by trying to reimplement dependency/marker resolution here.
        item = distribution()
        item['metadata']['requires_dist'] = ['win-only>=1; sys_platform == "win32"']
        self.assertEqual(len(pins(CONVERTER.build_lock(report(installs=[item]), 'linux-x86_64'))), 1)

    def test_missing_report_version_pip_version_or_install_tree_is_rejected(self):
        invalid = [None, [], {}, {**report(), 'version': '2'}, {**report(), 'install': []}]
        for missing in ('pip_version', 'environment', 'install'):
            value = report()
            del value[missing]
            invalid.append(value)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                CONVERTER.build_lock(value, 'linux-x86_64')

    def test_environment_and_wheel_must_match_the_requested_native_target(self):
        for field, value in (
            ('python_version', '3.11'), ('platform_machine', 'aarch64'),
            ('implementation_name', 'pypy'), ('platform_python_implementation', 'PyPy'),
            ('platform_system', 'Windows'), ('sys_platform', 'win32'), ('os_name', 'nt'),
        ):
            value_report = report()
            value_report['environment'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                CONVERTER.build_lock(value_report, 'linux-x86_64')
        with self.assertRaises(ValueError):
            CONVERTER.build_lock(report(), 'windows-amd64')
        with self.assertRaises(ValueError):
            CONVERTER.build_lock(report(), 'linux-aarch64')
        for wheel in (
            'Example_Pkg-1.2.3-cp311-cp311-manylinux_2_28_x86_64.whl',
            'Example_Pkg-1.2.3-cp313-cp313-manylinux_2_28_x86_64.whl',
            'Example_Pkg-1.2.3-cp312-cp312-manylinux_2_28_aarch64.whl',
            'Example_Pkg-1.2.3-cp312-cp312-win_amd64.whl',
            'Example_Pkg-1.2.3-cp312-cp312-macosx_14_0_x86_64.whl',
        ):
            with self.subTest(wheel=wheel), self.assertRaises(ValueError):
                CONVERTER.build_lock(report(installs=[distribution(wheel=wheel)]), 'linux-x86_64')

    def test_duplicate_names_cannot_bypass_detection_by_case_or_separators(self):
        for duplicate in ('EXAMPLE-PKG', 'example.pkg', 'example__pkg'):
            with self.subTest(name=duplicate), self.assertRaises(ValueError):
                CONVERTER.build_lock(report(installs=[distribution(), distribution(duplicate)]),
                                     'linux-x86_64')

    def test_unsafe_names_versions_and_hashes_cannot_inject_requirements(self):
        for key, values in (
            ('name', ('', '../pkg', 'pkg\nother==1', 'pkg --index-url=evil', 'pkg@https://evil')),
            ('version', ('', '../1', '1.0\nother==1', '1.0 --hash=sha256:bad', 'https://evil')),
        ):
            for value in values:
                item = distribution()
                item['metadata'][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    CONVERTER.build_lock(report(installs=[item]), 'linux-x86_64')
        for digest in ('', '0' * 63, 'g' * 64, 'a' * 64 + '\n--extra-index-url=https://evil', None):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                CONVERTER.build_lock(report(installs=[distribution(digest=digest)]), 'linux-x86_64')

    def test_every_distribution_requires_source_metadata_and_sha256(self):
        for missing in ('metadata', 'download_info', 'is_direct'):
            item = distribution()
            del item[missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                CONVERTER.build_lock(report(installs=[item]), 'linux-x86_64')
        item = distribution()
        del item['download_info']['archive_info']['hashes']['sha256']
        with self.assertRaises(ValueError):
            CONVERTER.build_lock(report(installs=[item]), 'linux-x86_64')

    def test_only_credential_free_https_wheel_urls_are_accepted(self):
        for url in (
            'file:///D:/private/Example_Pkg-1.2.3-py3-none-any.whl',
            'http://files.pythonhosted.org/Example_Pkg-1.2.3-py3-none-any.whl',
            'git+https://github.com/example/pkg.git',
            'https://user:secret@files.pythonhosted.org/Example_Pkg-1.2.3-py3-none-any.whl',
            'https://files.pythonhosted.org/Example_Pkg-1.2.3-py3-none-any.whl?token=secret',
            'https://files.pythonhosted.org/Example_Pkg-1.2.3-py3-none-any.whl#fragment',
            'https://files.pythonhosted.org/Example_Pkg-1.2.3.tar.gz',
            'https://files.pythonhosted.org/other_pkg-1.2.3-py3-none-any.whl',
            'https://files.pythonhosted.org/Example_Pkg-2.0-py3-none-any.whl',
        ):
            item = distribution()
            item['download_info']['url'] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                CONVERTER.build_lock(report(installs=[item]), 'linux-x86_64')

    def test_direct_editable_vcs_and_yanked_distributions_are_rejected(self):
        for field in ('is_direct', 'is_yanked'):
            item = distribution()
            item[field] = True
            with self.subTest(field=field), self.assertRaises(ValueError):
                CONVERTER.build_lock(report(installs=[item]), 'linux-x86_64')
        for field in ('dir_info', 'vcs_info'):
            item = distribution()
            item['download_info'][field] = {'editable': True}
            with self.subTest(field=field), self.assertRaises(ValueError):
                CONVERTER.build_lock(report(installs=[item]), 'linux-x86_64')

    def test_cli_requires_complete_plan_attestation_before_writing_lock(self):
        with tempfile.TemporaryDirectory(prefix='imager-lock-test-') as temporary:
            source, destination = Path(temporary) / 'report.json', Path(temporary) / 'lock.txt'
            source.write_text(json.dumps(report()), encoding='utf-8')
            command = [sys.executable, '-I', '-B', str(CONVERTER_PATH), '--target', 'linux-x86_64',
                       str(source), str(destination)]
            rejected = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(destination.exists())
            accepted = subprocess.run([*command, '--full-plan'], capture_output=True, text=True, timeout=15)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            value = destination.read_text(encoding='utf-8')
            self.assertEqual(pins(value), ['example-pkg==1.2.3 --hash=sha256:' + 'a' * 64])
            self.assertNotIn(str(source), value)
            self.assertNotIn(str(destination), value)
