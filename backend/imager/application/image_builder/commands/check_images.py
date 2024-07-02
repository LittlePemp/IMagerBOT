import os

from imager.infrastructure.data.image_builder.unit_of_work import get_uow
from imager.shared_kernel.result import Result
from settings import settings
from tqdm import tqdm


class CheckImagesCommand:
    def __init__(self, group_name):
        self.group_name = group_name


class CheckImagesCommandHandler:
    def handle(self, command: CheckImagesCommand) -> Result:
        uow = get_uow()
        try:
            directory = os.path.join(
                settings.image_groups_relative_path,
                command.group_name
            )
            file_repository = uow.file_repository
            image_files = file_repository.list_image_files(directory)
            for image_file in tqdm(image_files, desc='Checking images', unit='image'):
                image_result = file_repository.read_image_file(image_file)
                if not image_result.is_success:
                    return Result.Error(f'Failed to read {image_file}: {image_result.error}')

                image = image_result.value

                height, width = image.shape[:2]
                min_width, min_height = settings.min_size
                max_width, max_height = settings.max_size
                if not (min_width <= width <= max_width and min_height <= height <= max_height):
                    return Result.Error(
                        f'Image {image_file} size {width}x{height} is out of bounds '
                        f'({min_width}x{min_height} to {max_width}x{max_height})')

                aspect_ratio = width / height
                min_aspect_ratio, max_aspect_ratio = settings.aspect_ratio_limits
                if not (min_aspect_ratio <= aspect_ratio <= max_aspect_ratio):
                    return Result.Error(
                        f'Image {image_file} aspect ratio {aspect_ratio:.2f} is out of bounds '
                        f'({min_aspect_ratio:.2f} to {max_aspect_ratio:.2f})')

                if image.shape[2] not in [3, 4] or image.shape[2] == 4 and 'A' not in settings.allowed_formats:
                    return Result.Error(f'Image {image_file} format is not allowed')

            return Result.Success(f'All images in {command.group_name} checked successfully')
        except Exception as e:
            return Result.Error(f'An error occurred: {e}')
