from imager.infrastructure.data.image_builder.unit_of_work import get_uow
from imager.shared_kernel.result import Result
from imager.domain.image_builder.value_objects.image_build_params import (
    ImageInsertionFormat, AlphaChannel, NoiseLevel, CellSize, ResultSize, ImageGroup
)
from imager.application.image_builder.services.imager_builder import ImagerBuilder


class GenerateImageCommand:
    def __init__(self, image_path, insertion_format, alpha_channel, noise_level, cell_size, result_size, group_name):
        self.image_path = image_path
        self.insertion_format = insertion_format
        self.alpha_channel = alpha_channel
        self.noise_level = noise_level
        self.cell_size = cell_size
        self.result_size = result_size
        self.group_name = group_name


class GenerateImageCommandHandler:
    def handle(self, command: GenerateImageCommand) -> Result:
        try:
            uow = get_uow()

            insertion_format_result = ImageInsertionFormat.create(command.insertion_format)
            if not insertion_format_result.is_success:
                return insertion_format_result

            alpha_channel_result = AlphaChannel.create(command.alpha_channel)
            if not alpha_channel_result.is_success:
                return alpha_channel_result

            noise_level_result = NoiseLevel.create(command.noise_level)
            if not noise_level_result.is_success:
                return noise_level_result

            cell_size_result = CellSize.create(command.cell_size)
            if not cell_size_result.is_success:
                return cell_size_result

            result_size_result = ResultSize.create(command.result_size)
            if not result_size_result.is_success:
                return result_size_result

            image_group_result = ImageGroup.create(command.group_name)
            if not image_group_result.is_success:
                return image_group_result

            builder = ImagerBuilder(
                file_repository=uow.file_repository,
                cell_repository=uow.cell_repository,
                insertion_format=command.insertion_format,
                result_size=command.result_size,
                cell_size=command.cell_size,
                alpha=command.alpha_channel / 100,
                noise_degree=command.noise_level
            )
            final_image_result = builder.make_image(command.image_path, command.group_name)
            if not final_image_result.is_success:
                return final_image_result

            return final_image_result
        except Exception as e:
            return Result.Error(f'An error occurred: {e}')
