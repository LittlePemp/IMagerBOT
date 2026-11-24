"""Validate ZIP metadata before creating files and bound every extracted byte."""
import stat
import struct
import unicodedata
import zipfile
import zlib
from pathlib import Path

from imager_config.loader import config
from .errors import InvalidCatalogArchive


_EOCD = struct.Struct('<4s4H2LH')
_EOCD_SIGNATURE = b'PK\x05\x06'
_MAX_ZIP_COMMENT = 65535
_ALLOWED_METHODS = frozenset((zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED))

SKIPPED_SAMPLE_LIMIT = 20
_SIGNATURES = (
    (b'\x89PNG\r\n\x1a\n', '.png'), (b'\xff\xd8\xff', '.jpg'), (b'BM', '.bmp'),
    (b'II*\x00', '.tif'), (b'MM\x00*', '.tif'),
)


def sniff_image_extension(path):
    """Extension implied by a file's magic bytes, or None for non-images."""
    with Path(path).open('rb') as stream:
        head = stream.read(16)
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return '.webp'
    return next((extension for signature, extension in _SIGNATURES if head.startswith(signature)), None)


def _skip(skipped, name, reason):
    skipped['count'] += 1
    if len(skipped['samples']) < SKIPPED_SAMPLE_LIMIT:
        skipped['samples'].append(f'{name[:120]}: {reason}')


def _directory_entry_count(path, policy):
    # ZipFile allocates one Python ZipInfo per central-directory record. Bound
    # the count from EOCD before allowing that allocation, including ZIP bombs
    # consisting of hundreds of thousands of zero-byte names.
    size = path.stat().st_size
    with path.open('rb') as stream:
        stream.seek(max(0, size - _MAX_ZIP_COMMENT - _EOCD.size))
        tail = stream.read()
    offset = tail.rfind(_EOCD_SIGNATURE)
    if offset < 0 or len(tail) - offset < _EOCD.size:
        raise InvalidCatalogArchive('ZIP повреждён: отсутствует центральный каталог.')
    _, disk, start_disk, disk_count, count, central_size, central_offset, comment = _EOCD.unpack_from(tail, offset)
    if offset + _EOCD.size + comment != len(tail):
        raise InvalidCatalogArchive('ZIP содержит некорректные завершающие данные.')
    if disk or start_disk or disk_count != count:
        raise InvalidCatalogArchive('Многотомные ZIP не поддерживаются.')
    if count > policy.max_entries or count == 0xffff:
        raise InvalidCatalogArchive(f'В архиве допускается не более {policy.max_entries} записей.')
    if central_size > policy.max_central_directory_bytes:
        raise InvalidCatalogArchive('Каталог ZIP превышает допустимый размер.')
    if central_offset + central_size != size - _EOCD.size - comment:
        raise InvalidCatalogArchive('ZIP содержит некорректный центральный каталог.')
    # EOCD's count is attacker-controlled. ZipFile follows central_size instead,
    # so independently count every bounded central record before construction.
    with path.open('rb') as stream:
        stream.seek(central_offset)
        end, actual = central_offset + central_size, 0
        while stream.tell() < end:
            header = stream.read(46)
            if len(header) != 46 or header[:4] != b'PK\x01\x02':
                raise InvalidCatalogArchive('ZIP содержит повреждённую запись каталога.')
            name_size, extra_size, comment_size, disk_number = struct.unpack_from('<4H', header, 28)
            actual += 1
            if actual > policy.max_entries or name_size > policy.max_path_bytes or disk_number:
                raise InvalidCatalogArchive('Каталог ZIP превышает допустимые пределы.')
            target = stream.tell() + name_size + extra_size + comment_size
            if not name_size or target > end:
                raise InvalidCatalogArchive('ZIP содержит повреждённые поля каталога.')
            stream.seek(target)
        if actual != count:
            raise InvalidCatalogArchive('Количество записей ZIP не соответствует его каталогу.')
    return count


