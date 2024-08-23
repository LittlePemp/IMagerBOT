from settings import settings
from src.domain.image_builder.value_objects.image_build_params import (
    AlphaChannel, CellSize, ImageGroup, ImageInsertionFormat, NoiseLevel,
    ResultSize)
from src.infrastructure.data.image_builder.unit_of_work import get_uow
from src.shared_kernel.loggers import application_logger, exception_logger
from src.shared_kernel.result import Result

from ..errors.commands_errors import CommandsErrorMessages
from ..interfaces_cqrs import ICommand, ICommandHandler
from ..services.imager_builder import ImagerBuilder


class GenerateImageCommand(ICommand):
    def __init__(
        self, image_path, insertion_format, alpha_channel,
        noise_level, cell_size, result_size, group_name
    ):
        self.image_path = image_path
        self.insertion_format = insertion_format
        self.alpha_channel = alpha_channel
        self.noise_level = noise_level
        self.cell_size = cell_size
        self.result_size = result_size
        self.group_name = group_name


class GenerateImageCommandHandler(ICommandHandler):
    def handle(self, command: GenerateImageCommand) -> Result:
        application_logger.info(f'Starting image generation for {command.image_path} in group {command.group_name}')
        try:
            with get_uow() as uow:
                insertion_format_result = ImageInsertionFormat.create(command.insertion_format)
                if not insertion_format_result.is_success:
                    application_logger.error('Invalid insertion format.')
                    return insertion_format_result

                alpha_channel_result = AlphaChannel.create(command.alpha_channel)
                if not alpha_channel_result.is_success:
                    application_logger.error('Invalid alpha channel.')
                    return alpha_channel_result

                noise_level_result = NoiseLevel.create(command.noise_level)
                if not noise_level_result.is_success:
                    application_logger.error('Invalid noise level.')
                    return noise_level_result

                cell_size_result = CellSize.create(command.cell_size)
                if not cell_size_result.is_success:
                    application_logger.error('Invalid cell size.')
                    return cell_size_result

                result_size_result = ResultSize.create(command.result_size)
                if not result_size_result.is_success:
                    application_logger.error('Invalid result size.')
                    return result_size_result

                image_group_result = ImageGroup.create(command.group_name)
                if not image_group_result.is_success:
                    application_logger.error('Invalid image group.')
                    return image_group_result

                builder = ImagerBuilder(
                    file_repository=uow.file_repository,
                    cell_repository=uow.cell_repository,
                    insertion_format=insertion_format_result.value,
                    result_size=result_size_result.value,
                    cell_size=cell_size_result.value,
                    alpha=alpha_channel_result.value.value / 100,
                    noise_degree=noise_level_result.value
                )

                image_path = settings.file_path_prefix + command.image_path
                final_image_result = builder.make_image(image_path, command.group_name)

                if final_image_result.is_success:
                    application_logger.info(f'Image successfully generated at {image_path}')
                else:
                    application_logger.error(f'Failed to generate image at {image_path}')

                return final_image_result

        except Exception as e:
            exception_logger.error(f'An error occurred while generating image: {e}')
            return CommandsErrorMessages.general_error(e)
