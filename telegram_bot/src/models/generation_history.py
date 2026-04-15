from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class GenerationHistoryRecord(BaseModel):
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
