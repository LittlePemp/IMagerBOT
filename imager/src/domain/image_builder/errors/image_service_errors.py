from src.shared_kernel.result import Result


class ImageServiceErrorMessages:
    @staticmethod
    def failed_to_calculate_average_color(error_message: str):
        return Result.Error(f'Failed to calculate average color: {error_message}')

    @staticmethod
    def failed_to_resize_image(error_message: str):
        return Result.Error(f'Failed to resize image: {error_message}')

    @staticmethod
    def failed_to_crop_image(error_message: str):
        return Result.Error(f'Failed to crop image: {error_message}')

    @staticmethod
    def failed_to_create_template(error_message: str):
        return Result.Error(f'Failed to create template: {error_message}')

    @staticmethod
    def failed_to_split_image(error_message: str):
        return Result.Error(f'Failed to split image: {error_message}')

    @staticmethod
    def failed_to_overlay_image(error_message: str):
        return Result.Error(f'Failed to overlay image: {error_message}')

    @staticmethod
    def failed_to_find_closest_point(error_message: str):
        return Result.Error(f'Failed to find closest point: {error_message}')

    @staticmethod
    def failed_to_calculate_distance_squared(error_message: str):
        return Result.Error(f'Failed to calculate distance squared: {error_message}')

    @staticmethod
    def failed_to_convert_rgba_to_rgb(error_message: str):
        return Result.Error(f'Failed to convert RGBA to RGB: {error_message}')
