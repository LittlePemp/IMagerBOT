"""Offline Telegram-adapter tests; never connects to Telegram/Mongo/MinIO."""
import os
import sys
import tempfile
import types
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def main():
    root = Path(__file__).resolve().parents[1]
    repository = root.parent
    scratch = Path(
        os.environ.get('IMAGER_TEST_TEMP')
        or os.environ.get('TEMP')
        or os.environ.get('TMP')
        or repository / '.local' / 'tmp'
    )
    scratch.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(scratch)
    os.environ['TEMP'] = os.environ['TMP'] = str(scratch)
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    sys.path[:0] = [
        str(root), str(repository), str(root / '.packages'),
        str(repository / 'imager' / '.packages'),
    ]
    configuration = types.ModuleType('settings')
    configuration.settings = SimpleNamespace(
        tzinfo=timezone.utc,
        mongodb_uri='mongodb://127.0.0.1:1',
        imager_database_name='offline',
        generation_default_result_size=120, generation_default_cell_size=40,
        generation_default_noise_level=15, generation_default_alpha_channel=30,
        generation_default_insertion_format='crop',
        generation_result_size_min=1, generation_result_size_max=300,
        generation_cell_size_min=1, generation_cell_size_max=200,
        generation_noise_level_min=0, generation_noise_level_max=100,
        generation_estimated_jpeg_bytes_per_pixel=1.0,
        telegram_max_document_size_bytes=47185920,
        regular_user_upload_limit=5,
    )
    sys.modules['settings'] = configuration
    loggers = types.ModuleType('src.utils.loggers')
    for name in ('service_logger', 'exception_logger', 'db_logger', 'bot_requests_logger'):
        setattr(loggers, name, Mock())
    sys.modules['src.utils.loggers'] = loggers
    import pytest
    previous = os.getcwd()
    with tempfile.TemporaryDirectory(prefix='imager-bot-tests-', dir=scratch) as directory:
        os.chdir(directory)
        try:
            return pytest.main([
                str(root / 'tests'), '-q', '-p', 'no:cacheprovider', *sys.argv[1:],
            ])
        finally:
            os.chdir(previous)


if __name__ == '__main__':
    raise SystemExit(main())