def _safe_name(info, policy):
    name = info.orig_filename
    if (not name or name != info.filename or '\\' in name or name.startswith('/')
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise InvalidCatalogArchive('ZIP содержит небезопасное имя файла.')
    try:
        encoded = name.encode('utf-8')
    except UnicodeError as error:
        raise InvalidCatalogArchive('ZIP содержит некорректное имя Unicode.') from error
    if len(encoded) > policy.max_path_bytes:
        raise InvalidCatalogArchive('Путь внутри ZIP слишком длинный.')
    parts = name.rstrip('/').split('/')
    if len(parts) > policy.max_path_depth:
        raise InvalidCatalogArchive('Слишком большая вложенность каталогов ZIP.')
    if any(not part or part in ('.', '..') or ':' in part for part in parts):
        raise InvalidCatalogArchive('ZIP содержит абсолютный путь или переход между каталогами.')
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    allowed_kind = stat.S_IFDIR if info.is_dir() else stat.S_IFREG
    if kind not in (0, allowed_kind):
        raise InvalidCatalogArchive('Ссылки и специальные файлы в ZIP запрещены.')
    return '/'.join(unicodedata.normalize('NFC', part).casefold() for part in parts), parts


def _extract_member(path, archive, info, target, policy, expanded):
    # ZipExtFile intentionally truncates output to its declared file_size.
    # A forged small size/CRC could otherwise hide additional inflated bytes.
    # Use ZipFile only to validate local headers/overlaps, and count the entire
    # supported compression stream ourselves with bounded zlib max_length.
    with archive.open(info):
        pass
    written, checksum = 0, 0
    decompressor = zlib.decompressobj(-zlib.MAX_WBITS) if info.compress_type == zipfile.ZIP_DEFLATED else None

    def emit(data):
        nonlocal written, checksum, expanded
        written += len(data)
        expanded += len(data)
        if (written > info.file_size or written > policy.max_file_bytes
                or expanded > policy.max_expanded_bytes
                or written > max(info.compress_size, 1) * policy.max_compression_ratio):
            raise InvalidCatalogArchive('Фактический размер ZIP превышает допустимый предел.')
        checksum = zlib.crc32(data, checksum)
        target.write(data)

    with path.open('rb') as raw:
        raw.seek(info.header_offset)
        header = raw.read(30)
        if len(header) != 30 or header[:4] != b'PK\x03\x04':
            raise InvalidCatalogArchive('ZIP содержит повреждённый заголовок файла.')
        name_size, extra_size = struct.unpack_from('<2H', header, 26)
        raw.seek(name_size + extra_size, 1)
        remaining = info.compress_size
        while remaining:
            compressed = raw.read(min(policy.extract_chunk_bytes, remaining))
            if not compressed:
                raise InvalidCatalogArchive('Сжатый файл ZIP обрезан.')
            remaining -= len(compressed)
            if decompressor is None:
                emit(compressed)
                continue
            while compressed:
                data = decompressor.decompress(compressed, policy.extract_chunk_bytes)
                emit(data)
                compressed = decompressor.unconsumed_tail
                if decompressor.unused_data or (decompressor.eof and remaining):
                    raise InvalidCatalogArchive('В сжатом файле ZIP найдены лишние данные.')
        if decompressor is not None:
            # zlib may consume the last input byte while max_length still
            # leaves buffered output/end bits. Drain without an unbounded flush.
            while not decompressor.eof:
                data = decompressor.decompress(b'', policy.extract_chunk_bytes)
                emit(data)
                if decompressor.unused_data or (not data and not decompressor.eof):
                    raise InvalidCatalogArchive('Поток сжатия ZIP не завершён.')
    if written != info.file_size or checksum != info.CRC:
        raise InvalidCatalogArchive('Размер или контрольная сумма файла ZIP не совпадает.')
    return expanded


def extract_catalog_archive(path, directory, *, policy=None, progress=None):
    """Flatten validated image members to generated filenames in a fresh dir.

    Original archive paths are never used as destination filesystem paths.
    Multiple safe source directories and an outer wrapper directory work alike.
    """
    policy = config.pack_import if policy is None else policy
    path, directory = Path(path), Path(directory)
    if not 0 < path.stat().st_size <= policy.max_archive_bytes:
        raise InvalidCatalogArchive('ZIP превышает допустимый размер.')
    declared_count = _directory_entry_count(path, policy)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError('Archive extraction requires an empty private directory')
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) != declared_count or len(entries) > policy.max_entries:
                raise InvalidCatalogArchive('Количество записей ZIP не соответствует его каталогу.')
            planned, names, files, declared_total = [], set(), set(), 0
            skipped = {'count': 0, 'samples': []}
            for info in entries:
                canonical, parts = _safe_name(info, policy)
                if canonical in names:
                    raise InvalidCatalogArchive('ZIP содержит повторяющиеся имена файлов.')
                if any('/'.join(canonical.split('/')[:index]) in files
                       for index in range(1, len(parts))):
                    raise InvalidCatalogArchive('Пути файлов и каталогов ZIP конфликтуют.')
                names.add(canonical)
                if info.flag_bits & 1:
                    raise InvalidCatalogArchive('Зашифрованные ZIP не поддерживаются.')
                if info.compress_type not in policy.allowed_compression_methods or info.compress_type not in _ALLOWED_METHODS:
                    raise InvalidCatalogArchive('Метод сжатия ZIP не поддерживается.')
                if info.is_dir():
                    if info.file_size:
                        raise InvalidCatalogArchive('Каталог ZIP не должен содержать данные.')
                    continue
                if any(name.startswith(canonical + '/') for name in names if name != canonical):
                    raise InvalidCatalogArchive('Пути файлов и каталогов ZIP конфликтуют.')
                files.add(canonical)
                if info.file_size < 0 or info.compress_size < 0:
                    raise InvalidCatalogArchive('ZIP содержит некорректные размеры.')
                if info.file_size > policy.max_file_bytes:
                    raise InvalidCatalogArchive('Один из файлов ZIP превышает допустимый размер.')
                declared_total += info.file_size
                if declared_total > policy.max_expanded_bytes:
                    raise InvalidCatalogArchive('Распакованный ZIP превышает допустимый размер.')
                if info.file_size > max(info.compress_size, 1) * policy.max_compression_ratio:
                    raise InvalidCatalogArchive('Слишком высокая степень сжатия ZIP.')
                # Common operating-system metadata is never extracted.
                if any(part in policy.ignored_directories for part in parts) or parts[-1] in policy.ignored_files or parts[-1].startswith('._'):
                    continue
                # One stray file (readme, video, empty entry) must not reject a
                # whole pack: it is skipped and reported. Entries without an
                # extension are identified by their signature after extraction.
                extension = Path(parts[-1]).suffix.lower()
                if not info.file_size:
                    _skip(skipped, info.filename, 'пустой файл')
                    continue
                if extension not in config.catalog.allowed_extensions:
                    if extension:
                        _skip(skipped, info.filename, 'неподдерживаемый тип файла')
                        continue
                    extension = None
                planned.append((info, extension))
            if not planned:
                raise InvalidCatalogArchive('В ZIP не найдены изображения.')
            expanded, files = 0, 0
            from src.infrastructure.catalog_import.scratch import release_file_cache
            for index, (info, extension) in enumerate(planned):
                if progress:
                    progress({'phase': 'extracting', 'processed': index, 'total': len(planned)})
                destination = directory / f'{index:08d}{extension or ".unknown"}'
                with destination.open('xb') as target:
                    expanded = _extract_member(path, archive, info, target, policy, expanded)
                    release_file_cache(target, flush=True)
                if extension is None:
                    sniffed = sniff_image_extension(destination)
                    if sniffed is None or sniffed not in config.catalog.allowed_extensions:
                        destination.unlink()
                        _skip(skipped, info.filename, 'файл без расширения не является изображением')
                        continue
                    destination.rename(destination.with_suffix(sniffed))
                files += 1
            if progress:
                progress({'phase': 'extracting', 'processed': len(planned), 'total': len(planned)})
            if not files:
                raise InvalidCatalogArchive('В ZIP не найдены изображения.')
            return {'files': files, 'expanded_bytes': expanded,
                    'skipped': skipped['count'], 'skipped_samples': skipped['samples'],
                    # Extracted files are named by planned index; keep the originals for reports.
                    'origins': [info.filename for info, _ in planned]}
    except InvalidCatalogArchive:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError, UnicodeError, zlib.error) as error:
        raise InvalidCatalogArchive('ZIP повреждён или использует неподдерживаемый формат.') from error
