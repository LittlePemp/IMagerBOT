from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ValidationError, field_validator
from settings import settings
from src.utils.building_blocks.result import Result


class UserStatus(str, Enum):
    ADMIN = 'admin'
    USER = 'user'
    PRIVILEGED = 'privileged'

class User(BaseModel):
    telegram_username: str
    telegram_id: int
    name: Optional[str]
    registered_datetime_utc: datetime
    last_activity_datetime_utc: datetime
    isbanned: bool
    status: UserStatus

    @property
    def is_admin(self) -> bool:
        return self.status == UserStatus.ADMIN

    @field_validator('telegram_username')
    def validate_telegram_username(cls, value):
        return value

    @field_validator('telegram_id')
    def validate_telegram_id(cls, value):
        if value <= 0:
            raise ValueError('Telegram ID must be a positive integer')
        return value

    @classmethod
    def create(cls, **kwargs) -> Result:
        try:
            valid_fields = cls.model_fields.keys()
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in valid_fields}
            now = datetime.now(settings.tzinfo)
            if 'registered_datetime_utc' not in filtered_kwargs:
                filtered_kwargs['registered_datetime_utc'] = now
            if 'last_activity_datetime_utc' not in filtered_kwargs:
                filtered_kwargs['last_activity_datetime_utc'] = now
            user = cls(**filtered_kwargs)
            return Result.Success(user)
        except ValidationError as e:
            return Result.Error(f'Validation error: {e}')
