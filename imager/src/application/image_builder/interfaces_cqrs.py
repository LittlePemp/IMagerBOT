from abc import ABC, abstractmethod

from src.shared_kernel.result import Result


class IQuery(ABC):
    pass


class ICommand(ABC):
    pass


class IHandler(ABC):
    @abstractmethod
    def handle(self, command) -> Result:
        pass


class ICommandHandler(IHandler):
    pass


class IQueryHandler(IHandler):
    pass
