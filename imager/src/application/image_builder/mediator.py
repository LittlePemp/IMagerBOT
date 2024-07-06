from typing import Union

from .commands.check_images import (CheckImagesCommand,
                                    CheckImagesCommandHandler)
from .commands.generate_image import (GenerateImageCommand,
                                      GenerateImageCommandHandler)
from .commands.load_images import LoadImagesCommand, LoadImagesCommandHandler
from .commands.load_missing_groups import (LoadMissingGroupsCommand,
                                           LoadMissingGroupsCommandHandler)
from .commands.validate_images_data import (ValidateImagesDataCommand,
                                            ValidateImagesDataCommandHandler)
from .commands.verify_images import (VerifyImagesCommand,
                                     VerifyImagesCommandHandler)
from .interfaces_cqrs import ICommand, ICommandHandler, IQuery
from .queries.get_image_data import GetImageDataQuery, GetImageDataQueryHandler
from .queries.get_list_image_groups import (GetListImageGroupsQuery,
                                            GetListImageGroupsQueryHandler)


class Mediator:
    def __init__(self):
        self.handlers: dict[type[Union[ICommand, IQuery]],
                            ICommandHandler] = dict()

    def register(self,
                 command: type[Union[ICommand, IQuery]],
                 handler: ICommandHandler) -> None:
        self.handlers[command] = handler

    def send(self, request: ICommand):
        command = type(request)
        if command in self.handlers:
            handler = self.handlers[command]
            return handler.handle(request)
        raise ValueError(f'No handler registered for {command}')


mediator = Mediator()

mediator.register(GenerateImageCommand,
                  GenerateImageCommandHandler())
mediator.register(LoadImagesCommand,
                  LoadImagesCommandHandler())
mediator.register(LoadMissingGroupsCommand,
                  LoadMissingGroupsCommandHandler())
mediator.register(VerifyImagesCommand,
                  VerifyImagesCommandHandler())
mediator.register(ValidateImagesDataCommand,
                  ValidateImagesDataCommandHandler())
mediator.register(CheckImagesCommand,
                  CheckImagesCommandHandler())
mediator.register(GetImageDataQuery,
                  GetImageDataQueryHandler())
mediator.register(GetListImageGroupsQuery,
                  GetListImageGroupsQueryHandler())
