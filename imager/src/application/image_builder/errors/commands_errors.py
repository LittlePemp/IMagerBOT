from src.shared_kernel.result import Result


class CommandsErrorMessages:
    @staticmethod
    def failed_to_read_image(image_file, error):
        return Result.Error(f'Failed to read {image_file}: {error}')

    @staticmethod
    def image_size_out_of_bounds(
        image_file,
        width,
        height,
        min_width,
        min_height,
        max_width,
        max_height
    ):
        return Result.Error(f'Image {image_file} size {width}x{height}'
                            ' is out of bounds '
                            f'({min_width}x{min_height} '
                            f'to {max_width}x{max_height})')

    @staticmethod
    def image_aspect_ratio_out_of_bounds(
        image_file, aspect_ratio,
        min_aspect_ratio, max_aspect_ratio
    ):
        return Result.Error(f'Image {image_file} '
                            f'aspect ratio {aspect_ratio:.2f} is out of bounds'
                            f' ({min_aspect_ratio:.2f} '
                            f'to {max_aspect_ratio:.2f})')

    @staticmethod
    def invalid_image_file(image_file):
        return Result.Error(f'File {image_file} is invalid as image!')

    @staticmethod
    def general_error(e):
        return Result.Error(f'An error occurred: {e}')

    @staticmethod
    def images_missing_in_database(missing_in_db):
        return Result.Error(f'Images missing in database: {missing_in_db}')

    @staticmethod
    def images_missing_on_disk(missing_on_disk):
        return Result.Error(f'Images missing on disk: {missing_on_disk}')

    @staticmethod
    def missing_images(missing_images):
        return Result.Error(f'Missing images: {missing_images}')
