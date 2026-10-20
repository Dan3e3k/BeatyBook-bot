from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from config import Settings
from database import (
    BookingNotFoundError,
    Database,
    SlotUnavailableError,
    format_utc,
    parse_utc,
)


UTC = timezone.utc


class InvalidBookingTimeError(ValueError):
    """The requested local time is invalid or outside salon hours."""


def localize_wall_time(value: datetime, timezone_info: ZoneInfo) -> datetime | None:
    if value.tzinfo is not None:
        raise ValueError("Ожидается местное время без часового пояса.")
    first = value.replace(tzinfo=timezone_info, fold=0)
    second = value.replace(tzinfo=timezone_info, fold=1)
    first_valid = first.astimezone(UTC).astimezone(timezone_info).replace(tzinfo=None) == value
    second_valid = second.astimezone(UTC).astimezone(timezone_info).replace(tzinfo=None) == value
    if not first_valid and not second_valid:
        return None
    if first_valid and second_valid and first.utcoffset() != second.utcoffset():
        return None
    return first if first_valid else second


class BookingService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    def available_dates(self, *, today: date | None = None) -> list[date]:
        current_date = today or datetime.now(self.settings.timezone).date()
        return [
            current_date + timedelta(days=offset)
            for offset in range(self.settings.booking_days_ahead + 1)
        ]

    async def available_slots(
        self, service_id: int, local_date: date
    ) -> list[datetime]:
        service = await self.database.get_service(service_id)
        if service is None:
            return []

        today = datetime.now(self.settings.timezone).date()
        if local_date < today or local_date > today + timedelta(
            days=self.settings.booking_days_ahead
        ):
            return []

        start_of_day = datetime.combine(local_date, time.min).replace(
            tzinfo=self.settings.timezone
        )
        start_next_day = datetime.combine(
            local_date + timedelta(days=1), time.min
        ).replace(tzinfo=self.settings.timezone)
        busy = await self.database.list_busy_intervals(
            format_utc(start_of_day), format_utc(start_next_day)
        )
        busy_utc = [(parse_utc(start), parse_utc(end)) for start, end in busy]

        opens = datetime.combine(
            local_date, time(hour=self.settings.business_start)
        )
        closes = datetime.combine(
            local_date,
            time(hour=self.settings.business_end % 24),
        )
        if self.settings.business_end == 24:
            closes = datetime.combine(local_date + timedelta(days=1), time.min)

        results: list[datetime] = []
        minutes_after_open = 0
        while True:
            naive_start = opens + timedelta(minutes=minutes_after_open)
            candidate = localize_wall_time(naive_start, self.settings.timezone)
            if candidate is None:
                minutes_after_open += self.settings.slot_step_minutes
                if naive_start >= closes:
                    break
                continue
            candidate_utc = candidate.astimezone(UTC)
            end_utc = candidate_utc + timedelta(minutes=service["duration_minutes"])
            end_local = end_utc.astimezone(self.settings.timezone)
            if (
                (self.settings.business_end != 24 and end_local.date() != local_date)
                or end_local.replace(tzinfo=None) > closes
            ):
                break
            if candidate_utc <= datetime.now(UTC):
                minutes_after_open += self.settings.slot_step_minutes
                continue
            if all(candidate_utc >= busy_end or end_utc <= busy_start for busy_start, busy_end in busy_utc):
                results.append(candidate)
            minutes_after_open += self.settings.slot_step_minutes
            if opens + timedelta(minutes=minutes_after_open) >= closes:
                break
        return results

    async def create_booking(
        self,
        *,
        user_id: int,
        customer_name: str,
        phone: str,
        service_id: int,
        local_start: datetime,
    ) -> int:
        if not customer_name.strip():
            raise ValueError("Укажите имя.")
        normalized_phone = phone.strip()
        if len(normalized_phone) < 6 or len(normalized_phone) > 32:
            raise ValueError("Проверьте номер телефона.")
        if local_start.tzinfo is not None:
            raise ValueError("Время записи должно быть местным временем салона.")
        aware_start = localize_wall_time(local_start, self.settings.timezone)
        if aware_start is None:
            raise InvalidBookingTimeError(
                "Это местное время не существует или неоднозначно из-за перевода часов."
            )
        service = await self.database.get_service(service_id)
        if service is None:
            raise BookingNotFoundError("Выбранная услуга больше недоступна.")
        possible_slots = await self.available_slots(service_id, local_start.date())
        if aware_start not in possible_slots:
            raise InvalidBookingTimeError("Это время уже недоступно или вне расписания.")
        start_utc = format_utc(aware_start)
        return await self.database.create_booking(
            user_id=user_id,
            customer_name=customer_name.strip()[:100],
            phone=normalized_phone[:32],
            service_id=service_id,
            start_utc=start_utc,
        )

    def to_local(self, utc_value: str) -> datetime:
        return parse_utc(utc_value).astimezone(self.settings.timezone)

    def format_local(self, utc_value: str, *, include_date: bool = True) -> str:
        local = self.to_local(utc_value)
        return local.strftime("%d.%m.%Y в %H:%M" if include_date else "%H:%M")

    def parse_local_input(self, value: str) -> datetime:
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise InvalidBookingTimeError(
                "Введите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ."
            ) from exc


__all__ = (
    "BookingService",
    "InvalidBookingTimeError",
    "SlotUnavailableError",
)
