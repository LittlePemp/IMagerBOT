"""Opt-in isolated renderer benchmark; this file is not collected by pytest.

Run with the project's Python version and existing bundled dependencies::

    py -3.12 -I -B imager/tests/performance/benchmark_render.py
    py -3.12 -I -B imager/tests/performance/benchmark_render.py --grid 200 --cell-size 32
    py -3.12 -I -B imager/tests/performance/benchmark_render.py --legacy

Uses synthetic cached originals, a real KD-tree and real temporary input/output
files. No MongoDB, broker, bot, .env, network or application file logs are used.
The optional legacy comparison renders the same source with noise=0; it reads
the original renderer from the V2 Git baseline and compares pixels before encoding.
JSON measurements describe one synthetic run, not a general performance bound.
"""

import argparse
import ast
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types


def _arguments():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--grid', type=int, default=120)
    parser.add_argument('--cell-size', type=int, default=40)
    parser.add_argument('--noise', type=int, default=15)
    parser.add_argument('--cache-bytes', type=int, default=256_000_000)
    parser.add_argument('--legacy', action='store_true')
    parser.add_argument('--legacy-ref', default='58e639e',
                        help='Git revision of the original renderer (default: V2 baseline)')
    args = parser.parse_args()
    if not 1 <= args.grid <= 500 or not 1 <= args.cell_size <= 2000:
        parser.error('grid must be 1..500; cell-size must be 1..2000')
    if args.grid ** 2 * args.cell_size ** 2 > 48_000_000:
        parser.error('the benchmark must fit the core 48,000,000 pixel budget')
    if not 0 <= args.noise <= 100:
        parser.error('noise must be 0..100')
    if not 144 * 144 * 3 <= args.cache_bytes <= 256_000_000:
        parser.error('cache-bytes must fit one original and be at most 256,000,000')
    return args


def _class_from_source(source, name, context):
    parsed = ast.parse(source)
    parsed.body = [
        node for node in parsed.body
        if isinstance(node, ast.ClassDef) and node.name == name
    ]
    exec(compile(parsed, '<legacy-render-benchmark>', 'exec'), context)
    return context[name]


