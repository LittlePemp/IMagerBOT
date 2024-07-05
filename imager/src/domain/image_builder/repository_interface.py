from abc import ABC, abstractmethod, abstractproperty


class IRepository(ABC):
    @abstractproperty
    def data(self):
        ''' Хранилище данных '''

    @abstractmethod
    def initialize(self):
        ''' Инициализация репозитория
        с заполнением хранилища '''
