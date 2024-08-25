import asyncio

from setup import (set_initial_admin, setup_bot_and_dispatcher, setup_database,
                   setup_handlers, setup_middlewares)


async def main():
    # Bot + dispatcher
    bot, dp = setup_bot_and_dispatcher()

    # Database
    uow = setup_database()

    # Bot parametres
    setattr(bot, 'user_repository', uow.user_repository)

    # Middlewares
    setup_middlewares(dp, uow)

    # Handlers
    setup_handlers(dp, uow)

    # Admin
    await set_initial_admin(uow)

    # Start
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
