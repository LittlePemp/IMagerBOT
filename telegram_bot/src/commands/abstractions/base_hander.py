from abc import ABC, abstractmethod

from aiogram import Dispatcher


class BaseHandler(ABC):
    @abstractmethod
    def register_handlers(self, dp: Dispatcher):
        pass
