from .bot_setup import setup_bot_and_dispatcher
from .db_setup import setup_database
from .middlewares_setup import setup_middlewares
from .handlers_setup import setup_handlers
from .admin_setup import set_initial_admin

__all__ = [
    'setup_bot_and_dispatcher',
    'setup_database',
    'setup_middlewares',
    'setup_handlers',
    'setup_loggers',
    'set_initial_admin'
]
