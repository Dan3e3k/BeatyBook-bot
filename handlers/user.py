from __future__ import annotations

import logging
import re
from datetime import date, datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import Settings
from database import (
    BookingNotFoundError,
    Database,
    SlotUnavailableError,
)
from keyboards import (
    BOOK_BUTTON,
    MY_BOOKINGS_BUTTON,
    BookingFlowCallback,
    DateCallback,
    ServiceCallback,
    SlotCallback,
    UserBookingCallback,
    confirmation_keyboard,
    dates_keyboard,
    main_menu,
    services_keyboard,
    slots_keyboard,
    user_bookings_keyboard,
)
from services.booking_service import BookingService, InvalidBookingTimeError
from states import UserBookingStates


logger = logging.getLogger(__name__)
router = Router(name="user")
PHONE_PATTERN = re.compile(r"^[+0-9()\-\s]{6,32}$")


def _is_owner(user_id: int, settings: Settings) -> bool:
    return user_id == settings.owner_id


async def _begin_booking(
    message: Message,
    state: FSMContext,
    database: Database,
) -> None:
    services = await database.list_services()
    if not services:
        await message.answer("Сейчас нет доступных услуг. Попробуйте позже.")
        return
    await state.clear()
    await state.set_state(UserBookingStates.choosing_service)
    await message.answer("Выберите услугу:", reply_markup=services_keyboard(services))


@router.message(CommandStart())
async def start_command(
    message: Message, settings: Settings, state: FSMContext
) -> None:
    if message.from_user is None:
        return
    await state.clear()
    await message.answer(
        "Здравствуйте! Я помогу записаться в салон красоты. Выберите действие:",
        reply_markup=main_menu(is_owner=_is_owner(message.from_user.id, settings)),
    )


@router.message(Command("help"))
async def help_command(message: Message, settings: Settings) -> None:
    if message.from_user is None:
        return
    text = (
        "Чтобы записаться, нажмите «Записаться», выберите услугу и время, "
        "затем укажите имя и телефон. В разделе «Мои записи» можно посмотреть "
        "и отменить свои будущие визиты."
    )
    await message.answer(
        text,
        reply_markup=main_menu(is_owner=_is_owner(message.from_user.id, settings)),
    )


