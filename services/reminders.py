from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config import Settings
from database import Database, format_utc
from services.booking_service import BookingService


logger = logging.getLogger(__name__)
UTC = timezone.utc


async def send_due_reminders(
    bot: Bot,
    database: Database,
    booking_service: BookingService,
    settings: Settings,
) -> None:
    now = datetime.now(UTC)
    bookings = await database.get_due_reminders(
        format_utc(now), format_utc(now + timedelta(hours=1))
    )
    for booking in bookings:
        local_time = booking_service.format_local(booking["start_utc"])
        text = (
            f"Напоминаем: через час у вас запись в салон.\n"
            f"Услуга: {booking['service_name']}\n"
            f"Время: {local_time}\n"
            f"Номер записи: {booking['id']}."
        )
        admin_text = (
            f"Напоминание: запись №{booking['id']} примерно через час.\n"
            f"Клиент: {booking['customer_name']} · {booking['phone']}\n"
            f"Услуга: {booking['service_name']}\n"
            f"Время: {local_time}."
        )
        for recipient, chat_id, message_text in (
            ("client", booking["user_id"], text),
            ("admin", settings.owner_id, admin_text),
        ):
            if booking[f"{recipient}_reminder_status"] == "sent":
                continue
            try:
                await bot.send_message(chat_id, message_text)
            except TelegramAPIError as error:
                logger.warning(
                    "Не удалось отправить напоминание %s для записи #%s: %s",
                    recipient,
                    booking["id"],
                    error,
                )
                await database.update_reminder(
                    booking["id"], recipient, sent=False, error=str(error)
                )
            else:
                await database.update_reminder(booking["id"], recipient, sent=True)


async def reminder_worker(
    bot: Bot,
    database: Database,
    booking_service: BookingService,
    settings: Settings,
) -> None:
    while True:
        try:
            await send_due_reminders(bot, database, booking_service, settings)
        except aiosqlite.Error:
            logger.exception("Ошибка базы данных при обработке напоминаний")
        await asyncio.sleep(settings.reminder_poll_seconds)
