from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class User(BaseModel):
    telegram_username: str
    telegram_id: int
    name: Optional[str]
    registered_datetime_utc: datetime
    last_activity_datetime_utc: datetime
    isbanned: bool
    status: str  # admin/user/privileged
