"""Human-readable download names independent of internal generation identities."""
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from imager_config.loader import config
from imager_contracts.protocol import OUTPUT_EXTENSIONS, OUTPUT_NAME_MAX_LENGTH


_UUID = re.compile(r'(?i)(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f]))')


def contains_generation_reference(value, *, job_id=None):
    if not isinstance(value, str):
        return False
    return bool(_UUID.search(value) or (job_id and (
        str(job_id).casefold() in value.casefold()
        or str(job_id).replace('-', '').casefold() in value.casefold()
    )))


def new_result_filename(nickname=None, *, created_at=None):
    supplied = nickname if isinstance(nickname, str) and nickname else config.render.output_stem
    stem = re.sub(r'[^\w-]+', '_', supplied).strip('_-')
    stem = stem[:config.render.output_stem_max_length] or config.render.output_stem
    if contains_generation_reference(stem):
        stem = config.render.output_stem
    moment = created_at if isinstance(created_at, datetime) else datetime.now(timezone.utc)
    moment = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
    length = config.render.output_random_suffix_length
    return config.render.output_filename_template.format(
        stem=stem,
        timestamp=moment.strftime(config.render.output_timestamp_format),
        suffix=secrets.token_hex((length + 1) // 2)[:length],
        extension=config.render.output_extension,
    )


def delivery_result_filename(display_name, *, created_at=None, job_id=None):
    if (isinstance(display_name, str) and 0 < len(display_name) <= OUTPUT_NAME_MAX_LENGTH
            and re.fullmatch(r'[\w-][\w.-]*', display_name)
            and not contains_generation_reference(display_name, job_id=job_id)):
        extension = Path(display_name).suffix.lower().lstrip('.')
        stem = display_name[:-(len(extension) + 1)] if extension else display_name
        if (extension in OUTPUT_EXTENSIONS
                and not any(stem.lower().endswith('.' + value) for value in OUTPUT_EXTENSIONS)):
            return display_name
    return new_result_filename(created_at=created_at)
