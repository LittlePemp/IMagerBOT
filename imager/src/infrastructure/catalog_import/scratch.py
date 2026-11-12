"""Release this import's scratch page cache, never global OS caches."""
import errno
import os


def release_file_cache(stream, *, flush=False):
    if os.name == 'nt' or not hasattr(os, 'posix_fadvise'):
        return
    if flush:
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise


def release_path_cache(path, *, flush=False):
    with open(path, 'rb') as stream:
        release_file_cache(stream, flush=flush)
