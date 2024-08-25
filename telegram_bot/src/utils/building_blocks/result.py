from src.utils.loggers import exception_logger


class Result:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.is_success = error is None

        if not self.is_success:
            exception_logger.error(f'Error: {self.error}')

    @classmethod
    def Success(cls, value):
        return cls(value=value)

    @classmethod
    def Error(cls, error):
        return cls(error=error)

    def __repr__(self):
        if self.is_success:
            return f'<Success: {self.value}>'
        else:
            return f'<Error: {self.error}>'

    def __bool__(self):
        return self.is_success
