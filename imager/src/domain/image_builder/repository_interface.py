from abc import ABC, abstractmethod, abstractproperty
from typing import Protocol

from src.shared_kernel.result import Result


class ImageFiles(Protocol):
    def read_image_file(self, path: str) -> Result: ...

    def save_image_file(self, image, path: str, save_params=None) -> Result: ...


class ImageCatalog(Protocol):
    def find_closest_metadata(self, colors, group: str) -> Result: ...

    def get_cell_image(self, cell) -> Result: ...


class IRepository(ABC):
    @abstractproperty
    def data(self):
        ''' Хранилище данных '''

    @abstractmethod
    def initialize(self):
        ''' Инициализация репозитория
        с заполнением хранилища '''
