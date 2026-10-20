from __future__ import annotations

from datetime import datetime, time, timedelta
from datetime import timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import Settings
from database import (
    BookingNotFoundError,
    Database,
    SlotUnavailableError,
    format_utc,
)
from keyboards import (
    ADMIN_BUTTON,
    AdminBookingCallback,
    AdminCallback,
    admin_booking_actions,
    admin_bookings_keyboard,
    admin_menu_keyboard,
    booking_status_label,
    cancel_admin_input_keyboard,
)
from services.booking_service import BookingService, InvalidBookingTimeError, localize_wall_time
from states import AdminStates


router = Router(name="admin")
UTC = timezone.utc


def _authorized(user_id: int, settings: Settings) -> bool:
    return user_id == settings.owner_id


async def _show_admin_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Панель администратора:",
        reply_markup=admin_menu_keyboard(),
    )


async def _get_upcoming(database: Database, days: int) -> list[dict]:
    start_now = datetime.now(UTC)
    start = format_utc(start_now)
    end = format_utc(start_now + timedelta(days=days))
    return await database.list_bookings(start_utc=start, end_utc=end, limit=15)


def _booking_summary(booking: dict, booking_service: BookingService) -> str:
    return (
        f"Запись №{booking['id']} · {booking_status_label(booking['status'])}\n"
        f"Клиент: {booking['customer_name']}\n"
        f"Телефон: {booking['phone']}\n"
        f"Услуга: {booking['service_name']}\n"
        f"Время: {booking_service.format_local(booking['start_utc'])}\n"
        f"Длительность: {booking['duration_minutes']} мин."
    )


@router.message(Command("admin"))
async def admin_command(
    message: Message, settings: Settings, state: FSMContext
) -> None:
    if message.from_user is None or not _authorized(message.from_user.id, settings):
        await message.answer("Доступ запрещён.")
        return
    await _show_admin_menu(message, state)


@router.message(F.text == ADMIN_BUTTON)
async def admin_menu_button(
    message: Message, settings: Settings, state: FSMContext
) -> None:
    if message.from_user is None or not _authorized(message.from_user.id, settings):
        await message.answer("Доступ запрещён.")
        return
    await _show_admin_menu(message, state)


