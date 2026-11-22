"""Translate this bot's verified Telegram identity into generic output branding."""
import re

from imager_contracts import validate_watermark_text


BOT_LINK_TEMPLATE = 't.me/{username}'


def bot_watermark_text(identity):
    """Accept getMe's identity, not the sender of an incoming user message."""
    username = getattr(identity, 'username', None)
    if (getattr(identity, 'is_bot', False) is not True or not isinstance(username, str)
            or re.fullmatch(r'[A-Za-z0-9_]+', username) is None):
        raise ValueError('This bot must have a valid public Telegram username')
    return validate_watermark_text(BOT_LINK_TEMPLATE.format(username=username))
