"""Bounded removal of stale scratch files; stdlib only, shared by core and bot.

Temporary directories are normally removed by ``TemporaryDirectory``. A killed
process (OOM, SIGKILL, host crash) skips that cleanup, and the scratch paths are
bind mounts that outlive containers, so leftovers must be swept explicitly.
"""
import os
import shutil
import stat
import threading
import time
from collections import Counter
from pathlib import Path

# Media sub-directories that telegram-bot-api (--local) creates per bot token.
# Its TDLib databases (td.binlog, db.sqlite, ...) live next to them and are
# never touched.
TELEGRAM_MEDIA_DIRECTORIES = frozenset({
    'photos', 'documents', 'stickers', 'thumbnails', 'videos', 'animations',
    'voice', 'video_notes', 'music', 'temp', 'profile_photos', 'wallpapers',
    'secret', 'secret_thumbnails', 'stories',
})


class _TelegramCacheCoordinator:
    """Coordinate the single bot process and its threaded cache janitor.

    A getFile call can resolve to a path already used by another request.
    Register it before the call so a completed transfer cannot remove that
    path before the new reader has had a chance to pin it.
    """
    def __init__(self):
        self.lock = threading.RLock()
        self.resolving = Counter()
        self.readers = Counter()
        self.deferred = set()

    def busy(self, root, target):
        return bool(self.resolving[root] or self.readers[(root, target)])

    def drain(self, root):
        errors = []
        for owner, target in tuple(self.deferred):
            if owner != root or self.busy(root, target):
                continue
            self.deferred.discard((owner, target))
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                # The bot logs only the type; a Bot API path contains a token.
                # The age-based janitor can retry the unchanged file later.
                errors.append(type(error).__name__)
        return errors


_TELEGRAM_CACHE = _TelegramCacheCoordinator()


class TelegramCacheTransfer:
    """Pin a getFile resolution and its eventual reader until transfer ends."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.target = None
        self.cleanup_errors = []
        self.active = False

    def __enter__(self):
        with _TELEGRAM_CACHE.lock:
            if self.active or self.target is not None:
                raise RuntimeError('Telegram cache transfer is already used')
            _TELEGRAM_CACHE.resolving[self.root] += 1
            self.active = True
        return self

    def bind(self, path):
        target = Path(path).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError('Telegram file is outside the local Bot API directory')
        with _TELEGRAM_CACHE.lock:
            if not self.active or self.target is not None:
                raise RuntimeError('Telegram cache transfer cannot bind another file')
            # Pin first, then remove the conservative unresolved-getFile pin.
            _TELEGRAM_CACHE.readers[(self.root, target)] += 1
            _TELEGRAM_CACHE.resolving[self.root] -= 1
            if not _TELEGRAM_CACHE.resolving[self.root]:
                del _TELEGRAM_CACHE.resolving[self.root]
            self.target = target
        return target

    def __exit__(self, *exception):
        with _TELEGRAM_CACHE.lock:
            if not self.active:
                return
            if self.target is None:
                _TELEGRAM_CACHE.resolving[self.root] -= 1
                if not _TELEGRAM_CACHE.resolving[self.root]:
                    del _TELEGRAM_CACHE.resolving[self.root]
            else:
                key = self.root, self.target
                _TELEGRAM_CACHE.readers[key] -= 1
                if not _TELEGRAM_CACHE.readers[key]:
                    del _TELEGRAM_CACHE.readers[key]
            self.active = False
            self.cleanup_errors.extend(_TELEGRAM_CACHE.drain(self.root))


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
    root = Path(root).resolve()
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
                    with _TELEGRAM_CACHE.lock:
                        if _TELEGRAM_CACHE.busy(root, path):
                            continue
                        try:
                            # Check age and type under the same guard as the
                            # unlink and registration of new getFile readers.
                            info = path.lstat()
                            if not stat.S_ISREG(info.st_mode) or now - info.st_mtime <= max_age_seconds:
                                continue
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
    with _TELEGRAM_CACHE.lock:
        if _TELEGRAM_CACHE.busy(root, target):
            _TELEGRAM_CACHE.deferred.add((root, target))
            return False
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        return True