@router.callback_query(AdminCallback.filter())
async def admin_menu_action(
    callback: CallbackQuery,
    callback_data: AdminCallback,
    settings: Settings,
    database: Database,
    booking_service: BookingService,
    state: FSMContext,
) -> None:
    if not _authorized(callback.from_user.id, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return
    if callback_data.action == "back":
        await state.clear()
        await callback.message.answer(
            "Панель администратора:",
            reply_markup=admin_menu_keyboard(),
        )
        await callback.answer()
        return
    if callback_data.action == "close":
        await state.set_state(AdminStates.closing_interval)
        await callback.message.answer(
            "Отправьте строку в формате:\n"
            "ГГГГ-ММ-ДД ЧЧ:ММ; длительность в минутах; причина\n"
            "Например: 2026-10-20 13:00; 60; Перерыв.\n"
            "Время указывается по часовому поясу салона.",
            reply_markup=cancel_admin_input_keyboard(),
        )
        await callback.answer()
        return
    if callback_data.action not in {"bookings", "schedule"}:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    days = 7 if callback_data.action == "schedule" else 14
    bookings = await _get_upcoming(database, days)
    if not bookings:
        text = f"На ближайшие {days} дней записей нет."
    else:
        text = f"Записи на ближайшие {days} дней:"
        for booking in bookings:
            text += (
                f"\n\n№{booking['id']} · {booking_status_label(booking['status'])}\n"
                f"{booking['service_name']} · {booking['customer_name']}\n"
                f"{booking_service.format_local(booking['start_utc'])}"
            )
    if callback_data.action == "schedule":
        today = datetime.now(booking_service.settings.timezone).date()
        day_start = datetime.combine(today, time.min).replace(
            tzinfo=booking_service.settings.timezone
        )
        day_end = datetime.combine(today + timedelta(days=7), time.min).replace(
            tzinfo=booking_service.settings.timezone
        )
        closures = await database.list_closed_intervals(
            format_utc(day_start), format_utc(day_end)
        )
        text += "\n\nЗакрытые интервалы:"
        if closures:
            for interval in closures:
                text += (
                    f"\n{booking_service.format_local(interval['start_utc'])} — "
                    f"{booking_service.format_local(interval['end_utc'])}"
                    + (f" · {interval['note']}" if interval["note"] else "")
                )
        else:
            text += "\nНет."
    await callback.message.answer(text, reply_markup=admin_bookings_keyboard(bookings))
    await callback.answer()


@router.callback_query(AdminBookingCallback.filter())
async def admin_booking_action(
    callback: CallbackQuery,
    callback_data: AdminBookingCallback,
    settings: Settings,
    database: Database,
    booking_service: BookingService,
) -> None:
    if not _authorized(callback.from_user.id, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return
    booking = await database.get_booking(callback_data.booking_id)
    if booking is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    if callback_data.action == "details":
        await callback.message.answer(
            _booking_summary(booking, booking_service),
            reply_markup=admin_booking_actions(booking["id"], booking["status"]),
        )
        await callback.answer()
        return
    if callback_data.action not in {"confirmed", "completed", "cancelled"}:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    try:
        changed = await database.set_booking_status(
            callback_data.booking_id, callback_data.action
        )
    except SlotUnavailableError as error:
        await callback.answer(str(error), show_alert=True)
        return
    except BookingNotFoundError:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    if not changed:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    await callback.answer("Статус записи обновлён.")
    updated = await database.get_booking(callback_data.booking_id)
    if updated is not None:
        await callback.message.answer(
            _booking_summary(updated, booking_service),
            reply_markup=admin_booking_actions(updated["id"], updated["status"]),
        )


@router.message(AdminStates.closing_interval)
async def create_closed_interval(
    message: Message,
    settings: Settings,
    database: Database,
    booking_service: BookingService,
    state: FSMContext,
) -> None:
    if message.from_user is None or not _authorized(message.from_user.id, settings):
        await state.clear()
        await message.answer("Доступ запрещён.")
        return
    parts = [part.strip() for part in (message.text or "").split(";", maxsplit=2)]
    if len(parts) < 2:
        await message.answer(
            "Формат не распознан. Укажите дату/время и длительность через «;»."
        )
        return
    try:
        local_start = booking_service.parse_local_input(parts[0])
        duration = int(parts[1])
        if not 5 <= duration <= 1440:
            raise ValueError("Длительность должна быть от 5 до 1440 минут.")
        aware_start = localize_wall_time(local_start, settings.timezone)
        if aware_start is None:
            raise InvalidBookingTimeError(
                "Местное время не существует или неоднозначно из-за перевода часов."
            )
        if aware_start <= datetime.now(settings.timezone):
            raise ValueError("Интервал должен начинаться в будущем.")
        start_utc = format_utc(aware_start)
        end_utc_dt = aware_start.astimezone(UTC) + timedelta(minutes=duration)
        end_utc = format_utc(end_utc_dt)
        end_local = end_utc_dt.astimezone(settings.timezone)
        local_start_value = aware_start.astimezone(settings.timezone)
        closing_hour = settings.business_end
        closes = datetime.combine(
            local_start_value.date() + (timedelta(days=1) if closing_hour == 24 else timedelta()),
            time.min if closing_hour == 24 else time(hour=closing_hour),
        )
        if (
            local_start_value.date() < datetime.now(settings.timezone).date()
            or local_start_value.hour < settings.business_start
            or (
                closing_hour != 24
                and end_local.date() != local_start_value.date()
            )
            or end_local.replace(tzinfo=None) > closes
        ):
            raise ValueError("Интервал должен быть будущим и находиться в рабочем времени.")
        interval_id = await database.close_interval(
            start_utc,
            end_utc,
            parts[2] if len(parts) == 3 else "",
            message.from_user.id,
        )
    except (ValueError, InvalidBookingTimeError, SlotUnavailableError) as error:
        await message.answer(f"Интервал не закрыт: {error}")
        return

    await state.clear()
    await message.answer(
        f"Интервал №{interval_id} закрыт: "
        f"{booking_service.format_local(start_utc)} — "
        f"{booking_service.format_local(end_utc)}.",
        reply_markup=admin_menu_keyboard(),
    )
