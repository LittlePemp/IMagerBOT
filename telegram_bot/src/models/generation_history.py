import hashlib
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel


def generation_history_record_id(job_id: str) -> str:
    """One stable Mongo identity for each accepted generation command."""
    return hashlib.sha256(f'imager-history:{job_id}'.encode()).hexdigest()[:24]


class GenerationHistoryRecord(BaseModel):
    job_id: Optional[str] = None
    telegram_id: int
    telegram_username: Optional[str] = None
    queue_position: Optional[int] = None
    source_image_path: str
    result_image_path: Optional[str] = None
    image_group: str
    generation_mode: str
    insertion_format: str
    alpha_channel: int
    noise_level: int
    cell_size: int
    result_size: int
    status: str
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_job(cls, job):
        """Repair the bot's disposable history projection from the core inbox."""
        request = job['request']
        context = request['client_context']
        source = request['source']
        parameters = request['parameters']
        now = datetime.now(timezone.utc)
        return cls(
            job_id=job['_id'], telegram_id=context['user_id'],
            telegram_username=None, queue_position=None,
            source_image_path=f"s3://{source['bucket']}/{source['key']}",
            image_group=parameters['group_name'],
            generation_mode=context.get('generation_mode', 'presets'),
            insertion_format=parameters['insertion_format'],
            alpha_channel=parameters['alpha_channel'], noise_level=parameters['noise_level'],
            cell_size=parameters['cell_size'], result_size=parameters['result_size'],
            status=job['status'], created_at=job.get('created_at', now),
            updated_at=job.get('updated_at', now),
        )