@router.message(Command("cancel"))
async def cancel_dialog(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Текущий сценарий отменён.")


@router.message(Command("mybookings"))
@router.message(F.text == MY_BOOKINGS_BUTTON)
async def show_my_bookings(
    message: Message, database: Database, booking_service: BookingService
) -> None:
    if message.from_user is None:
        return
    bookings = await database.list_user_bookings(message.from_user.id)
    if not bookings:
        await message.answer("У вас пока нет предстоящих записей.")
        return
    lines = ["Ваши будущие записи:"]
    for booking in bookings:
        lines.append(
            f"№{booking['id']} · {booking['service_name']} · "
            f"{booking_service.format_local(booking['start_utc'])}"
        )
    await message.answer(
        "\n".join(lines),
        reply_markup=user_bookings_keyboard(bookings),
    )


@router.message(Command("book"))
@router.message(F.text == BOOK_BUTTON)
async def start_booking(
    message: Message,
    state: FSMContext,
    database: Database,
) -> None:
    await _begin_booking(message, state, database)


@router.callback_query(ServiceCallback.filter())
async def choose_service(
    callback: CallbackQuery,
    callback_data: ServiceCallback,
    state: FSMContext,
    database: Database,
    booking_service: BookingService,
) -> None:
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    service = await database.get_service(callback_data.service_id)
    if service is None:
        await callback.answer("Услуга больше недоступна.", show_alert=True)
        return
    await state.update_data(service_id=service["id"])
    await state.set_state(UserBookingStates.choosing_date)
    await callback.message.answer(
        f"Вы выбрали «{service['name']}». Теперь выберите дату:",
        reply_markup=dates_keyboard(service["id"], booking_service.available_dates()),
    )
    await callback.answer()


@router.callback_query(DateCallback.filter())
async def choose_date(
    callback: CallbackQuery,
    callback_data: DateCallback,
    state: FSMContext,
    booking_service: BookingService,
) -> None:
    if callback.message is None:
        await callback.answer()
        return
    data = await state.get_data()
    if (
        await state.get_state() != UserBookingStates.choosing_date.state
        or data.get("service_id") != callback_data.service_id
    ):
        await callback.answer("Начните запись заново через меню.", show_alert=True)
        return
    try:
        selected_date = date.fromisoformat(callback_data.selected_date)
    except ValueError:
        await callback.answer("Некорректная дата.", show_alert=True)
        return
    slots = await booking_service.available_slots(callback_data.service_id, selected_date)
    if not slots:
        await callback.answer("На эту дату свободного времени нет.", show_alert=True)
        return
    await state.update_data(selected_date=selected_date.isoformat())
    await state.set_state(UserBookingStates.choosing_time)
    await callback.message.answer(
        f"Свободное время на {selected_date.strftime('%d.%m.%Y')}:",
        reply_markup=slots_keyboard(callback_data.service_id, selected_date, slots),
    )
    await callback.answer()


@router.callback_query(SlotCallback.filter())
async def choose_time(
    callback: CallbackQuery,
    callback_data: SlotCallback,
    state: FSMContext,
    booking_service: BookingService,
    database: Database,
) -> None:
    if callback.message is None:
        await callback.answer()
        return
    data = await state.get_data()
    if (
        await state.get_state() != UserBookingStates.choosing_time.state
        or data.get("service_id") != callback_data.service_id
        or data.get("selected_date") != callback_data.selected_date
    ):
        await callback.answer("Выбор устарел. Начните запись заново.", show_alert=True)
        return
    try:
        selected_date = date.fromisoformat(callback_data.selected_date)
        selected_time = datetime.strptime(callback_data.selected_time, "%H:%M").time()
    except ValueError:
        await callback.answer("Некорректное время.", show_alert=True)
        return
    available = await booking_service.available_slots(callback_data.service_id, selected_date)
    if not any(slot.strftime("%H:%M") == callback_data.selected_time for slot in available):
        await callback.answer("Это время уже недоступно.", show_alert=True)
        return
    local_start = datetime.combine(selected_date, selected_time)
    await state.update_data(local_start=local_start.isoformat())
    await state.set_state(UserBookingStates.entering_name)
    await callback.message.answer("Как вас зовут?")
    await callback.answer()


@router.message(UserBookingStates.entering_name)
async def enter_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 100:
        await message.answer("Введите имя длиной от 2 до 100 символов.")
        return
    await state.update_data(customer_name=name)
    await state.set_state(UserBookingStates.entering_phone)
    await message.answer("Укажите номер телефона для связи.")


@router.message(UserBookingStates.entering_phone)
async def enter_phone(message: Message, state: FSMContext, booking_service: BookingService) -> None:
    phone = (message.text or "").strip()
    digits = sum(character.isdigit() for character in phone)
    if not PHONE_PATTERN.fullmatch(phone) or not 6 <= digits <= 15:
        await message.answer("Проверьте номер: допустимы цифры, пробелы, «+», скобки и дефисы.")
        return
    data = await state.get_data()
    service = next(
        (
            item
            for item in await booking_service.database.list_services()
            if item["id"] == data.get("service_id")
        ),
        None,
    )
    if service is None or not data.get("local_start"):
        await state.clear()
        await message.answer("Сеанс записи устарел. Начните запись заново.")
        return
    local_start = datetime.fromisoformat(data["local_start"])
    await state.update_data(phone=phone)
    await state.set_state(UserBookingStates.confirming)
    await message.answer(
        "Проверьте данные записи:\n"
        f"Услуга: {service['name']}\n"
        f"Дата и время: {local_start.strftime('%d.%m.%Y в %H:%M')}\n"
        f"Имя: {data['customer_name']}\n"
        f"Телефон: {phone}\n\n"
        "Подтвердить запись?",
        reply_markup=confirmation_keyboard(),
    )


@router.callback_query(BookingFlowCallback.filter())
async def confirm_or_abort_booking(
    callback: CallbackQuery,
    callback_data: BookingFlowCallback,
    state: FSMContext,
    booking_service: BookingService,
    settings: Settings,
    bot: Bot,
) -> None:
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    if await state.get_state() != UserBookingStates.confirming.state:
        await callback.answer("Сеанс записи завершён. Начните заново.", show_alert=True)
        return
    if callback_data.action == "cancel":
        await state.clear()
        await callback.message.answer("Запись не создана.")
        await callback.answer()
        return
    data = await state.get_data()
    try:
        local_start = datetime.fromisoformat(data["local_start"])
        service = await booking_service.database.get_service(int(data["service_id"]))
        if service is None:
            raise BookingNotFoundError("Выбранная услуга больше недоступна.")
        booking_id = await booking_service.create_booking(
            user_id=callback.from_user.id,
            customer_name=data["customer_name"],
            phone=data["phone"],
            service_id=int(data["service_id"]),
            local_start=local_start,
        )
    except SlotUnavailableError:
        await state.clear()
        await callback.message.answer(
            "Пока вы заполняли данные, время заняли. Пожалуйста, начните запись заново "
            "и выберите другой слот."
        )
        await callback.answer("Время уже занято.", show_alert=True)
        return
    except (InvalidBookingTimeError, BookingNotFoundError, KeyError, ValueError) as error:
        await state.clear()
        await callback.message.answer(f"Не удалось создать запись: {error}")
        await callback.answer("Запись не создана.", show_alert=True)
        return

    await state.clear()
    local_time = local_start.strftime("%d.%m.%Y в %H:%M")
    confirmation = f"Готово! Ваша запись №{booking_id} создана. Салон ждёт вас {local_time}."
    await callback.message.answer(confirmation)
    notification = (
        f"Новая запись №{booking_id}\n"
        f"Клиент: {data['customer_name']}\n"
        f"Телефон: {data['phone']}\n"
        f"Услуга: {service['name']}\n"
        f"Время: {local_time}"
    )
    try:
        await bot.send_message(settings.owner_id, notification)
    except TelegramAPIError:
        logger.exception("Не удалось уведомить владельца о записи #%s", booking_id)
        await callback.message.answer(
            "Запись сохранена, но Telegram не доставил уведомление администратору. "
            "Данные доступны в админ-панели."
        )
    await callback.answer("Запись создана.")


@router.callback_query(UserBookingCallback.filter())
async def cancel_own_booking(
    callback: CallbackQuery,
    callback_data: UserBookingCallback,
    database: Database,
    booking_service: BookingService,
) -> None:
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    if callback_data.action != "cancel":
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    cancelled = await database.cancel_user_booking(
        callback_data.booking_id, callback.from_user.id
    )
    if not cancelled:
        await callback.answer(
            "Запись не найдена, уже отменена или её нельзя отменить.", show_alert=True
        )
        return
    await callback.answer("Запись отменена.")
    await callback.message.answer("Запись отменена. Другие будущие записи:")
    bookings = await database.list_user_bookings(callback.from_user.id)
    if bookings:
        lines = [
            f"№{booking['id']} · {booking['service_name']} · "
            f"{booking_service.format_local(booking['start_utc'])}"
            for booking in bookings
        ]
        await callback.message.answer(
            "\n".join(lines),
            reply_markup=user_bookings_keyboard(bookings),
        )
    else:
        await callback.message.answer("Предстоящих записей больше нет.")
