"""Convert a complete native pip installation report into a wheel hash lock.

Generate the input using --dry-run --ignore-installed --only-binary=:all:.
The report is not itself a lock; the output is a pip requirements file. Install
it into a clean environment with --require-hashes, then run pip check to verify
the complete dependency tree. No third-party modules or network access needed.
"""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


_NAME = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z')
_VERSION = re.compile(r'[0-9][A-Za-z0-9.!+_-]*\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_TARGETS = {
    'linux-x86_64': ('linux', 'Linux', 'posix', 'x86_64'),
    'windows-amd64': ('win32', 'Windows', 'nt', 'amd64'),
}


def _normalized_name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError('Distribution name must be a valid package identifier')
    return re.sub(r'[-_.]+', '-', value).lower()


def _wheel_matches(url, name, version, target):
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        return False
    filename = unquote(parsed.path.rsplit('/', 1)[-1])
    if not filename.endswith('.whl'):
        return False
    try:
        distribution, python_tag, abi_tag, platform_tag = filename[:-4].rsplit('-', 3)
        prefix = distribution.split('-')
        if (len(prefix) not in (2, 3) or _normalized_name(prefix[0]) != name
                or prefix[1] != version):
            return False
    except ValueError:
        return False
    python_tags, abi_tags = python_tag.split('.'), abi_tag.split('.')
    python_ok = 'py3' in python_tags or 'cp312' in python_tags
    if 'abi3' in abi_tags:
        python_ok |= any(
            tag.startswith('cp3') and tag[3:].isdigit() and int(tag[3:]) <= 12
            for tag in python_tags
        )
    abi_ok = bool({'none', 'abi3', 'cp312'} & set(abi_tags))
    platform_ok = any(
        tag == 'any'
        or (target == 'windows-amd64' and tag == 'win_amd64')
        or (target == 'linux-x86_64' and (
            tag == 'linux_x86_64' or tag.startswith('manylinux') and tag.endswith('_x86_64')
        ))
        for tag in platform_tag.split('.')
    )
    return python_ok and abi_ok and platform_ok


def build_lock(report, target):
    """Validate every supplied distribution and return portable pinned text."""
    if target not in _TARGETS:
        raise ValueError('Unsupported lock target')
    if not isinstance(report, dict) or report.get('version') != '1':
        raise ValueError('A pip installation report with version 1 is required')
    environment = report.get('environment')
    if not isinstance(environment, dict):
        raise ValueError('Report must identify its resolver environment')
    expected = _TARGETS[target]
    environment_fields = (
        'sys_platform', 'platform_system', 'os_name', 'platform_machine',
    )
    if any(not isinstance(environment.get(field), str) for field in environment_fields):
        raise ValueError('Invalid resolver environment fields')
    actual = tuple(environment[field].lower() for field in environment_fields)
    if (actual != tuple(value.lower() for value in expected)
            or environment.get('python_version') != '3.12'
            or environment.get('implementation_name') != 'cpython'
            or environment.get('platform_python_implementation') != 'CPython'):
        raise ValueError('Report environment does not match the CPython 3.12 lock target')
    for field in ('python_full_version', 'implementation_version'):
        value = environment.get(field)
        if not isinstance(value, str) or not value.startswith('3.12.') or not _VERSION.fullmatch(value):
            raise ValueError('Resolver full versions must match CPython 3.12')
    pip_version = report.get('pip_version')
    if not isinstance(pip_version, str) or not _VERSION.fullmatch(pip_version):
        raise ValueError('Report must identify the resolver pip version')
    installs = report.get('install')
    if not isinstance(installs, list) or not installs:
        raise ValueError('Report must include the entire nonempty install plan')
    pins = {}
    for item in installs:
        if not isinstance(item, dict):
            raise ValueError('Invalid distribution entry')
        metadata, download = item.get('metadata'), item.get('download_info')
        if not isinstance(metadata, dict) or not isinstance(download, dict):
            raise ValueError('Distribution metadata and download information are required')
        name = _normalized_name(metadata.get('name'))
        version = metadata.get('version')
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise ValueError('Distribution version must be an exact safe version')
        if (item.get('is_direct') is not False or item.get('is_yanked', False)
                or 'vcs_info' in download or 'dir_info' in download):
            raise ValueError('Direct, editable, VCS and yanked distributions are not supported')
        archive = download.get('archive_info')
        hashes = archive.get('hashes') if isinstance(archive, dict) else None
        digest = hashes.get('sha256') if isinstance(hashes, dict) else None
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError('Every distribution must have an exact SHA256 wheel hash')
        if not _wheel_matches(download.get('url'), name, version, target):
            raise ValueError('Distribution must use a matching CPython 3.12 target wheel')
        if name in pins:
            raise ValueError('Report contains duplicate normalized distribution names')
        pins[name] = f'{name}=={version} --hash=sha256:{digest}'
    header = [
        '# Target: CPython 3.12 / ' + target,
        '# Generated from a full native pip --dry-run --ignore-installed --only-binary=:all: report.',
        '# Resolver pip: ' + pip_version,
        '# Converter: imager/security/lock_from_report.py; refresh instructions: imager/security/README.md',
        '# Install with --require-hashes --only-binary=:all:. Do not edit by hand.',
        '',
    ]
    return '\n'.join([*header, *(pins[name] for name in sorted(pins)), ''])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, choices=_TARGETS)
    parser.add_argument('--full-plan', required=True, action='store_true',
                        help='Confirm the report was generated with --ignore-installed')
    parser.add_argument('report', type=Path)
    parser.add_argument('output', type=Path)
    arguments = parser.parse_args()
    report = json.loads(arguments.report.read_text(encoding='utf-8-sig'))
    arguments.output.write_text(build_lock(report, arguments.target), encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
