from pydantic import BaseModel, Field
from datetime import datetime
from typing import Dict

class GeneratedImage(BaseModel):
    generated_path: str
    generated_datetime_utc: datetime
    data: Dict[str, any]  # TODO: any?
