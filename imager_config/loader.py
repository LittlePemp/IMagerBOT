"""Load shared TOML once per process, before starting infrastructure adapters.

IMAGER_CONFIG_FILE may point at a partial TOML override. Secret credentials are
still provided through the existing environment-based Settings classes.
Workflow code imports only immutable models and receives a policy snapshot.
"""
import copy
import math
import os
import re
import string
import tomllib
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import get_args, get_origin, get_type_hints
from uuid import UUID

from .models import RuntimeConfig, WORKER_MEMORY_CEILING_BYTES


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / 'config' / 'imager.toml'
CONFIG_OVERRIDE_VARIABLE = 'IMAGER_CONFIG_FILE'


class ConfigurationError(ValueError):
    pass


def _merge(base, override, path=''):
    result = copy.deepcopy(base)
    for key, value in override.items():
        location = f'{path}.{key}'.strip('.')
        if key not in base:
            raise ConfigurationError(f'Unknown configuration key: {location}')
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ConfigurationError(f'Expected a table: {location}')
            result[key] = _merge(base[key], value, location)
        else:
            result[key] = value
    return result


def _model(model_type, values, path=''):
    expected = get_type_hints(model_type)
    if set(values) != set(expected):
        raise ConfigurationError(f'Unexpected or missing configuration fields in {path or "root"}')
    parsed = {}
    for field in fields(model_type):
        value, annotation = values[field.name], expected[field.name]
        location = f'{path}.{field.name}'.strip('.')
        if is_dataclass(annotation):
            if not isinstance(value, dict):
                raise ConfigurationError(f'Expected a table: {location}')
            value = _model(annotation, value, location)
        elif get_origin(annotation) is tuple:
            item_type = get_args(annotation)[0]
            if not isinstance(value, list) or any(type(item) is not item_type for item in value):
                raise ConfigurationError(f'Invalid list: {location}')
            value = tuple(value)
        elif annotation is float and type(value) in (int, float):
            value = float(value)
        elif type(value) is not annotation:
            raise ConfigurationError(f'Invalid type: {location}')
        parsed[field.name] = value
    return model_type(**parsed)


