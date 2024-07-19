from pydantic import BaseModel, Field
from datetime import datetime
from typing import Any, Dict

class GeneratedImage(BaseModel):
    generated_path: str
    generated_datetime_utc: datetime
    data: Dict[str, Any]  # TODO: any?

    class Config:
        arbitrary_types_allowed = True
