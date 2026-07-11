"""Команды и FSM-визард настройки фильтров."""

from __future__ import annotations

import asyncio
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import db
from app.bot.keyboards import (
    BoolCB,
    BrandCB,
    FilterDeleteCB,
    ModelCB,
    SkipCB,
    brands_kb,
    filters_kb,
    models_kb,
    skip_kb,
    yes_no_kb,
)
from app.bot.messages import HELP_TEXT, START_TEXT, format_filter_line
from app.bot.states import AddFilter

router = Router()

YEAR_RANGE_RE = re.compile(r"^\s*(\d{4})(?:\s*[-–—]\s*(\d{4}))?\s*$")
MIN_YEAR, MAX_YEAR = 1950, 2035
MAX_MILEAGE_KM = 1_500_000
MAX_PRICE_USD = 5_000_000


def _parse_positive_int(text: str) -> int | None:
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits or len(digits) > 9:
        return None
    value = int(digits)
    return value if value > 0 else None


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено. /add_filter — начать заново.")


# --- /filters ---------------------------------------------------------------
# Зарегистрирован раньше шагов визарда, чтобы команда работала и внутри него.


def _filters_text(filters: list[db.FilterDTO]) -> str:
    lines = ["📋 Ваши фильтры:\n"]
    for index, item in enumerate(filters, start=1):
        line = format_filter_line(
            brand_name=item.brand_name,
            model_name=item.model_name,
            year_from=item.year_from,
            year_to=item.year_to,
            mileage_max_km=item.mileage_max_km,
            price_max_usd=item.price_max_usd,
            below_market_only=item.below_market_only,
        )
        lines.append(f"{index}. {line}")
    return "\n".join(lines)


@router.message(Command("filters"))
async def cmd_filters(message: Message) -> None:
    filters = await asyncio.to_thread(db.list_filters, message.from_user.id)
    if not filters:
        await message.answer("У вас пока нет фильтров. Создайте первый: /add_filter")
        return
    await message.answer(_filters_text(filters), reply_markup=filters_kb(filters))


@router.callback_query(FilterDeleteCB.filter())
async def delete_filter_cb(query: CallbackQuery, callback_data: FilterDeleteCB) -> None:
    deleted = await asyncio.to_thread(
        db.delete_filter, query.from_user.id, callback_data.filter_id
    )
    await query.answer("Фильтр удален" if deleted else "Фильтр уже удален")
    filters = await asyncio.to_thread(db.list_filters, query.from_user.id)
    if not filters:
        await query.message.edit_text("Фильтров не осталось. /add_filter — создать новый.")
        return
    await query.message.edit_text(_filters_text(filters), reply_markup=filters_kb(filters))


# --- Визард /add_filter -----------------------------------------------------


@router.message(Command("add_filter"))
async def cmd_add_filter(message: Message, state: FSMContext) -> None:
    await state.clear()
    brands = await asyncio.to_thread(db.list_brands)
    if not brands:
        await message.answer(
            "Каталог марок пока пуст — база еще наполняется. Попробуйте позже."
        )
        return
    await state.set_state(AddFilter.brand)
    await message.answer("Шаг 1/6. Выберите марку:", reply_markup=brands_kb(brands))


