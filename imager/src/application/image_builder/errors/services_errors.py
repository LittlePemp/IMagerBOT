from src.shared_kernel.loggers import application_logger
from src.shared_kernel.result import Result
from src.shared_kernel.error_handler import error_from_exception


class ServicesErrorMessages:
    @staticmethod
    def error_in_make_image(e):
        application_logger.exception(f'Error in make_image: {e}')
        return Result.Error(error_from_exception(e, 'Image generation failed'))

    @staticmethod
    def no_closest_cell_found(pixel_rgb, group):
        message = f'No closest cell found for pixel {pixel_rgb} in group {group}'
        application_logger.error(
            message
        )
        return message

    @staticmethod
    def cell_image_is_none(pixel_rgb, group):
        message = f'Cell image is None for pixel {pixel_rgb} in group {group}'
        application_logger.error(
            message
        )
        return message