def _basename(value, name):
    if (not value or value != value.strip() or value in ('.', '..')
            or value.endswith('.') or PureWindowsPath(value).is_reserved()
            or any(char in value for char in '<>:"/\\|?*')
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ConfigurationError(f'{name} must be a plain filename')


def _relative_path(value, name):
    normalized = value.replace('\\', '/')
    if (normalized.startswith('/') or PureWindowsPath(value).drive
            or any(part in ('', '.', '..') for part in normalized.split('/'))):
        raise ConfigurationError(f'{name} must be a normalized relative path')
    for part in normalized.split('/'):
        _basename(part, name)


def _plain_template(template, required, name, samples):
    try:
        placeholders = []
        for _, field, format_spec, conversion in string.Formatter().parse(template):
            if field is not None:
                if format_spec or conversion is not None:
                    raise ValueError('Only plain placeholders are supported')
                placeholders.append(field)
        if set(placeholders) != required or len(placeholders) != len(required):
            raise ValueError('Incorrect placeholders')
        return template.format(**samples)
    except (ValueError, KeyError, AttributeError, IndexError) as error:
        raise ConfigurationError(f'Invalid placeholders in {name}') from error


def _key_template(template, required, name, samples, max_length):
    sample = _plain_template(template, required, f'storage.{name}', samples)
    if (len(sample) > max_length or '\\' in sample
            or any(part in ('', '.', '..') for part in sample.split('/'))):
        raise ConfigurationError(f'storage.{name} must produce a normalized relative object key')


def _validate(config):
    from imager_contracts.protocol import (
        BUCKET_PATTERN, COLOR_CHANNELS, GROUP_DIGEST_LENGTH, INSERTION_FORMATS,
        JOB_ID_MAX_LENGTH, JPEG_EXTENSIONS, OBJECT_KEY_MAX_LENGTH, OUTPUT_EXTENSIONS,
        PERCENT_MAX,
    )

    zero_allowed = {
        'bot.noise_min', 'bot.noise_max', 'generation.alpha_channel', 'generation.noise_level',
        'limits.original_cache_bytes', 'limits.prepared_cache_bytes',
        'storage.retry_attempts', 'storage.retry_backoff_seconds', 'legacy.rabbitmq_max_retries',
        'temporal.worker.cached_workflows', 'logging.retained_archives',
    }

    def numbers(model, path=''):
        for field in fields(model):
            value = getattr(model, field.name)
            name = f'{path}.{field.name}'.strip('.')
            if is_dataclass(value):
                numbers(value, name)
            elif type(value) in (int, float):
                if ((type(value) is float and not math.isfinite(value))
                        or value < 0 or (value == 0 and name not in zero_allowed)):
                    raise ConfigurationError(f'{name} must be finite and positive')
                if (name.endswith('_seconds') and value != 0
                        and not timedelta.resolution.total_seconds() <= value <= timedelta.max.total_seconds()):
                    raise ConfigurationError(f'{name} is outside supported duration bounds')
            elif isinstance(value, str):
                if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
                    raise ConfigurationError(f'{name} must be nonempty text without control characters')

    numbers(config)
    limits, image = config.limits, config.image
    # The one-gigabyte ceiling is a core invariant; configuration can lower it.
    if limits.memory_bytes > WORKER_MEMORY_CEILING_BYTES:
        raise ConfigurationError('limits.memory_bytes exceeds the core worker ceiling')
    if limits.original_cache_bytes + limits.prepared_cache_bytes + limits.memory_headroom_bytes >= limits.memory_bytes:
        raise ConfigurationError('Image caches leave no rendering memory')
    for name in ('decode_reserve_bytes', 'manifest_reserve_bytes',
                 'catalog_check_reserve_bytes', 'encode_reserve_bytes'):
        if getattr(config.memory, name) + limits.memory_headroom_bytes >= limits.memory_bytes:
            raise ConfigurationError(f'memory.{name} leaves no working memory')
    if config.storage.transfer_chunk_bytes > limits.memory_headroom_bytes:
        raise ConfigurationError('storage.transfer_chunk_bytes exceeds reserved memory headroom')
    if config.memory.render_bytes_per_pixel < 2 * COLOR_CHANNELS:
        raise ConfigurationError('memory.render_bytes_per_pixel must cover mosaic and overlay pixels')
    if not (image.min_width <= image.max_width <= limits.decoder_width
            and image.min_height <= image.max_height <= limits.decoder_height
            and image.aspect_ratio_min <= image.aspect_ratio_max):
        raise ConfigurationError('Image bounds exceed decoder limits or are inverted')
    if image.min_width * image.min_height > limits.source_pixels:
        raise ConfigurationError('Image minimum dimensions exceed the decoder pixel budget')
    if (not image.allowed_color_modes or len(set(image.allowed_color_modes)) != len(image.allowed_color_modes)
            or any(not mode.strip() or any(ord(char) < 32 for char in mode)
                   for mode in image.allowed_color_modes)):
        raise ConfigurationError('image.allowed_color_modes must be a nonempty set of modes')
    if not 1 <= config.render.jpeg_quality <= PERCENT_MAX:
        raise ConfigurationError('render.jpeg_quality must fit the JPEG codec range')
    if config.render.output_extension not in OUTPUT_EXTENSIONS:
        raise ConfigurationError('render.output_extension is not a supported encoder format')
    _basename(config.render.output_stem, 'render.output_stem')
    if config.render.output_random_suffix_length > len(UUID(int=0).hex):
        raise ConfigurationError('render.output_random_suffix_length exceeds UUID hex length')
    # The timestamp is part of a filename, never a user-controlled directory.
    try:
        timestamp = datetime(2000, 12, 31, 23, 59, 59, 999999).strftime(config.render.output_timestamp_format)
    except ValueError as error:
        raise ConfigurationError('render.output_timestamp_format is invalid') from error
    _basename(timestamp, 'render.output_timestamp_format')
    filename = _plain_template(
        config.render.output_filename_template, {'stem', 'timestamp', 'suffix', 'extension'},
        'render.output_filename_template',
        {'stem': 's' * config.render.output_stem_max_length, 'timestamp': timestamp,
         'suffix': 'a' * config.render.output_random_suffix_length,
         'extension': max(OUTPUT_EXTENSIONS, key=len)},
    )
    _basename(filename, 'render.output_filename_template')
    # A portable filesystem component must fit the common 255-byte ceiling.
    if len(filename.encode('utf-8')) > 255:
        raise ConfigurationError('render.output_filename_template produces an oversized filename')

    bot, generation = config.bot, config.generation
    if generation.insertion_format not in INSERTION_FORMATS:
        raise ConfigurationError('generation.insertion_format is unsupported')
    if not 0 <= generation.alpha_channel <= PERCENT_MAX:
        raise ConfigurationError('generation.alpha_channel must fit the percentage range')
    if not 0 <= bot.noise_min <= generation.noise_level <= bot.noise_max <= PERCENT_MAX:
        raise ConfigurationError('Generation noise default must fit bot percentage limits')
    for name in ('cell_size', 'result_size'):
        minimum, maximum = getattr(bot, f'{name}_min'), getattr(bot, f'{name}_max')
        if not minimum <= getattr(generation, name) <= maximum <= getattr(limits, name):
            raise ConfigurationError(f'Generation {name} default and bot range must fit core limits')
    if (generation.cell_size * generation.result_size) ** 2 > limits.output_pixels:
        raise ConfigurationError('Default generation exceeds the output pixel budget')
    if generation.result_size ** 2 > limits.cells:
        raise ConfigurationError('Default generation exceeds the mosaic cell budget')
    if config.temporal.worker.activity_slots != 1:
        raise ConfigurationError('Each worker container must have exactly one Activity slot')
    if not (config.jobs.heartbeat_interval_seconds < config.temporal.render.heartbeat_seconds
            < config.jobs.execution_lease_seconds):
        raise ConfigurationError('Heartbeat intervals must fit inside the execution lease')
    if config.jobs.heartbeat_renew_divisor <= 1:
        raise ConfigurationError('jobs.heartbeat_renew_divisor must renew before heartbeat expiry')
    if config.jobs.claim_wait_seconds >= config.temporal.render.heartbeat_seconds:
        raise ConfigurationError('Execution claim wait must fit inside the Activity heartbeat timeout')
    if config.mongo.socket_timeout_ms / 1000 >= config.jobs.heartbeat_join_seconds:
        raise ConfigurationError('Mongo socket timeout must fit inside heartbeat shutdown')
    if (config.jobs.heartbeat_interval_seconds
            + max(config.mongo.socket_timeout_ms, config.mongo.server_selection_timeout_ms) / 1000
            >= config.temporal.render.heartbeat_seconds):
        raise ConfigurationError('Mongo heartbeat I/O must fit inside the Activity heartbeat timeout')
    for name in ('render', 'failure'):
        policy = getattr(config.temporal, name)
        if not (policy.start_to_close_seconds <= policy.schedule_to_close_seconds
                and policy.retry_initial_seconds <= policy.retry_maximum_seconds
                <= policy.schedule_to_close_seconds):
            raise ConfigurationError(f'temporal.{name} retry and timeout bounds are inconsistent')
    if config.temporal.render.heartbeat_seconds >= config.temporal.render.start_to_close_seconds:
        raise ConfigurationError('Activity heartbeat timeout must fit inside its execution timeout')
    if config.bot.notification_renew_divisor <= 1:
        raise ConfigurationError('Notification lease renewal must happen before lease expiry')
    if max(config.bot.notification_renew_min_seconds,
           config.bot.notification_lease_seconds / config.bot.notification_renew_divisor
           ) >= config.bot.notification_lease_seconds:
        raise ConfigurationError('Notification renewal interval must fit inside the lease')
    if (bot.notification_retry_backoff_coefficient < 1
            or bot.notification_retry_initial_seconds > bot.notification_retry_max_seconds):
        raise ConfigurationError('Notification retry backoff bounds are inconsistent')
    if bot.error_excerpt_length > bot.error_max_length:
        raise ConfigurationError('Bot error excerpt exceeds retained error length')
    for name in ('source_filename', 'upload_filename', 'cell_filename', 'manifest_filename', 'result_filename'):
        _basename(getattr(config.storage, name), f'storage.{name}')
    for field in fields(config.storage):
        if field.name.endswith('_temp_prefix'):
            _basename(getattr(config.storage, field.name), f'storage.{field.name}')
    if Path(config.storage.result_filename).suffix.lower() not in JPEG_EXTENSIONS:
        raise ConfigurationError('storage.result_filename must match the JPEG result encoder')
    if Path(config.storage.upload_filename).suffix.lower() not in JPEG_EXTENSIONS:
        raise ConfigurationError('storage.upload_filename must match Telegram photo encoding')
    if not re.fullmatch(BUCKET_PATTERN, config.storage.bucket):
        raise ConfigurationError('storage.bucket is not a valid object bucket')
    if (not config.catalog.allowed_extensions
            or len(set(config.catalog.allowed_extensions)) != len(config.catalog.allowed_extensions)
            or any(not re.fullmatch(r'\.[a-z0-9]+', extension)
                   for extension in config.catalog.allowed_extensions)):
        raise ConfigurationError('catalog.allowed_extensions must be unique lowercase dotted extensions')
    for name in ('ui_credentials', 'ui_password_hash', 'readable_credentials',
                 'mongo_directory', 'minio_directory', 'temporal_directory',
                 'worker_scratch_directory', 'bot_scratch_directory', 'client_temp_directory',
                 'rpc_journal_directory'):
        _basename(getattr(config.paths, name), f'paths.{name}')
    for name in ('image_groups', 'uploads', 'generated', 'compose_env', 'legacy_bot_env'):
        _relative_path(getattr(config.paths, name), f'paths.{name}')
    for name in ('data_root', 'secrets_root'):
        value = getattr(config.paths, name)
        normalized = value.replace('\\', '/')
        if any(part in ('.', '..') for part in normalized.split('/')):
            raise ConfigurationError(f'paths.{name} must not contain traversal components')
    if not 1 <= config.deployment.temporal_ui_port <= 65535 or not 1 <= config.deployment.minio_console_port <= 65535:
        raise ConfigurationError('Deployment ports must fit the TCP port range')
    if config.deployment.temporal_ui_port == config.deployment.minio_console_port:
        raise ConfigurationError('Deployment UI ports must be distinct')
    templates = {
        'source_key_template': {'job_id', 'sha256'},
        'result_key_template': {'job_id', 'execution_token', 'result_filename'},
        'catalog_image_key_template': {'sha256', 'extension'},
        'catalog_manifest_key_template': {'group_id', 'sha256'},
    }
    samples = {
        'job_id': 'j' * JOB_ID_MAX_LENGTH, 'execution_token': 't' * JOB_ID_MAX_LENGTH,
        'result_filename': config.storage.result_filename, 'sha256': 'a' * 64,
        'group_id': 'a' * GROUP_DIGEST_LENGTH, 'extension': '.jpeg',
    }
    for name, required in templates.items():
        _key_template(getattr(config.storage, name), required, name, samples, OBJECT_KEY_MAX_LENGTH)
    return config


def load_config(override_path=None):
    try:
        with DEFAULT_CONFIG_PATH.open('rb') as stream:
            values = tomllib.load(stream)
        selected = override_path or os.environ.get(CONFIG_OVERRIDE_VARIABLE)
        if selected:
            with Path(selected).open('rb') as stream:
                values = _merge(values, tomllib.load(stream))
        return _validate(_model(RuntimeConfig, values))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f'Cannot load IMager TOML configuration: {error}') from error


config = load_config()
