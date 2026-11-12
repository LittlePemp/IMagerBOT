"""Process-wide budgets for one renderer and its original-image cache.

These are safety ceilings, not the administrator's selectable generation values.
"""
import ctypes
import os
import threading
from contextlib import contextmanager

from imager_config.loader import config


MEMORY_LIMIT_BYTES = config.limits.memory_bytes
MEMORY_HEADROOM_BYTES = config.limits.memory_headroom_bytes
CACHE_LIMIT_BYTES = config.limits.original_cache_bytes
PREPARED_CACHE_LIMIT_BYTES = config.limits.prepared_cache_bytes
MAX_SOURCE_FILE_BYTES = config.limits.source_file_bytes
MAX_SOURCE_PIXELS = config.limits.source_pixels
MAX_OUTPUT_PIXELS = config.limits.output_pixels
MAX_CELLS = config.limits.cells


def configure_decoder_limits():
    """Call before importing OpenCV so oversized inputs fail before decoding."""
    for name, ceiling in (
        ('OPENCV_IO_MAX_IMAGE_WIDTH', config.limits.decoder_width),
        ('OPENCV_IO_MAX_IMAGE_HEIGHT', config.limits.decoder_height),
        ('OPENCV_IO_MAX_IMAGE_PIXELS', MAX_SOURCE_PIXELS),
    ):
        try:
            value = min(int(os.environ.get(name, ceiling)), ceiling)
        except ValueError:
            value = ceiling
        os.environ[name] = str(max(1, value))


def current_rss_bytes():
    """Resident process memory, including native NumPy/OpenCV allocations."""
    if os.name == 'nt':
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb,
        ):
            raise OSError(ctypes.get_last_error(), 'Cannot read process memory usage')
        return int(counters.WorkingSetSize)
    try:
        with open('/proc/self/statm', encoding='ascii') as stream:
            resident = int(stream.read().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        # In the dedicated bounded worker include page cache/tmpfs as well.
        # Do not count an unbounded host/parent cgroup as this application's RAM.
        for limit_path, usage_path in (
            ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory.current'),
            ('/sys/fs/cgroup/memory/memory.limit_in_bytes',
             '/sys/fs/cgroup/memory/memory.usage_in_bytes'),
        ):
            try:
                with open(limit_path, encoding='ascii') as stream:
                    limit = int(stream.read().strip())
                if 0 < limit <= MEMORY_LIMIT_BYTES:
                    with open(usage_path, encoding='ascii') as stream:
                        resident = max(resident, int(stream.read().strip()))
                    break
            except (OSError, ValueError):
                continue
        return resident
    except (OSError, IndexError, ValueError):
        import resource
        import sys
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == 'darwin' else peak * 1024)


def require_memory(additional_bytes=0):
    used = current_rss_bytes()
    if used + additional_bytes + MEMORY_HEADROOM_BYTES > MEMORY_LIMIT_BYTES:
        raise MemoryError(
            f'Render memory budget exceeded: resident={used}, '
            f'additional={additional_bytes}, limit={MEMORY_LIMIT_BYTES}'
        )
    return used


_generation_lock = threading.RLock()


@contextmanager
def generation_slot():
    """One image at a time within a core process, for all entry points."""
    with _generation_lock:
        yield
