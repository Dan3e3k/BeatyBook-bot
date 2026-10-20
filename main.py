from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from config import load_settings
from database import Database
from handlers.admin import router as admin_router
from handlers.user import router as user_router
from services.booking_service import BookingService
from services.google_sheets import sheets_worker
from services.reminders import reminder_worker


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    database = Database(settings.database_path)
    await database.initialize()
    booking_service = BookingService(database, settings)

    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher["settings"] = settings
    dispatcher["database"] = database
    dispatcher["booking_service"] = booking_service
    dispatcher.include_router(admin_router)
    dispatcher.include_router(user_router)

    background_tasks = [
        asyncio.create_task(
            reminder_worker(bot, database, booking_service, settings),
            name="beautybook-reminders",
        ),
        asyncio.create_task(
            sheets_worker(database, booking_service, settings),
            name="beautybook-google-sheets",
        ),
    ]

    def report_background_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logging.getLogger(__name__).error(
                "Фоновая задача %s завершилась с ошибкой",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    for task in background_tasks:
        task.add_done_callback(report_background_failure)
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
