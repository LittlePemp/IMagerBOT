import os

from settings import settings
from src.infrastructure.data.image_builder.unit_of_work import get_uow
from src.shared_kernel.result import Result
from tqdm import tqdm

from ..errors.commands_errors import CommandsErrorMessages
from ..interfaces_cqrs import ICommand, ICommandHandler


class CheckImagesCommand(ICommand):
    def __init__(self, group_name):
        self.group_name = group_name


class CheckImagesCommandHandler(ICommandHandler):
    def handle(self, command: CheckImagesCommand) -> Result:
        uow = get_uow()
        try:
            directory = os.path.join(
                settings.image_groups_relative_path,
                command.group_name
            )
            file_repository = uow.file_repository
            image_files = file_repository.list_image_files(directory)
            for image_file in tqdm(image_files, desc='Checking images',
                                   unit='image'):
                image_result = file_repository.read_image_file(image_file)
                if not image_result.is_success:
                    return CommandsErrorMessages.failed_to_read_image(
                        image_file, image_result.error)

                image = image_result.value

                height, width = image.shape[:2]
                min_width, min_height = settings.min_size
                max_width, max_height = settings.max_size
                if not (min_width <= width <= max_width
                        and min_height <= height <= max_height):
                    return CommandsErrorMessages.image_size_out_of_bounds(
                        image_file, width, height, min_width, min_height,
                        max_width, max_height)

                aspect_ratio = width / height
                min_aspect_ratio, max_aspect_ratio = (
                    settings.aspect_ratio_limits
                )
                if not (min_aspect_ratio <= aspect_ratio <= max_aspect_ratio):
                    return CommandsErrorMessages.image_aspect_ratio_out_of_bounds(  # noqa
                        image_file, aspect_ratio, min_aspect_ratio,
                        max_aspect_ratio)

                if (image.shape[2] not in [3, 4]
                        or image.shape[2] == 4
                        and 'A' not in settings.allowed_formats):
                    return CommandsErrorMessages.invalid_image_file(image_file)

            return Result.Success(f'All images in {command.group_name} '
                                  f'checked successfully')
        except Exception as e:
            return CommandsErrorMessages.general_error(e)
