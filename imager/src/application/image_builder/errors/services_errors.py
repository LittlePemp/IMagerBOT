from src.shared_kernel.loggers import application_logger
from src.shared_kernel.result import Result


class ServicesErrorMessages:
    @staticmethod
    def error_in_make_image(e):
        application_logger.error(f'Error in make_image: {e}')
        return Result.Error(str(e))

    @staticmethod
    def no_closest_cell_found(pixel_rgb, group):
        application_logger.error(
            f'No closest cell found for pixel {pixel_rgb} in group {group}'
        )

    @staticmethod
    def cell_image_is_none(pixel_rgb, group):
        application_logger.error(
            f'Cell image is None for pixel {pixel_rgb} in group {group}'
        )
