"""Bounded removal of stale scratch files; stdlib only, shared by core and bot.

Temporary directories are normally removed by ``TemporaryDirectory``. A killed
process (OOM, SIGKILL, host crash) skips that cleanup, and the scratch paths are
bind mounts that outlive containers, so leftovers must be swept explicitly.
"""
import os
import shutil
import time
from pathlib import Path

# Media sub-directories that telegram-bot-api (--local) creates per bot token.
# Its TDLib databases (td.binlog, db.sqlite, ...) live next to them and are
# never touched.
TELEGRAM_MEDIA_DIRECTORIES = frozenset({
    'photos', 'documents', 'stickers', 'thumbnails', 'videos', 'animations',
    'voice', 'video_notes', 'music', 'temp', 'profile_photos', 'wallpapers',
    'secret', 'secret_thumbnails', 'stories',
})


def _age(path, now):
    try:
        return now - path.lstat().st_mtime
    except FileNotFoundError:
        return -1


def purge_stale_entries(root, prefixes, max_age_seconds, *, now=None):
    """Remove direct children of ``root`` named with one of ``prefixes``.

    Only entries older than ``max_age_seconds`` are removed. Symlinks are
    unlinked, never followed. Returns the number of removed entries.
    """
    root = Path(root)
    if not root.is_dir() or not prefixes:
        return 0
    now = time.time() if now is None else now
    removed = 0
    for entry in root.iterdir():
        if not entry.name.startswith(tuple(prefixes)) or _age(entry, now) <= max_age_seconds:
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except FileNotFoundError:
            continue
    return removed


def purge_telegram_cache(root, max_age_seconds, *, now=None):
    """Remove old downloaded media from a telegram-bot-api --local work dir.

    Layout: ``<root>/<bot token>/<media dir>/<file>``. Only regular files in
    known media directories are removed; databases and unknown files stay.
    Returns (files removed, bytes released).
    """
    root = Path(root)
    if not root.is_dir():
        return 0, 0
    now = time.time() if now is None else now
    files = released = 0
    for bot_dir in root.iterdir():
        if not bot_dir.is_dir() or bot_dir.is_symlink():
            continue
        for media_dir in bot_dir.iterdir():
            if (media_dir.name not in TELEGRAM_MEDIA_DIRECTORIES
                    or not media_dir.is_dir() or media_dir.is_symlink()):
                continue
            for current, directories, names in os.walk(media_dir, followlinks=False):
                for name in names:
                    path = Path(current) / name
                    try:
                        info = path.lstat()
                    except FileNotFoundError:
                        continue
                    if not os.path.isfile(path) or path.is_symlink() or now - info.st_mtime <= max_age_seconds:
                        continue
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                    files += 1
                    released += info.st_size
    return files, released


def release_telegram_file(root, path):
    """Delete one file the bot has finished transferring from the local cache.

    The path must resolve inside ``root`` and inside a known media directory.
    Missing files are fine. Returns True when a file was removed.
    """
    root = Path(root).resolve()
    target = Path(path).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Telegram file is outside the local Bot API directory')
    parts = target.relative_to(root).parts
    if len(parts) < 3 or parts[1] not in TELEGRAM_MEDIA_DIRECTORIES:
        raise ValueError('Telegram file is not a downloaded media file')
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True
