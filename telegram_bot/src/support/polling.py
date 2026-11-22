"""Supervise independent bot dispatchers within one process."""
import asyncio

from imager_config.loader import config
from src.utils.loggers import exception_logger


async def poll_dispatchers(main_dispatcher, main_bot, *, support_dispatcher=None, support_bot=None):
    pairs = [(main_dispatcher, main_bot, ['message', 'callback_query'])]
    if support_dispatcher is not None:
        if support_bot is None:
            raise ValueError('A support dispatcher requires its own bot')
        pairs.append((support_dispatcher, support_bot, ['message']))
    tasks = [asyncio.create_task(dispatcher.start_polling(
        bot, close_bot_session=False, allowed_updates=updates,
        handle_signals=index == 0, tasks_concurrency_limit=config.bot.max_concurrent_updates,
    )) for index, (dispatcher, bot, updates) in enumerate(pairs)]

    async def stop(dispatcher, task):
        if task.done():
            return
        try:
            await dispatcher.stop_polling()
        except RuntimeError as error:
            if str(error) != 'Polling is not started':
                exception_logger.exception('Cannot stop Telegram polling')
            # This dispatcher did not reach its polling phase, so it has no
            # internal long-polling task to stop through the public API.
            task.cancel()

    try:
        completed, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            task.result()
    finally:
        try:
            await asyncio.wait_for(asyncio.gather(*[
                stop(dispatcher, task) for (dispatcher, _, _), task in zip(pairs, tasks)
            ]), timeout=config.bot.shutdown_seconds)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
