import os

from settings import settings
from src.domain.image_builder.value_objects.image_build_params import (
    AlphaChannel, CellSize, ImageGroup, ImageInsertionFormat, NoiseLevel,
    ResultSize)
from src.infrastructure.data.image_builder.unit_of_work import get_uow
from src.shared_kernel.result import Result
from src.shared_kernel.error_handler import error_from_exception
from src.shared_kernel.loggers import application_logger
from src.shared_kernel.resource_limits import MAX_OUTPUT_PIXELS, generation_slot

from ..errors.commands_errors import CommandsErrorMessages
from ..interfaces_cqrs import ICommand, ICommandHandler
from ..services.imager_builder import ImagerBuilder


class GenerateImageCommand(ICommand):
    def __init__(
        self, image_path, insertion_format, alpha_channel,
        noise_level, cell_size, result_size, group_name, output_name=None
    ):
        self.image_path = image_path
        self.insertion_format = insertion_format
        self.alpha_channel = alpha_channel
        self.noise_level = noise_level
        self.cell_size = cell_size
        self.result_size = result_size
        self.group_name = group_name
        self.output_name = output_name


class GenerateImageCommandHandler(ICommandHandler):
    def __init__(self, uow_factory=None):
        self.uow_factory = uow_factory

    @staticmethod
    def _reject(result):
        application_logger.error(f'Generation request rejected: {result.error}')
        return result

    def handle(self, command: GenerateImageCommand) -> Result:
        try:
            if not isinstance(command.image_path, str) or not command.image_path.strip():
                return self._reject(Result.Error('Source image path must be a non-empty string'))
            if command.output_name is not None and not isinstance(command.output_name, str):
                return self._reject(Result.Error('Output name must be a string'))

            insertion_format_result = ImageInsertionFormat.create(
                command.insertion_format
            )
            if not insertion_format_result.is_success:
                return self._reject(insertion_format_result)

            alpha_channel_result = AlphaChannel.create(command.alpha_channel)
            if not alpha_channel_result.is_success:
                return self._reject(alpha_channel_result)

            noise_level_result = NoiseLevel.create(command.noise_level)
            if not noise_level_result.is_success:
                return self._reject(noise_level_result)

            cell_size_result = CellSize.create(command.cell_size)
            if not cell_size_result.is_success:
                return self._reject(cell_size_result)

            result_size_result = ResultSize.create(command.result_size)
            if not result_size_result.is_success:
                return self._reject(result_size_result)

            image_group_result = ImageGroup.create(command.group_name)
            if not image_group_result.is_success:
                return self._reject(image_group_result)

            estimated_pixels = (command.result_size * command.cell_size) ** 2
            if estimated_pixels > MAX_OUTPUT_PIXELS:
                return self._reject(Result.Error(
                    f'Output pixel estimate {estimated_pixels} exceeds {MAX_OUTPUT_PIXELS}'
                ))
            estimated_bytes = int(
                estimated_pixels * settings.generation_estimated_jpeg_bytes_per_pixel
            )
            if estimated_bytes > settings.telegram_max_document_size_bytes:
                return self._reject(CommandsErrorMessages.output_estimate_too_large(
                    command.result_size,
                    command.cell_size,
                    estimated_bytes,
                    settings.telegram_max_document_size_bytes,
                ))

            # Validate the request before initializing Mongo/catalog/cache.
            # All entry points share one generation slot in this process.
            with generation_slot():
                uow = (self.uow_factory or get_uow)()
                builder = ImagerBuilder(
                    file_repository=uow.file_repository,
                    cell_repository=uow.cell_repository,
                    insertion_format=command.insertion_format,
                    result_size=command.result_size,
                    cell_size=command.cell_size,
                    alpha=command.alpha_channel / 100,
                    noise_degree=command.noise_level
                )
                image_path = os.path.join(settings.file_path_prefix, command.image_path)
                result = builder.make_image(image_path, command.group_name, command.output_name)
                if not result:
                    application_logger.error(f'Generation failed: {result.error}')
                return result
        except Exception as e:
            application_logger.exception('Generation command failed')
            return Result.Error(error_from_exception(e, 'Generation command failed'))
