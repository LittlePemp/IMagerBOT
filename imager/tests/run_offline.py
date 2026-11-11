"""Run core regression tests without user configuration, services or log files.

Usage: py -3.12 -I -B imager/tests/run_offline.py
Installed dependencies or this checkout's ignored .packages are supported.
"""
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import Mock


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root), str(root / '.packages')]
    os.environ['MONGODB_URI'] = 'mongodb://127.0.0.1:1'
    os.environ['DATABASE_NAME'] = 'offline_tests'
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    loggers = types.ModuleType('src.shared_kernel.loggers')
    for name in ('application_logger', 'exception_logger', 'db_logger', 'presentation_logger'):
        setattr(loggers, name, Mock())
    sys.modules['src.shared_kernel.loggers'] = loggers
    from src.shared_kernel.resource_limits import configure_decoder_limits
    configure_decoder_limits()
    import pytest
    # Relative test files are confined to a temporary working directory.
    previous = os.getcwd()
    with tempfile.TemporaryDirectory(prefix='imager-offline-') as directory:
        os.chdir(directory)
        try:
            return pytest.main([
                str(root / 'tests'), '-q', '-p', 'no:cacheprovider',
                '--ignore=' + str(root / 'tests' / 'custom_tests'),
                *sys.argv[1:],
            ])
        finally:
            os.chdir(previous)


if __name__ == '__main__':
    raise SystemExit(main())
