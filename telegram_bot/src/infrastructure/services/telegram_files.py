"""Constrain Local Bot API reads to its shared cache and release transferred files."""
from pathlib import Path

from imager_config.loader import config
from imager_contracts.scratch import release_telegram_file
from src.utils.loggers import exception_logger


def _is_local(bot):
    api = getattr(getattr(bot, 'session', None), 'api', None)
    return bool(getattr(api, 'is_local', False))


def validate_download_path(bot, remote):
    if not isinstance(remote.file_path, str) or not remote.file_path:
        raise ValueError('Telegram ещё не подготовил файл для скачивания. Повторите позже.')
    if _is_local(bot):
        root = Path(config.pack_import.bot_api_container_directory).resolve()
        if not Path(remote.file_path).resolve().is_relative_to(root):
            raise ValueError('Telegram вернул недопустимый путь к файлу.')


def release_local_file(bot, file_path):
    """Best-effort removal of a file already copied out of the Local Bot API cache.

    A failure only leaves the file for the periodic janitor; it never fails the
    user's request, whose bytes are already stored in MinIO.
    """
    if not _is_local(bot) or not isinstance(file_path, str) or not file_path:
        return False
    try:
        return release_telegram_file(config.pack_import.bot_api_container_directory, file_path)
    except (OSError, ValueError):
        exception_logger.exception('Cannot release Local Bot API file; the janitor will remove it')
        return False
