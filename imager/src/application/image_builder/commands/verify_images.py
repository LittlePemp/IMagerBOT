from src.shared_kernel.result import Result

from ..interfaces_cqrs import ICommand, ICommandHandler
from .validate_images_data import ValidateImagesDataCommand, ValidateImagesDataCommandHandler


class VerifyImagesCommand(ICommand):
    def __init__(self, group_name: str):
        self.group_name = group_name


class VerifyImagesCommandHandler(ICommandHandler):
    def handle(self, command: VerifyImagesCommand) -> Result:
        return ValidateImagesDataCommandHandler().handle(ValidateImagesDataCommand(command.group_name))
