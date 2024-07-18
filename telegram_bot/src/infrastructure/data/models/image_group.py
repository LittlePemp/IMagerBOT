from pydantic import BaseModel


class ImageGroup(BaseModel):
    imager_name: str
    display_name: str
    images_count: int