@router.callback_query(AddFilter.brand, BrandCB.filter())
async def wizard_brand(query: CallbackQuery, callback_data: BrandCB, state: FSMContext) -> None:
    # brand_id=0 - пункт "Все марки": фильтр без ограничения по марке и модели.
    if callback_data.brand_id == 0:
        await state.update_data(
            brand_id=None, brand_name=None, car_model_id=None, model_name=None
        )
        await state.set_state(AddFilter.years)
        await query.answer()
        await query.message.edit_text(
            "Выбрано: все марки.\n\n"
            "Шаг 3/6. Укажите годы выпуска, например `2015-2020` или `2018`:",
            reply_markup=skip_kb("years"),
        )
        return

    brands = await asyncio.to_thread(db.list_brands)
    brand = next((item for item in brands if item.id == callback_data.brand_id), None)
    if brand is None:
        await query.answer("Марка не найдена, начните заново: /add_filter", show_alert=True)
        await state.clear()
        return
    await state.update_data(brand_id=brand.id, brand_name=brand.name)
    await query.answer()

    models = await asyncio.to_thread(db.list_models, brand.id)
    if not models:
        await state.update_data(car_model_id=None, model_name=None)
        await state.set_state(AddFilter.years)
        await query.message.edit_text(
            f"Марка: {brand.name} (все модели).\n\n"
            "Шаг 3/6. Укажите годы выпуска, например `2015-2020` или `2018`:",
            reply_markup=skip_kb("years"),
        )
        return
    await state.set_state(AddFilter.model)
    await query.message.edit_text(
        f"Марка: {brand.name}.\n\nШаг 2/6. Выберите модель:",
        reply_markup=models_kb(models),
    )


@router.callback_query(AddFilter.model, ModelCB.filter())
async def wizard_model(query: CallbackQuery, callback_data: ModelCB, state: FSMContext) -> None:
    data = await state.get_data()
    model_name: str | None = None
    model_id: int | None = None
    if callback_data.model_id:
        models = await asyncio.to_thread(db.list_models, data["brand_id"])
        model = next((item for item in models if item.id == callback_data.model_id), None)
        if model is None:
            await query.answer("Модель не найдена, начните заново: /add_filter", show_alert=True)
            await state.clear()
            return
        model_id, model_name = model.id, model.name
    await state.update_data(car_model_id=model_id, model_name=model_name)
    await state.set_state(AddFilter.years)
    await query.answer()
    chosen = f"{data['brand_name']} {model_name}" if model_name else f"{data['brand_name']} (все модели)"
    await query.message.edit_text(
        f"Выбрано: {chosen}.\n\n"
        "Шаг 3/6. Укажите годы выпуска, например `2015-2020` или `2018`:",
        reply_markup=skip_kb("years"),
    )


