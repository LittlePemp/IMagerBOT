from src.shared_kernel.result import Result


class EntitiesErrorMessages:
    @staticmethod
    def group_must_be_non_empty_string():
        return Result.Error('Group must be a non-empty string')

    @staticmethod
    def relative_file_path_must_be_non_empty_string():
        return Result.Error('Relative file path must be a non-empty string')

    @staticmethod
    def error_opening_image(error):
        return Result.Error(f'Error opening image: {error}')

    @staticmethod
    def rgb_must_be_an_integer():
        return Result.Error('RGB must be an integer')

    @staticmethod
    def rgb_must_be_in_range():
        return Result.Error('RGB must be in [0..255]')

    @staticmethod
    def invalid_insertion_format():
        return Result.Error('Invalid insertion format')

    @staticmethod
    def alpha_channel_must_be_an_integer():
        return Result.Error('Alpha channel must be an integer')

    @staticmethod
    def alpha_channel_must_be_in_range():
        return Result.Error('Alpha channel must be between 0 and 100')

    @staticmethod
    def noise_level_must_be_an_integer():
        return Result.Error('Noise level must be an integer')

    @staticmethod
    def noise_level_must_be_in_range():
        return Result.Error('Noise level must be between 0 and 100')

    @staticmethod
    def cell_size_must_be_an_integer():
        return Result.Error('Cell size must be an integer')

    @staticmethod
    def cell_size_must_be_greater_than_zero():
        return Result.Error('Cell size must be greater than 0')

    @staticmethod
    def result_size_must_be_an_integer():
        return Result.Error('Result size must be an integer')

    @staticmethod
    def result_size_must_be_greater_than_zero():
        return Result.Error('Result size must be greater than 0')

    @staticmethod
    def image_group_must_be_a_string():
        return Result.Error('Image group must be a string')

    @staticmethod
    def image_group_cannot_be_empty():
        return Result.Error('Image group cannot be empty')
