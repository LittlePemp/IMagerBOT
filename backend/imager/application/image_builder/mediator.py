from imager.application.image_builder.commands.load_missing_groups import (
    LoadMissingGroupsCommand, LoadMissingGroupsCommandHandler)

from .commands.check_images import (CheckImagesCommand,
                                    CheckImagesCommandHandler)
from .commands.load_images import LoadImagesCommand, LoadImagesCommandHandler
from .commands.validate_images_data import (ValidateImagesDataCommand,
                                            ValidateImagesDataCommandHandler)
from .commands.verify_images import (VerifyImagesCommand,
                                     VerifyImagesCommandHandler)
from .queries.get_image_data import GetImageDataQuery, GetImageDataQueryHandler
from .queries.get_list_image_groups import (GetListImageGroupsQuery,
                                            GetListImageGroupsQueryHandler)


class Mediator:
    def __init__(self):
        self.handlers = {}

    def register(self, request_type, handler):
        self.handlers[request_type] = handler

    def send(self, request):
        request_type = type(request)
        if request_type in self.handlers:
            handler = self.handlers[request_type]
            return handler.handle(request)
        raise ValueError(f'No handler registered for {request_type}')


mediator = Mediator()

mediator.register(LoadImagesCommand, LoadImagesCommandHandler())
mediator.register(LoadMissingGroupsCommand, LoadMissingGroupsCommandHandler())
mediator.register(VerifyImagesCommand, VerifyImagesCommandHandler())
mediator.register(ValidateImagesDataCommand, ValidateImagesDataCommandHandler())
mediator.register(CheckImagesCommand, CheckImagesCommandHandler())
mediator.register(GetImageDataQuery, GetImageDataQueryHandler())
mediator.register(GetListImageGroupsQuery, GetListImageGroupsQueryHandler())
