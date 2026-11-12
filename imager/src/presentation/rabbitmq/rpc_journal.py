"""Bounded response journal for retrying an RPC after its reply queue disappeared.

This stores completed responses, not running jobs. A crash after writing an image
but before journaling its response can still cause a repeated calculation.
"""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from imager_config.loader import config
from imager_contracts.protocol import JSON_EXTENSION, TEMPORARY_FILE_SUFFIX
from src.shared_kernel.loggers import presentation_logger


class RpcJournal:
    def __init__(self, artifact_root: str | Path, ttl_seconds: float | None = None, max_entries: int | None = None):
        self.artifact_root = Path(artifact_root).resolve()
        self.directory = self.artifact_root / config.paths.rpc_journal_directory
        self.ttl_seconds = config.legacy.rpc_result_ttl_seconds if ttl_seconds is None else ttl_seconds
        self.max_entries = config.legacy.rpc_result_max_entries if max_entries is None else max_entries

    @staticmethod
    def fingerprint(action: str, payload: dict) -> str:
        encoded = json.dumps({'action': action, 'payload': payload}, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest()

    def _path(self, request_id: str) -> Path:
        return self.directory / (hashlib.sha256(request_id.encode('utf-8')).hexdigest() + JSON_EXTENSION)

    def get(self, request_id: str, fingerprint: str) -> dict | None:
        path = self._path(request_id)
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            presentation_logger.exception(f'Cannot read RPC response journal: request_id={request_id}')
            return None
        if time.time() - record.get('created_at', 0) > self.ttl_seconds:
            path.unlink(missing_ok=True)
            return None
        if record.get('fingerprint') != fingerprint:
            raise ValueError('request_id was already used with different parameters')
        response = record.get('response')
        if not isinstance(response, dict):
            return None
        if response.get('status') == 'ok':
            name = response.get('data', {}).get('path')
            if not isinstance(name, str):
                return None
            artifact = (self.artifact_root / name).resolve()
            if not artifact.is_relative_to(self.artifact_root) or not artifact.is_file():
                path.unlink(missing_ok=True)
                return None
        return response

    def store(self, request_id: str, fingerprint: str, response: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {
            'request_id': request_id,
            'fingerprint': fingerprint,
            'response': response,
            'created_at': time.time(),
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=config.storage.rpc_temp_prefix, suffix=TEMPORARY_FILE_SUFFIX, dir=self.directory,
        )
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                json.dump(record, target, separators=(',', ':'))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self._path(request_id))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.prune()

    def prune(self) -> None:
        now = time.time()
        entries = []
        for path in self.directory.glob(f'*{JSON_EXTENSION}'):
            try:
                modified = path.stat().st_mtime
                if now - modified > self.ttl_seconds:
                    path.unlink(missing_ok=True)
                else:
                    entries.append((modified, path))
            except FileNotFoundError:
                continue
        for _, path in sorted(entries)[:max(0, len(entries) - self.max_entries)]:
            path.unlink(missing_ok=True)
        for path in self.directory.glob(f'{config.storage.rpc_temp_prefix}*{TEMPORARY_FILE_SUFFIX}'):
            try:
                if now - path.stat().st_mtime > self.ttl_seconds:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
