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
    extra_dependencies = os.environ.get('IMAGER_TEST_DEPS')
    sys.path[:0] = [
        str(root), str(repository),
        *([extra_dependencies] if extra_dependencies else [
            str(root / '.packages'), str(repository / 'imager' / '.packages'),
        ]),
    ]
    from imager_config.loader import config
    configuration = types.ModuleType('settings')
    configuration.settings = SimpleNamespace(
        telegram_admin_ids=(999,),
        telegram_api_local=False,
        telegram_api_base_url=config.bot.api_base_url,
        tzinfo=timezone.utc,
        mongodb_uri='mongodb://127.0.0.1:1',
        imager_database_name='offline',
        generation_default_result_size=config.generation.result_size,
        generation_default_cell_size=config.generation.cell_size,
        generation_default_noise_level=config.generation.noise_level,
        generation_default_alpha_channel=config.generation.alpha_channel,
        generation_default_insertion_format=config.generation.insertion_format,
        generation_result_size_min=config.bot.result_size_min,
        generation_result_size_max=config.bot.result_size_max,
        generation_cell_size_min=config.bot.cell_size_min,
        generation_cell_size_max=config.bot.cell_size_max,
        generation_noise_level_min=config.bot.noise_min,
        generation_noise_level_max=config.bot.noise_max,
        generation_estimated_jpeg_bytes_per_pixel=config.render.estimated_jpeg_bytes_per_pixel,
        telegram_max_document_size_bytes=config.render.max_output_bytes,
        regular_user_upload_limit=config.bot.regular_user_upload_limit,
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