def main():
    args = _arguments()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit('Use Python 3.12: py -3.12 -I -B <this script>')
    core = Path(__file__).resolve().parents[2]
    project = core.parent
    sys.path[:0] = [str(core), str(core / '.packages')]

    from src.shared_kernel.resource_limits import (
        configure_decoder_limits, current_rss_bytes,
    )

    configure_decoder_limits()
    import cv2
    import numpy as np
    from scipy.spatial import KDTree
    from src.shared_kernel.result import Result

    def deny_network(*_args, **_kwargs):
        raise RuntimeError('Network is disabled in the renderer benchmark')

    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.create_connection = deny_network

    errors = []
    logger = types.SimpleNamespace(
        **{level: (lambda *_args, **_kwargs: None)
           for level in ('info', 'debug', 'warning')},
        error=lambda message, *_args, **_kwargs: errors.append(str(message)),
        exception=lambda message, *_args, **_kwargs: errors.append(str(message)),
    )
    sys.modules['src.shared_kernel.loggers'] = types.SimpleNamespace(
        application_logger=logger, db_logger=logger,
        exception_logger=logger, presentation_logger=logger,
    )
    with tempfile.TemporaryDirectory(prefix='imager-render-benchmark-') as directory:
        work = Path(directory).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        assert os.path.commonpath([str(work), str(temp_root)]) == str(temp_root)
        config = types.SimpleNamespace(
            file_path_prefix='', image_groups_relative_path=str(work / 'groups'),
            generated_images_path=str(work / 'generated'),
            min_size=(1, 1), max_size=(4000, 4000),
            aspect_ratio_limits=(9 / 16, 16 / 9),
            generated_image_extension='jpg', generated_image_jpeg_quality=90,
            telegram_max_document_size_bytes=47_185_920,
        )
        sys.modules['settings'] = types.SimpleNamespace(settings=config)
        from src.application.image_builder.services.imager_builder import ImagerBuilder
        from src.application.image_builder.errors.services_errors import ServicesErrorMessages
        from src.domain.image_builder.errors.image_service_errors import ImageServiceErrorMessages
        from src.infrastructure.data.image_builder.repositories.file_repository import FileRepository

        peak = [current_rss_bytes()]
        stop = threading.Event()

        def monitor():
            while not stop.is_set():
                current = current_rss_bytes()
                peak[0] = max(peak[0], current)
                if current > 970_000_000:
                    print(json.dumps({
                        'error': 'benchmark_memory_watchdog', 'rss_bytes': current,
                    }), flush=True)
                    os._exit(17)
                stop.wait(0.002)

        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        try:
            rng = np.random.default_rng(44)
            backing = np.empty(args.cache_bytes, dtype=np.uint8)
            backing.fill(121)  # Touch all cache pages, not only virtual memory.
            original_bytes = 144 * 144 * 3
            count = min(4096, args.cache_bytes // original_bytes)
            originals = backing[:count * original_bytes].reshape(count, 144, 144, 3)
            means = rng.integers(0, 256, size=(count, 3), dtype=np.uint8)
            originals[:] = means[:, None, None, :]
            metadata = [
                types.SimpleNamespace(
                    cell=types.SimpleNamespace(relative_file_path=f'fake-{i}.jpg'),
                    image=originals[i],
                ) for i in range(count)
            ]
            tree = KDTree(means.astype(float))

            class Catalog:
                def find_closest_metadata(self, colors, _group):
                    _, indices = tree.query(colors)
                    return Result.Success([metadata[int(i)] for i in indices])

                def get_cell_image(self, item):
                    return Result.Success(item.image)

                def find_closest_cell(self, color, _group):
                    _, index = tree.query(color)
                    return metadata[int(index)]

            catalog = Catalog()
            source = rng.integers(
                0, 256, size=(args.grid, args.grid, 3), dtype=np.uint8,
            )
            source_path = work / 'input.png'
            if not cv2.imwrite(str(source_path), source):
                raise RuntimeError('Could not write the synthetic source')
            builder = ImagerBuilder(
                FileRepository(), catalog, 'crop', args.grid, args.cell_size,
                0.3, args.noise, seed=42,
            )
            before = current_rss_bytes()
            started = time.perf_counter()
            result = builder.make_image(str(source_path), 'fake', 'benchmark')
            seconds = time.perf_counter() - started
            peak[0] = max(peak[0], current_rss_bytes())
            report = {
                'grid': args.grid, 'cell_size': args.cell_size, 'noise': args.noise,
                'catalog_images': count, 'original_cache_bytes': backing.nbytes,
                'rss_before_bytes': before, 'peak_rss_bytes': peak[0],
                'seconds': round(seconds, 4), 'success': result.is_success,
                'error': str(result.error) if result.error else None,
            }
            if result:
                report['output_bytes'] = (work / 'generated' / result.value).stat().st_size
            if args.legacy:
                def head_file(relative):
                    return subprocess.check_output(
                        ['git', '-C', str(project), 'show', f'{args.legacy_ref}:{relative}'],
                        text=True, encoding='utf-8', timeout=10,
                    )

                old_service = _class_from_source(
                    head_file('imager/src/domain/image_builder/services/image_service.py'),
                    'ImageService', {
                        'np': np, 'cv2': cv2, 'KDTree': KDTree, 'Result': Result,
                        'ImageServiceErrorMessages': ImageServiceErrorMessages,
                    },
                )
                from random import randint
                old_type = _class_from_source(
                    head_file('imager/src/application/image_builder/services/imager_builder.py'),
                    'ImagerBuilder', {
                        'np': np, 'cv2': cv2, 'Result': Result,
                        'ImageService': old_service,
                        'ServicesErrorMessages': ServicesErrorMessages,
                        'FileRepository': object, 'CellRepository': object,
                        'randint': randint, 'settings': config, 'os': os,
                    },
                )
                old = old_type(
                    FileRepository(), catalog, 'crop', args.grid, args.cell_size, 0.3, 0,
                )
                new = ImagerBuilder(
                    FileRepository(), catalog, 'crop', args.grid, args.cell_size,
                    0.3, 0, seed=42,
                )
                started = time.perf_counter()
                new_pixels = new.create_result_image(source, args.grid, args.grid, 'fake')
                new_seconds = time.perf_counter() - started
                started = time.perf_counter()
                old_pixels = old.create_result_image(source, args.grid, args.grid, 'fake')
                old_seconds = time.perf_counter() - started
                report['legacy_comparison'] = {
                    'noise': 0,
                    'new_calculation_seconds': round(new_seconds, 4),
                    'legacy_calculation_seconds': round(old_seconds, 4),
                    'speedup': round(old_seconds / new_seconds, 2),
                    'pixels_equal': bool(new_pixels and old_pixels and np.array_equal(
                        new_pixels.value, old_pixels.value,
                    )),
                    'peak_including_comparison_bytes': peak[0],
                }
            if errors:
                report['logged_errors'] = errors
        finally:
            stop.set()
            watcher.join()
    print(json.dumps(report), flush=True)
    return 0 if result.is_success else 1


if __name__ == '__main__':
    raise SystemExit(main())
