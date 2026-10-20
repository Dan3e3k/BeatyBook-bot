from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import Settings
from database import Database, SlotUnavailableError
from services.booking_service import BookingService, localize_wall_time


class BookingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "test.sqlite3")
        await self.database.initialize()
        self.settings = Settings(
            bot_token="test-token",
            owner_id=1,
            timezone=ZoneInfo("Europe/Moscow"),
            database_path=Path(self.tempdir.name) / "test.sqlite3",
            business_start=9,
            business_end=18,
            slot_step_minutes=30,
            booking_days_ahead=30,
            google_sheet_id=None,
            google_credentials_file=None,
        )
        self.booking_service = BookingService(self.database, self.settings)
        self.future_date = datetime.now(self.settings.timezone).date() + timedelta(days=2)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_overlapping_concurrent_bookings_only_create_one(self) -> None:
        local_start = datetime.combine(self.future_date, datetime.min.time()).replace(
            hour=10
        )

        async def create(user_id: int) -> object:
            try:
                return await self.booking_service.create_booking(
                    user_id=user_id,
                    customer_name=f"Клиент {user_id}",
                    phone="+79990000000",
                    service_id=1,
                    local_start=local_start,
                )
            except SlotUnavailableError as error:
                return error

        results = await asyncio.gather(create(101), create(202))
        self.assertEqual(sum(isinstance(result, int) for result in results), 1)
        self.assertEqual(
            sum(isinstance(result, SlotUnavailableError) for result in results), 1
        )
        self.assertEqual(len(await self.database.list_bookings()), 1)

    async def test_adjacent_bookings_do_not_conflict(self) -> None:
        first_start = datetime.combine(self.future_date, datetime.min.time()).replace(
            hour=10
        )
        second_start = first_start + timedelta(minutes=60)
        await self.booking_service.create_booking(
            user_id=101,
            customer_name="Первый клиент",
            phone="+79990000000",
            service_id=1,
            local_start=first_start,
        )
        second_id = await self.booking_service.create_booking(
            user_id=202,
            customer_name="Второй клиент",
            phone="+79990000001",
            service_id=1,
            local_start=second_start,
        )
        self.assertGreater(second_id, 1)

    async def test_customer_can_only_cancel_own_future_booking(self) -> None:
        local_start = datetime.combine(self.future_date, datetime.min.time()).replace(
            hour=11
        )
        booking_id = await self.booking_service.create_booking(
            user_id=101,
            customer_name="Клиент",
            phone="+79990000000",
            service_id=1,
            local_start=local_start,
        )
        self.assertFalse(await self.database.cancel_user_booking(booking_id, 202))
        self.assertTrue(await self.database.cancel_user_booking(booking_id, 101))
        self.assertEqual(await self.database.list_user_bookings(101), [])

    async def test_nonexistent_and_ambiguous_wall_times_are_rejected(self) -> None:
        timezone_info = ZoneInfo("Europe/Berlin")
        self.assertIsNone(
            localize_wall_time(datetime(2026, 3, 29, 2, 30), timezone_info)
        )
        self.assertIsNone(
            localize_wall_time(datetime(2026, 10, 25, 2, 30), timezone_info)
        )

    async def test_duration_filters_slots_that_would_end_after_closing(self) -> None:
        slots = await self.booking_service.available_slots(3, self.future_date)
        self.assertTrue(slots)
        self.assertTrue(
            all(
                (slot + timedelta(minutes=90)).strftime("%H:%M") <= "18:00"
                for slot in slots
            )
        )


if __name__ == "__main__":
    unittest.main()
