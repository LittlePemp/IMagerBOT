"""Constrain Local Bot API reads to its explicitly shared, read-only cache."""
from pathlib import Path

from imager_config.loader import config


def validate_download_path(bot, remote):
    if not isinstance(remote.file_path, str) or not remote.file_path:
        raise ValueError('Telegram ещё не подготовил файл для скачивания. Повторите позже.')
    api = getattr(getattr(bot, 'session', None), 'api', None)
    if getattr(api, 'is_local', False):
        root = Path(config.pack_import.bot_api_container_directory).resolve()
        if not Path(remote.file_path).resolve().is_relative_to(root):
            raise ValueError('Telegram вернул недопустимый путь к файлу.')
