from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    owner_id: int
    timezone: ZoneInfo
    database_path: Path
    business_start: int
    business_end: int
    slot_step_minutes: int
    booking_days_ahead: int
    google_sheet_id: str | None
    google_credentials_file: Path | None
    reminder_poll_seconds: int = 30
    sheets_poll_seconds: int = 30


def load_settings() -> Settings:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("Не задан обязательный BOT_TOKEN в файле .env.")

    owner_id_value = os.getenv("OWNER_ID", "").strip()
    try:
        owner_id = int(owner_id_value)
    except ValueError as exc:
        raise ValueError("OWNER_ID должен быть целым числом Telegram ID.") from exc
    if owner_id <= 0:
        raise ValueError("OWNER_ID должен быть положительным Telegram ID.")

    timezone_name = os.getenv("TIMEZONE", "Europe/Moscow").strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Неизвестный часовой пояс TIMEZONE: {timezone_name}") from exc

    business_start = _read_int("BUSINESS_START_HOUR", 9, 0, 23)
    business_end = _read_int("BUSINESS_END_HOUR", 18, 1, 24)
    if business_start >= business_end:
        raise ValueError("BUSINESS_START_HOUR должен быть меньше BUSINESS_END_HOUR.")

    google_sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip() or None
    credentials_value = os.getenv("GOOGLE_CREDENTIALS_FILE", "").strip()
    credentials_file = Path(credentials_value).expanduser() if credentials_value else None
    if bool(google_sheet_id) != bool(credentials_file):
        raise ValueError(
            "Для Google Sheets одновременно задайте GOOGLE_SHEET_ID "
            "и GOOGLE_CREDENTIALS_FILE либо оставьте оба значения пустыми."
        )

    return Settings(
        bot_token=bot_token,
        owner_id=owner_id,
        timezone=timezone,
        database_path=Path(os.getenv("DATABASE_PATH", "beautybook.sqlite3")).expanduser(),
        business_start=business_start,
        business_end=business_end,
        slot_step_minutes=_read_int("SLOT_STEP_MINUTES", 30, 5, 240),
        booking_days_ahead=_read_int("BOOKING_DAYS_AHEAD", 30, 1, 365),
        google_sheet_id=google_sheet_id,
        google_credentials_file=credentials_file,
        reminder_poll_seconds=_read_int("REMINDER_POLL_SECONDS", 30, 5, 3600),
        sheets_poll_seconds=_read_int("SHEETS_POLL_SECONDS", 30, 5, 3600),
    )


def _read_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} должен быть целым числом.") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} должен быть в диапазоне {minimum}–{maximum}.")
    return parsed