@router.callback_query(AddFilter.years, SkipCB.filter(F.step == "years"))
async def wizard_years_skip(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(year_from=None, year_to=None)
    await state.set_state(AddFilter.mileage)
    await query.answer()
    await query.message.edit_text(
        "Шаг 4/6. Максимальный пробег в км, например `150000`:",
        reply_markup=skip_kb("mileage"),
    )


@router.message(AddFilter.years, F.text, ~F.text.startswith("/"))
async def wizard_years(message: Message, state: FSMContext) -> None:
    match = YEAR_RANGE_RE.match(message.text or "")
    if match is None:
        await message.answer(
            "Не понял 🤔 Напишите диапазон, например `2015-2020`, один год `2018` "
            "или нажмите «Пропустить».",
            reply_markup=skip_kb("years"),
        )
        return
    year_from = int(match.group(1))
    year_to = int(match.group(2)) if match.group(2) else year_from
    if not (MIN_YEAR <= year_from <= MAX_YEAR) or not (MIN_YEAR <= year_to <= MAX_YEAR):
        await message.answer(
            f"Год должен быть между {MIN_YEAR} и {MAX_YEAR}. Попробуйте еще раз:",
            reply_markup=skip_kb("years"),
        )
        return
    if year_from > year_to:
        year_from, year_to = year_to, year_from
    await state.update_data(year_from=year_from, year_to=year_to)
    await state.set_state(AddFilter.mileage)
    await message.answer(
        "Шаг 4/6. Максимальный пробег в км, например `150000`:",
        reply_markup=skip_kb("mileage"),
    )


@router.callback_query(AddFilter.mileage, SkipCB.filter(F.step == "mileage"))
async def wizard_mileage_skip(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(mileage_max_km=None)
    await state.set_state(AddFilter.price)
    await query.answer()
    await query.message.edit_text(
        "Шаг 5/6. Максимальная цена в долларах, например `20000`:",
        reply_markup=skip_kb("price"),
    )


@router.message(AddFilter.mileage, F.text, ~F.text.startswith("/"))
async def wizard_mileage(message: Message, state: FSMContext) -> None:
    value = _parse_positive_int(message.text or "")
    if value is None or value > MAX_MILEAGE_KM:
        await message.answer(
            "Нужно число в км, например `150000` (или «Пропустить»):",
            reply_markup=skip_kb("mileage"),
        )
        return
    await state.update_data(mileage_max_km=value)
    await state.set_state(AddFilter.price)
    await message.answer(
        "Шаг 5/6. Максимальная цена в долларах, например `20000`:",
        reply_markup=skip_kb("price"),
    )


@router.callback_query(AddFilter.price, SkipCB.filter(F.step == "price"))
async def wizard_price_skip(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(price_max_usd=None)
    await state.set_state(AddFilter.below_avg)
    await query.answer()
    await query.message.edit_text(
        "Шаг 6/6. Присылать только авто с ценой ниже средней по рынку?",
        reply_markup=yes_no_kb(),
    )


@router.message(AddFilter.price, F.text, ~F.text.startswith("/"))
async def wizard_price(message: Message, state: FSMContext) -> None:
    value = _parse_positive_int(message.text or "")
    if value is None or value > MAX_PRICE_USD:
        await message.answer(
            "Нужно число в долларах, например `20000` (или «Пропустить»):",
            reply_markup=skip_kb("price"),
        )
        return
    await state.update_data(price_max_usd=float(value))
    await state.set_state(AddFilter.below_avg)
    await message.answer(
        "Шаг 6/6. Присылать только авто с ценой ниже средней по рынку?",
        reply_markup=yes_no_kb(),
    )


@router.callback_query(AddFilter.below_avg, BoolCB.filter())
async def wizard_below_avg(query: CallbackQuery, callback_data: BoolCB, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    created = await asyncio.to_thread(
        db.create_filter,
        query.from_user.id,
        brand_id=data["brand_id"],
        car_model_id=data.get("car_model_id"),
        year_from=data.get("year_from"),
        year_to=data.get("year_to"),
        mileage_max_km=data.get("mileage_max_km"),
        price_max_usd=data.get("price_max_usd"),
        below_market_only=callback_data.value,
    )
    await query.answer("Фильтр сохранен")
    summary = format_filter_line(
        brand_name=created.brand_name,
        model_name=created.model_name,
        year_from=created.year_from,
        year_to=created.year_to,
        mileage_max_km=created.mileage_max_km,
        price_max_usd=created.price_max_usd,
        below_market_only=created.below_market_only,
    )
    await query.message.edit_text(
        f"✅ Фильтр создан:\n{summary}\n\n"
        "Как только появится подходящее объявление — пришлю его сюда.\n"
        "/filters — мои фильтры, /add_filter — еще один."
    )


# --- Fallback: неожиданный ввод внутри визарда ------------------------------
# Зарегистрированы последними: срабатывают, только если шаг выше не подошел
# (не тот тип сообщения, фото/стикер, неизвестная команда и т.п.).


@router.message(AddFilter.brand)
@router.message(AddFilter.model)
@router.message(AddFilter.below_avg)
async def wizard_expect_buttons(message: Message) -> None:
    await message.answer("Пожалуйста, выберите вариант кнопкой в сообщении выше. Отмена: /cancel")


@router.message(AddFilter.years)
async def wizard_years_fallback(message: Message) -> None:
    await message.answer(
        "Отправьте годы текстом, например `2015-2020`, или нажмите «Пропустить».",
        reply_markup=skip_kb("years"),
    )


@router.message(AddFilter.mileage)
async def wizard_mileage_fallback(message: Message) -> None:
    await message.answer(
        "Отправьте число в км, например `150000`, или нажмите «Пропустить».",
        reply_markup=skip_kb("mileage"),
    )


@router.message(AddFilter.price)
async def wizard_price_fallback(message: Message) -> None:
    await message.answer(
        "Отправьте число в долларах, например `20000`, или нажмите «Пропустить».",
        reply_markup=skip_kb("price"),
    )
