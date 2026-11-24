"""Ownership checks shared by upload, retention and the administrative reader."""
import re

from .protocol import JOB_ID_PATTERN, SHA256_PATTERN


def _source_pattern(template, *, job_id=None, sha256=None):
    pattern = re.escape(template)
    job_placeholder = re.escape('{job_id}')
    if job_id is None:
        parts = pattern.split(job_placeholder)
        pattern = parts[0] + f'(?P<job_id>{JOB_ID_PATTERN.strip("^$")})' + r'(?P=job_id)'.join(parts[1:])
    else:
        pattern = pattern.replace(job_placeholder, re.escape(job_id))
    return (pattern.replace(re.escape('{sha256}'), re.escape(sha256) if sha256 is not None else SHA256_PATTERN.strip('^$'))
            .replace(re.escape('{upload_token}'), '[0-9a-f]{32}'))


def is_owned_source_key(key, job_id, sha256, storage):
    if (not isinstance(key, str) or not isinstance(job_id, str) or not isinstance(sha256, str)
            or not re.fullmatch(JOB_ID_PATTERN, job_id) or not re.fullmatch(SHA256_PATTERN, sha256)):
        return False
    templates = (storage.source_key_template, storage.source_attempt_key_template)
    if any(re.fullmatch(_source_pattern(template, job_id=job_id, sha256=sha256), key) for template in templates):
        return True
    return key in {f'uploads/{job_id}/{name}' for name in (storage.source_filename, storage.upload_filename)}


def source_job_id(key, storage):
    if not isinstance(key, str):
        return None
    for template in (storage.source_attempt_key_template, storage.source_key_template):
        match = re.fullmatch(_source_pattern(template), key)
        if match:
            return match['job_id']
    for name in (storage.source_filename, storage.upload_filename):
        match = re.fullmatch(f'uploads/(?P<job_id>{JOB_ID_PATTERN.strip("^$")})/' + re.escape(name), key)
        if match:
            return match['job_id']
    return None
