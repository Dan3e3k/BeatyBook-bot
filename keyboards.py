from __future__ import annotations

from datetime import date, datetime

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


BOOK_BUTTON = "📅 Записаться"
MY_BOOKINGS_BUTTON = "🗓 Мои записи"
ADMIN_BUTTON = "⚙️ Админ-панель"

BOOKING_STATUS_LABELS = {
    "confirmed": "Подтверждена",
    "completed": "Завершена",
    "cancelled": "Отменена",
}


def booking_status_label(status: str) -> str:
    return BOOKING_STATUS_LABELS.get(status, status)


class ServiceCallback(CallbackData, prefix="svc"):
    service_id: int


class DateCallback(CallbackData, prefix="day"):
    service_id: int
    selected_date: str


class SlotCallback(CallbackData, prefix="slot"):
    service_id: int
    selected_date: str
    selected_time: str


class BookingFlowCallback(CallbackData, prefix="flow"):
    action: str


class UserBookingCallback(CallbackData, prefix="mybook"):
    booking_id: int
    action: str


class AdminCallback(CallbackData, prefix="admin"):
    action: str


class AdminBookingCallback(CallbackData, prefix="admbook"):
    booking_id: int
    action: str


def main_menu(*, is_owner: bool) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text=BOOK_BUTTON), KeyboardButton(text=MY_BOOKINGS_BUTTON)]
    ]
    if is_owner:
        keyboard.append([KeyboardButton(text=ADMIN_BUTTON)])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def services_keyboard(services: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{service['name']} · {service['duration_minutes']} мин · {service['price']:.0f} ₽",
                callback_data=ServiceCallback(service_id=service["id"]).pack(),
            )
        ]
        for service in services
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dates_keyboard(service_id: int, dates: list[date]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for selected_date in dates:
        row.append(
            InlineKeyboardButton(
                text=selected_date.strftime("%d.%m"),
                callback_data=DateCallback(
                    service_id=service_id, selected_date=selected_date.isoformat()
                ).pack(),
            )
        )
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slots_keyboard(
    service_id: int, selected_date: date, slots: list[datetime]
) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for slot in slots:
        row.append(
            InlineKeyboardButton(
                text=slot.strftime("%H:%M"),
                callback_data=SlotCallback(
                    service_id=service_id,
                    selected_date=selected_date.isoformat(),
                    selected_time=slot.strftime("%H:%M"),
                ).pack(),
            )
        )
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=BookingFlowCallback(action="confirm").pack(),
                ),
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data=BookingFlowCallback(action="cancel").pack(),
                ),
            ]
        ]
    )


def user_bookings_keyboard(bookings: list[dict]) -> InlineKeyboardMarkup | None:
    rows = [
        [
            InlineKeyboardButton(
                text=f"Отменить запись #{booking['id']}",
                callback_data=UserBookingCallback(
                    booking_id=booking["id"], action="cancel"
                ).pack(),
            )
        ]
        for booking in bookings
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Ближайшие записи",
                    callback_data=AdminCallback(action="bookings").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗓 Расписание на 7 дней",
                    callback_data=AdminCallback(action="schedule").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⛔ Закрыть интервал",
                    callback_data=AdminCallback(action="close").pack(),
                )
            ],
        ]
    )


def admin_bookings_keyboard(bookings: list[dict]) -> InlineKeyboardMarkup | None:
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"Запись #{booking['id']} · "
                    f"{booking_status_label(booking['status'])}"
                ),
                callback_data=AdminBookingCallback(
                    booking_id=booking["id"], action="details"
                ).pack(),
            )
        ]
        for booking in bookings
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ В меню админки",
                callback_data=AdminCallback(action="back").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_booking_actions(booking_id: int, status: str) -> InlineKeyboardMarkup:
    actions: list[list[InlineKeyboardButton]] = []
    if status != "confirmed":
        actions.append(
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=AdminBookingCallback(
                        booking_id=booking_id, action="confirmed"
                    ).pack(),
                )
            ]
        )
    if status != "completed":
        actions.append(
            [
                InlineKeyboardButton(
                    text="🏁 Завершить",
                    callback_data=AdminBookingCallback(
                        booking_id=booking_id, action="completed"
                    ).pack(),
                )
            ]
        )
    if status != "cancelled":
        actions.append(
            [
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=AdminBookingCallback(
                        booking_id=booking_id, action="cancelled"
                    ).pack(),
                )
            ]
        )
    actions.append(
        [
            InlineKeyboardButton(
                text="⬅️ К списку",
                callback_data=AdminCallback(action="bookings").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=actions)


def cancel_admin_input_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=AdminCallback(action="back").pack(),
                )
            ]
        ]
    )
