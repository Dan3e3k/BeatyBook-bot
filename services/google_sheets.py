from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiosqlite

from config import Settings
from database import Database
from services.booking_service import BookingService


logger = logging.getLogger(__name__)
HEADERS = [
    "ID записи",
    "Статус",
    "Имя клиента",
    "Телефон",
    "Услуга",
    "Начало",
    "Окончание",
    "Создано (UTC)",
]


def _sync_booking(
    credentials_file: Path,
    spreadsheet_id: str,
    row: list[str],
) -> None:
    import gspread

    client = gspread.service_account(filename=str(credentials_file))
    worksheet = client.open_by_key(spreadsheet_id).sheet1
    existing_ids = worksheet.col_values(1)
    booking_id = row[0]
    if not existing_ids:
        worksheet.update(values=[HEADERS], range_name="A1:H1")
        existing_ids = [HEADERS[0]]
    try:
        row_index = existing_ids.index(booking_id) + 1
    except ValueError:
        worksheet.append_row(row, value_input_option="USER_ENTERED")
    else:
        worksheet.update(values=[row], range_name=f"A{row_index}:H{row_index}")


async def sync_pending_once(
    database: Database,
    booking_service: BookingService,
    settings: Settings,
) -> None:
    if settings.google_sheet_id is None or settings.google_credentials_file is None:
        return
    if not settings.google_credentials_file.is_file():
        logger.error(
            "Файл Google credentials не найден: %s",
            settings.google_credentials_file,
        )
        for booking in await database.pending_sheet_rows():
            await database.mark_sheet_failed(
                booking["id"],
                booking["sync_updated_utc"],
                "Файл Google credentials не найден.",
            )
        return
    try:
        from google.auth.exceptions import GoogleAuthError
        from gspread.exceptions import GSpreadException
    except ImportError as error:
        logger.error("Не установлены зависимости Google Sheets: %s", error)
        for booking in await database.pending_sheet_rows():
            await database.mark_sheet_failed(
                booking["id"], booking["sync_updated_utc"], str(error)
            )
        return
    rows = await database.pending_sheet_rows()
    for booking in rows:
        row = [
            str(booking["id"]),
            booking["status"],
            booking["customer_name"],
            booking["phone"],
            booking["service_name"],
            booking_service.format_local(booking["start_utc"]),
            booking_service.format_local(booking["end_utc"]),
            booking["created_utc"],
        ]
        try:
            await asyncio.to_thread(
                _sync_booking,
                settings.google_credentials_file,
                settings.google_sheet_id,
                row,
            )
        except (GSpreadException, GoogleAuthError, OSError, ImportError) as error:
            logger.warning(
                "Синхронизация записи #%s с Google Sheets не удалась: %s",
                booking["id"],
                error,
            )
            await database.mark_sheet_failed(
                booking["id"], booking["sync_updated_utc"], str(error)
            )
        else:
            await database.mark_sheet_synced(
                booking["id"], booking["sync_updated_utc"]
            )


async def sheets_worker(
    database: Database,
    booking_service: BookingService,
    settings: Settings,
) -> None:
    if settings.google_sheet_id is None or settings.google_credentials_file is None:
        logger.info("Google Sheets отключён: конфигурация не задана.")
        return
    while True:
        try:
            await sync_pending_once(database, booking_service, settings)
        except aiosqlite.Error:
            logger.exception("Ошибка SQLite при обработке очереди Google Sheets")
        await asyncio.sleep(settings.sheets_poll_seconds)


__all__ = ("sheets_worker", "sync_pending_once")
