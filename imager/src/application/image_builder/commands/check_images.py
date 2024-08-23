import os

from settings import settings
from src.infrastructure.data.image_builder.unit_of_work import get_uow
from src.shared_kernel.loggers import application_logger, exception_logger
from src.shared_kernel.result import Result
from tqdm import tqdm

from ..errors.commands_errors import CommandsErrorMessages
from ..interfaces_cqrs import ICommand, ICommandHandler


class CheckImagesCommand(ICommand):
    def __init__(self, group_name):
        self.group_name = group_name


class CheckImagesCommandHandler(ICommandHandler):
    def __init__(self):
        self.uow = get_uow()
        self.file_repository = self.uow.file_repository

    def handle(self, command: CheckImagesCommand) -> Result:
        application_logger.info(f'Starting to check images in group {command.group_name}')
        try:
            with self.uow:
                directory = os.path.join(settings.image_groups_relative_path, command.group_name)
                image_files = self.file_repository.list_image_files(directory)

                for image_file in tqdm(image_files, desc='Checking images', unit='image'):
                    image_result = self.file_repository.read_image_file(image_file)
                    if not image_result.is_success:
                        application_logger.error(f'Failed to read image: {image_file}')
                        return CommandsErrorMessages.failed_to_read_image(image_file, image_result.error)

                    image = image_result.value
                    if not self.validate_image_size(image, image_file):
                        application_logger.warning(f'Image size out of bounds: {image_file}')
                        return CommandsErrorMessages.image_size_out_of_bounds(
                            image_file, image.shape[1], image.shape[0],
                            settings.min_size[0], settings.min_size[1],
                            settings.max_size[0], settings.max_size[1])

                    if not self.validate_aspect_ratio(image, image_file):
                        application_logger.warning(f'Aspect ratio out of bounds: {image_file}')
                        return CommandsErrorMessages.image_aspect_ratio_out_of_bounds(
                            image_file, image.shape[1] / image.shape[0],
                            settings.aspect_ratio_limits[0],
                            settings.aspect_ratio_limits[1])

                    if not self.validate_image_format(image, image_file):
                        application_logger.warning(f'Invalid image format: {image_file}')
                        return CommandsErrorMessages.invalid_image_file(image_file)

                application_logger.info(f'All images in {command.group_name} checked successfully')
                return Result.Success(f'All images in {command.group_name} checked successfully')
        except Exception as e:
            exception_logger.exception(f'An error occurred while checking images: {e}')
            return CommandsErrorMessages.general_error(e)

    def validate_image_size(self, image, _):
        width, height = image.shape[1], image.shape[0]
        return (settings.min_size[0] <= width <= settings.max_size[0]
                and settings.min_size[1] <= height <= settings.max_size[1])

    def validate_aspect_ratio(self, image, _):
        aspect_ratio = image.shape[1] / image.shape[0]
        return settings.aspect_ratio_limits[0] <= aspect_ratio <= settings.aspect_ratio_limits[1]

    def validate_image_format(self, image, _):
        channels = image.shape[2] if len(image.shape) == 3 else 1
        return channels in [3, 4] and (channels != 4 or 'A' in settings.allowed_formats)
