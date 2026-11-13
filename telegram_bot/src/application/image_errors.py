"""Map known image validation failures to safe Telegram copy."""
import math
import re

from imager_config.loader import config


BUILD_FAILED_TEXT = 'Не получилось собрать изображение 😔'
IMAGE_TOO_NARROW_TEXT = 'Картинка слишком узкая 😔 Обрежьте её до квадрата или пришлите другую.'
IMAGE_TOO_WIDE_TEXT = 'Картинка слишком широкая 😔 Обрежьте её до квадрата или пришлите другую.'

_ASPECT_ERROR = re.compile(
    r'Invalid image: Image aspect ratio (?P<ratio>[0-9]+(?:\.[0-9]+)?) '
    r'must be between (?P<minimum>[0-9]+(?:\.[0-9]+)?) '
    r'and (?P<maximum>[0-9]+(?:\.[0-9]+)?)'
)


def _aspect_message(ratio, minimum, maximum):
    if not all(math.isfinite(number) and number > 0 for number in (ratio, minimum, maximum)):
        return None
    if minimum >= maximum:
        return None
    if ratio < minimum:
        return IMAGE_TOO_NARROW_TEXT
    if ratio > maximum:
        return IMAGE_TOO_WIDE_TEXT
    return None


def image_aspect_error(width, height):
    """Preflight Telegram dimensions; missing metadata remains the core's job."""
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        return None
    try:
        ratio = width / height
    except OverflowError:
        return None
    return _aspect_message(ratio, config.image.aspect_ratio_min, config.image.aspect_ratio_max)


def format_generation_error(job):
    """Only recognized core failures are translated; raw infrastructure text stays private."""
    error = job.get('error')
    if not isinstance(error, str) or len(error) > config.bot.error_max_length:
        return BUILD_FAILED_TEXT
    match = _ASPECT_ERROR.fullmatch(error)
    if match is None:
        return BUILD_FAILED_TEXT
    try:
        values = [float(match[name]) for name in ('ratio', 'minimum', 'maximum')]
    except (ValueError, OverflowError):
        return BUILD_FAILED_TEXT
    message = _aspect_message(*values)
    if message:
        return message
    ratio, minimum, maximum = values
    if (all(math.isfinite(value) and value > 0 for value in values)
            and minimum < maximum):
        # The core formats the ratio/bounds to four decimals. A rejected value
        # can round to the boundary itself; the explicit error still tells us
        # which side failed without leaking the original diagnostic.
        if ratio == minimum:
            return IMAGE_TOO_NARROW_TEXT
        if ratio == maximum:
            return IMAGE_TOO_WIDE_TEXT
    return BUILD_FAILED_TEXT
