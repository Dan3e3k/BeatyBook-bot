from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite


UTC = timezone.utc
BOOKING_STATUSES = {"confirmed", "completed", "cancelled"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0),
    price REAL NOT NULL DEFAULT 0 CHECK (price >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    service_id INTEGER NOT NULL REFERENCES services(id),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed'
        CHECK (status IN ('confirmed', 'completed', 'cancelled')),
    client_reminder_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (client_reminder_status IN ('pending', 'sent')),
    admin_reminder_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (admin_reminder_status IN ('pending', 'sent')),
    client_reminder_error TEXT,
    admin_reminder_error TEXT,
    client_reminder_attempts INTEGER NOT NULL DEFAULT 0,
    admin_reminder_attempts INTEGER NOT NULL DEFAULT 0,
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS closed_intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL,
    created_utc TEXT NOT NULL,
    CHECK (start_utc < end_utc)
);

CREATE TABLE IF NOT EXISTS sheets_sync (
    booking_id INTEGER PRIMARY KEY REFERENCES bookings(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'synced')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bookings_interval
    ON bookings(status, start_utc, end_utc);
CREATE INDEX IF NOT EXISTS idx_bookings_customer
    ON bookings(user_id, start_utc);
CREATE INDEX IF NOT EXISTS idx_reminders
    ON bookings(status, start_utc, client_reminder_status, admin_reminder_status);
CREATE INDEX IF NOT EXISTS idx_closed_interval
    ON closed_intervals(start_utc, end_utc);
CREATE INDEX IF NOT EXISTS idx_sheets_sync
    ON sheets_sync(status, updated_utc);
"""

DEFAULT_SERVICES = (
    ("Стрижка", 60, 1500),
    ("Укладка", 45, 1200),
    ("Маникюр", 90, 2000),
)


def utc_now_string() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Для сохранения времени требуется часовой пояс.")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("В базе обнаружено время без часового пояса.")
    return parsed.astimezone(UTC)


class SlotUnavailableError(Exception):
    """The requested slot overlaps an existing booking or a closed interval."""


class BookingNotFoundError(Exception):
    """The booking does not exist or cannot be changed by the caller."""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(str(self.path), timeout=15)
        try:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA busy_timeout = 15000")
            yield connection
        finally:
            await connection.close()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connection() as db:
            await db.executescript(SCHEMA)
            await db.executemany(
                """
                INSERT OR IGNORE INTO services (name, duration_minutes, price)
                VALUES (?, ?, ?)
                """,
                DEFAULT_SERVICES,
            )
            await db.commit()

    async def list_services(self) -> list[dict[str, Any]]:
        async with self._connection() as db:
            cursor = await db.execute(
                "SELECT id, name, duration_minutes, price FROM services "
                "WHERE active = 1 ORDER BY id"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_service(self, service_id: int) -> dict[str, Any] | None:
        async with self._connection() as db:
            cursor = await db.execute(
                "SELECT id, name, duration_minutes, price FROM services "
                "WHERE id = ? AND active = 1",
                (service_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_busy_intervals(
        self, start_utc: str, end_utc: str
    ) -> list[tuple[str, str]]:
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT start_utc, end_utc FROM bookings
                WHERE status = 'confirmed' AND start_utc < ? AND end_utc > ?
                UNION ALL
                SELECT start_utc, end_utc FROM closed_intervals
                WHERE start_utc < ? AND end_utc > ?
                """,
                (end_utc, start_utc, end_utc, start_utc),
            )
            return [(row["start_utc"], row["end_utc"]) for row in await cursor.fetchall()]

    async def create_booking(
        self,
        *,
        user_id: int,
        customer_name: str,
        phone: str,
        service_id: int,
        start_utc: str,
    ) -> int:
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT duration_minutes FROM services WHERE id = ? AND active = 1",
                    (service_id,),
                )
                service = await cursor.fetchone()
                if service is None:
                    raise BookingNotFoundError("Выбранная услуга больше недоступна.")

                start = parse_utc(start_utc)
                end_utc = format_utc(start + timedelta(minutes=service["duration_minutes"]))
                cursor = await db.execute(
                    """
                    SELECT 1 FROM bookings
                    WHERE status = 'confirmed' AND start_utc < ? AND end_utc > ?
                    UNION ALL
                    SELECT 1 FROM closed_intervals
                    WHERE start_utc < ? AND end_utc > ?
                    LIMIT 1
                    """,
                    (end_utc, start_utc, end_utc, start_utc),
                )
                if await cursor.fetchone():
                    raise SlotUnavailableError("Это время уже занято.")

                now = utc_now_string()
                cursor = await db.execute(
                    """
                    INSERT INTO bookings (
                        user_id, customer_name, phone, service_id, start_utc, end_utc,
                        status, created_utc, updated_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)
                    """,
                    (user_id, customer_name, phone, service_id, start_utc, end_utc, now, now),
                )
                booking_id = int(cursor.lastrowid)
                await db.execute(
                    """
                    INSERT INTO sheets_sync (booking_id, status, updated_utc)
                    VALUES (?, 'pending', ?)
                    """,
                    (booking_id, now),
                )
                await db.commit()
                return booking_id
            except BaseException:
                await db.rollback()
                raise

    async def list_user_bookings(
        self, user_id: int, *, future_only: bool = True
    ) -> list[dict[str, Any]]:
        now = utc_now_string()
        query = """
            SELECT b.*, s.name AS service_name, s.duration_minutes, s.price
            FROM bookings b JOIN services s ON s.id = b.service_id
            WHERE b.user_id = ?
        """
        params: list[Any] = [user_id]
        if future_only:
            query += " AND b.status = 'confirmed' AND b.start_utc > ?"
            params.append(now)
        query += " ORDER BY b.start_utc LIMIT 50"
        async with self._connection() as db:
            cursor = await db.execute(query, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def cancel_user_booking(self, booking_id: int, user_id: int) -> bool:
        now = utc_now_string()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE bookings SET status = 'cancelled', updated_utc = ?
                WHERE id = ? AND user_id = ? AND status = 'confirmed' AND start_utc > ?
                """,
                (now, booking_id, user_id, now),
            )
            if cursor.rowcount:
                await self._queue_sync(db, booking_id, now)
            await db.commit()
            return cursor.rowcount == 1

    async def list_bookings(
        self,
        *,
        start_utc: str | None = None,
        end_utc: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT b.*, s.name AS service_name, s.duration_minutes, s.price
            FROM bookings b JOIN services s ON s.id = b.service_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if start_utc is not None:
            query += " AND b.start_utc >= ?"
            params.append(start_utc)
        if end_utc is not None:
            query += " AND b.start_utc < ?"
            params.append(end_utc)
        query += " ORDER BY b.start_utc LIMIT ?"
        params.append(limit)
        async with self._connection() as db:
            cursor = await db.execute(query, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def get_booking(self, booking_id: int) -> dict[str, Any] | None:
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT b.*, s.name AS service_name, s.duration_minutes, s.price
                FROM bookings b JOIN services s ON s.id = b.service_id
                WHERE b.id = ?
                """,
                (booking_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def set_booking_status(self, booking_id: int, status: str) -> bool:
        if status not in BOOKING_STATUSES:
            raise ValueError("Недопустимый статус записи.")
        now = utc_now_string()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT start_utc, end_utc FROM bookings WHERE id = ?",
                    (booking_id,),
                )
                booking = await cursor.fetchone()
                if booking is None:
                    await db.commit()
                    return False
                if status == "confirmed":
                    cursor = await db.execute(
                        """
                        SELECT 1 FROM bookings
                        WHERE id != ? AND status = 'confirmed'
                            AND start_utc < ? AND end_utc > ?
                        UNION ALL
                        SELECT 1 FROM closed_intervals
                        WHERE start_utc < ? AND end_utc > ?
                        LIMIT 1
                        """,
                        (
                            booking["end_utc"],
                            booking["start_utc"],
                            booking["end_utc"],
                            booking["start_utc"],
                        ),
                    )
                    if await cursor.fetchone():
                        raise SlotUnavailableError(
                            "Нельзя подтвердить запись: время уже занято."
                        )
                cursor = await db.execute(
                    """
                    UPDATE bookings
                    SET status = ?, updated_utc = ?,
                        client_reminder_status = CASE
                            WHEN ? = 'confirmed' THEN 'pending' ELSE client_reminder_status END,
                        admin_reminder_status = CASE
                            WHEN ? = 'confirmed' THEN 'pending' ELSE admin_reminder_status END
                    WHERE id = ?
                    """,
                    (status, now, status, status, booking_id),
                )
                await self._queue_sync(db, booking_id, now)
                await db.commit()
                return cursor.rowcount == 1
            except BaseException:
                await db.rollback()
                raise

    async def close_interval(
        self, start_utc: str, end_utc: str, note: str, created_by: int
    ) -> int:
        now = utc_now_string()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT 1 FROM bookings
                    WHERE status = 'confirmed' AND start_utc < ? AND end_utc > ?
                    UNION ALL
                    SELECT 1 FROM closed_intervals
                    WHERE start_utc < ? AND end_utc > ?
                    LIMIT 1
                    """,
                    (end_utc, start_utc, end_utc, start_utc),
                )
                if await cursor.fetchone():
                    raise SlotUnavailableError(
                        "Интервал пересекается с записью или уже закрытым временем."
                    )
                cursor = await db.execute(
                    """
                    INSERT INTO closed_intervals (start_utc, end_utc, note, created_by, created_utc)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (start_utc, end_utc, note, created_by, now),
                )
                await db.commit()
                return int(cursor.lastrowid)
            except BaseException:
                await db.rollback()
                raise

    async def list_closed_intervals(
        self, start_utc: str, end_utc: str
    ) -> list[dict[str, Any]]:
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT id, start_utc, end_utc, note FROM closed_intervals
                WHERE start_utc < ? AND end_utc > ? ORDER BY start_utc
                """,
                (end_utc, start_utc),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_due_reminders(self, now_utc: str, due_utc: str) -> list[dict[str, Any]]:
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT b.*, s.name AS service_name
                FROM bookings b JOIN services s ON s.id = b.service_id
                WHERE b.status = 'confirmed' AND b.start_utc > ? AND b.start_utc <= ?
                    AND (b.client_reminder_status = 'pending'
                         OR b.admin_reminder_status = 'pending')
                ORDER BY b.start_utc LIMIT 100
                """,
                (now_utc, due_utc),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def update_reminder(
        self, booking_id: int, recipient: str, *, sent: bool, error: str | None = None
    ) -> None:
        if recipient not in {"client", "admin"}:
            raise ValueError("Неизвестный получатель напоминания.")
        status_column = f"{recipient}_reminder_status"
        error_column = f"{recipient}_reminder_error"
        attempts_column = f"{recipient}_reminder_attempts"
        async with self._connection() as db:
            await db.execute(
                f"""
                UPDATE bookings
                SET {status_column} = ?, {error_column} = ?, {attempts_column} = {attempts_column} + 1
                WHERE id = ? AND status = 'confirmed'
                """,
                ("sent" if sent else "pending", error, booking_id),
            )
            await db.commit()

    async def pending_sheet_rows(self, limit: int = 25) -> list[dict[str, Any]]:
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT b.*, s.name AS service_name, q.updated_utc AS sync_updated_utc
                FROM sheets_sync q
                JOIN bookings b ON b.id = q.booking_id
                JOIN services s ON s.id = b.service_id
                WHERE q.status = 'pending'
                ORDER BY q.updated_utc LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def mark_sheet_synced(
        self, booking_id: int, expected_updated_utc: str
    ) -> None:
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE sheets_sync
                SET status = 'synced', attempts = attempts + 1,
                    last_error = NULL, updated_utc = ?
                WHERE booking_id = ? AND status = 'pending' AND updated_utc = ?
                """,
                (utc_now_string(), booking_id, expected_updated_utc),
            )
            await db.commit()

    async def mark_sheet_failed(
        self, booking_id: int, expected_updated_utc: str, error: str
    ) -> None:
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE sheets_sync
                SET status = 'pending', attempts = attempts + 1,
                    last_error = ?, updated_utc = ?
                WHERE booking_id = ? AND status = 'pending' AND updated_utc = ?
                """,
                (error[:1000], utc_now_string(), booking_id, expected_updated_utc),
            )
            await db.commit()

    @staticmethod
    async def _queue_sync(
        db: aiosqlite.Connection, booking_id: int, now: str
    ) -> None:
        await db.execute(
            """
            INSERT INTO sheets_sync (booking_id, status, updated_utc)
            VALUES (?, 'pending', ?)
            ON CONFLICT(booking_id) DO UPDATE SET status = 'pending', updated_utc = excluded.updated_utc
            """,
            (booking_id, now),
        )
