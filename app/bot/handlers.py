"""Команды и FSM-визард настройки фильтров."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import db
from app.bot.keyboards import (
    BlockCB,
    BlockDeleteCB,
    BlockMenuCB,
    BoolCB,
    BrandCB,
    FilterDeleteCB,
    ModelCB,
    PercentCB,
    RegionCB,
    SkipCB,
    TrialCB,
    block_choice_kb,
    blocks_kb,
    brands_kb,
    filters_kb,
    models_kb,
    pay_kb,
    percent_kb,
    region_kb,
    share_kb,
    skip_kb,
    trial_kb,
    yes_no_kb,
)
from app.bot.messages import (
    BLOCK_LIST_EMPTY_TEXT,
    DEMO_INTRO_TEXT,
    DEMO_UNAVAILABLE_TEXT,
    HELP_TEXT,
    SUBSCRIPTION_REQUIRED_TEXT,
    SUBSCRIPTION_SOON_TEXT,
    SUBSCRIPTION_UNAVAILABLE_TEXT,
    TRIAL_ALREADY_USED_TEXT,
    block_added_text,
    block_limit_text,
    blocks_text,
    duplicate_filter_text,
    filters_limit_text,
    format_filter_line,
    format_notification,
    invite_text,
    onboarding_text,
    referral_bonus_text,
    start_text,
    status_text,
    subscribe_error_text,
    subscribe_prompt,
    trial_started_text,
)
from app.bot.states import AddFilter
from app.core.config import get_settings
from app.services.blocklist import MAX_BLOCKS_PER_USER
from app.services.finik import init_payment
from app.services.regions import ALL_CARS_CODE, is_valid_region

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
async def cmd_start(message: Message, command: CommandObject | None = None) -> None:
    """Онбординг: приветствие → как работает → демо-лот → кнопка триала.

    Ветки: у кого доступ уже есть (член группы, подписчик, идущий триал) —
    обычное меню; кто триал ещё не брал — оффер с кнопкой; кто израсходовал —
    предложение оплаты. Deep-link `ref_<code>` разбирается здесь же.
    """
    settings = get_settings()
    info = await asyncio.to_thread(
        db.handle_start,
        message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        deep_link_payload=command.args if command else None,
    )

    if info.access_status == "member" or info.subscription_active or info.trial_active:
        await message.answer(start_text())
        return

    if info.trial_used or not settings.trial_enabled:
        # Триал израсходован (или выключен) — сразу к оплате.
        await message.answer(start_text())
        await message.answer(
            TRIAL_ALREADY_USED_TEXT
            if info.trial_used
            else SUBSCRIPTION_REQUIRED_TEXT
        )
        return

    await message.answer(onboarding_text(settings.trial_days))
    await _send_demo_listing(message)
    await message.answer(
        f"🎁 {settings.trial_days} дня бесплатно — без карты и без оплаты.",
        reply_markup=trial_kb(settings.trial_days),
    )


async def _send_demo_listing(message: Message) -> None:
    """Показывает реальное объявление ниже рынка — тем же форматом, что и рассылка."""
    demo = await asyncio.to_thread(db.get_demo_listing, message.from_user.id)
    if demo is None:
        await message.answer(DEMO_UNAVAILABLE_TEXT)
        return
    await message.answer(DEMO_INTRO_TEXT)
    await message.answer(format_notification(demo), disable_web_page_preview=False)


@router.callback_query(TrialCB.filter(F.action == "start"))
async def cb_trial_start(callback: CallbackQuery) -> None:
    settings = get_settings()
    if not settings.trial_enabled:
        await callback.answer("Бесплатный период сейчас недоступен", show_alert=True)
        return

    try:
        result = await asyncio.to_thread(db.activate_trial, callback.from_user.id)
    except LookupError:
        await callback.answer("Не удалось активировать. Отправьте /start ещё раз", show_alert=True)
        return

    if not result.ok:
        await callback.answer()
        await callback.message.answer(TRIAL_ALREADY_USED_TEXT)
        return

    await callback.answer()
    # Кнопку убираем: повторные нажатия всё равно ничего не дадут (триал
    # одноразовый), но выглядели бы как поломка.
    with suppress(TelegramAPIError):
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(trial_started_text(result.expires_at, settings.trial_days))

    if result.referrer_chat_id and result.referrer_bonus_days:
        await _notify_referral_bonus(
            callback.bot, result.referrer_chat_id, result.referrer_bonus_days
        )


async def _notify_referral_bonus(bot, chat_id: int, days: int) -> None:
    """Сообщает пригласившему о бонусе. Best-effort: он мог заблокировать бота."""
    with suppress(TelegramAPIError):
        await bot.send_message(chat_id, referral_bonus_text(days))


@router.message(Command("invite"))
async def cmd_invite(message: Message) -> None:
    settings = get_settings()
    if not settings.referral_enabled:
        await message.answer("Реферальная программа сейчас недоступна.")
        return
    if not settings.bot_username:
        # Без username бота deep-link не собрать — молча отдавать «ссылку» вида
        # t.me/None было бы хуже честного отказа.
        await message.answer("Реферальные ссылки временно недоступны.")
        return

    try:
        info = await asyncio.to_thread(db.get_invite_info, message.from_user.id)
    except LookupError:
        await message.answer("Отправьте /start, чтобы начать пользоваться ботом.")
        return

    link = f"https://t.me/{settings.bot_username.lstrip('@')}?start=ref_{info.invite_code}"
    await message.answer(
        invite_text(
            link=link,
            qualified=info.qualified,
            friends_to_next_bonus=info.friends_to_next_bonus,
            bonus_days=settings.referral_bonus_days,
            friends_per_bonus=settings.referral_friends_per_bonus,
            bonuses_granted=info.bonuses_granted,
            limit_reached=info.limit_reached,
        ),
        reply_markup=share_kb(link),
        disable_web_page_preview=True,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    settings = get_settings()
    if not settings.subscription_allowed_for(message.from_user.id):
        # Обкатка платежей на проде: контур включён и проверяется тестировщиком,
        # остальным честно говорим «скоро», а не «сломалось».
        await message.answer(
            SUBSCRIPTION_SOON_TEXT
            if settings.subscription_testing
            else SUBSCRIPTION_UNAVAILABLE_TEXT
        )
        return

    try:
        order_id = await asyncio.to_thread(
            db.create_pending_payment,
            message.from_user.id,
            amount=settings.subscription_price_kgs,
            currency="KGS",
            period_days=settings.subscription_period_days,
        )
    except LookupError:
        await message.answer(subscribe_error_text())
        return

    result = await asyncio.to_thread(
        init_payment,
        order_id=order_id,
        amount=settings.subscription_price_kgs,
        description=f"Подписка на {settings.subscription_period_days} дней",
    )
    if not result.ok or not result.redirect_url:
        await message.answer(subscribe_error_text())
        return

    price = int(settings.subscription_price_kgs)
    await message.answer(
        subscribe_prompt(price, settings.subscription_period_days),
        reply_markup=pay_kb(result.redirect_url, f"💳 Оплатить {price} сом"),
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    info = await asyncio.to_thread(db.get_subscription_info, message.from_user.id)
    if info is None:
        await message.answer("❌ Подписка неактивна. Оформить: /subscribe")
        return
    await message.answer(
        status_text(
            access_status=info.access_status,
            expires_at=info.expires_at,
            active=info.active,
            trial_active=info.trial_active,
            trial_expires_at=info.trial_expires_at,
        )
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено. /add_filter — начать заново.")


# --- /filters ---------------------------------------------------------------
# Зарегистрирован раньше шагов визарда, чтобы команда работала и внутри него.


def _filters_text(filters: list[db.FilterDTO]) -> str:
    limit = get_settings().bot_max_filters_per_user
    lines = [f"📋 Ваши фильтры ({len(filters)} из {limit}):\n"]
    for index, item in enumerate(filters, start=1):
        line = format_filter_line(
            brand_name=item.brand_name,
            model_name=item.model_name,
            year_from=item.year_from,
            year_to=item.year_to,
            mileage_max_km=item.mileage_max_km,
            price_max_usd=item.price_max_usd,
            below_market_only=item.below_market_only,
            below_market_percent=item.below_market_percent,
            skip_foreign=item.skip_foreign,
            region=item.region,
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


# --- Чёрный список ----------------------------------------------------------
# Запрет глобальный: действует поверх всех фильтров пользователя (matching.py).


@router.message(Command("blacklist"))
async def cmd_blacklist(message: Message) -> None:
    blocks = await asyncio.to_thread(db.list_blocks, message.from_user.id)
    if not blocks:
        await message.answer(BLOCK_LIST_EMPTY_TEXT)
        return
    await message.answer(blocks_text(blocks), reply_markup=blocks_kb(blocks))


@router.callback_query(BlockDeleteCB.filter())
async def block_delete_cb(query: CallbackQuery, callback_data: BlockDeleteCB) -> None:
    deleted = await asyncio.to_thread(db.delete_block, query.from_user.id, callback_data.block_id)
    await query.answer("Вернул в выдачу" if deleted else "Уже удалено")
    blocks = await asyncio.to_thread(db.list_blocks, query.from_user.id)
    if not blocks:
        await query.message.edit_text(BLOCK_LIST_EMPTY_TEXT)
        return
    await query.message.edit_text(blocks_text(blocks), reply_markup=blocks_kb(blocks))


@router.callback_query(BlockMenuCB.filter())
async def block_menu_cb(query: CallbackQuery, callback_data: BlockMenuCB) -> None:
    """Разворачивает/сворачивает меню прямо под карточкой.

    listing_id=0 — «Отмена»: возвращаем исходную одну кнопку, но по ней уже не
    восстановить listing_id, поэтому просто убираем клавиатуру.
    """
    if callback_data.listing_id == 0:
        await query.answer()
        with suppress(TelegramAPIError):
            await query.message.edit_reply_markup(reply_markup=None)
        return

    options = await asyncio.to_thread(db.block_options, callback_data.listing_id)
    if options is None:
        await query.answer("Объявление больше недоступно", show_alert=True)
        return
    await query.answer()
    with suppress(TelegramAPIError):
        await query.message.edit_reply_markup(
            reply_markup=block_choice_kb(
                callback_data.listing_id,
                brand_name=options.brand_name,
                model_name=options.model_name,
                city=options.city,
            )
        )


@router.callback_query(BlockCB.filter())
async def block_add_cb(query: CallbackQuery, callback_data: BlockCB) -> None:
    try:
        result = await asyncio.to_thread(
            db.add_block_from_listing,
            query.from_user.id,
            callback_data.listing_id,
            callback_data.kind,
        )
    except LookupError:
        await query.answer("Отправьте /start, чтобы начать пользоваться ботом", show_alert=True)
        return

    if result.limit_reached:
        await query.answer()
        await query.message.answer(block_limit_text(MAX_BLOCKS_PER_USER))
        return
    if result.unavailable:
        await query.answer("По этому объявлению так не получится", show_alert=True)
        return
    if result.already:
        await query.answer(f"Уже в чёрном списке: {result.label}")
    else:
        await query.answer(block_added_text(result.label), show_alert=True)
    # Меню сделало своё дело — убираем, чтобы карточка не мигала кнопками.
    with suppress(TelegramAPIError):
        await query.message.edit_reply_markup(reply_markup=None)


# --- Визард /add_filter -----------------------------------------------------


@router.message(Command("add_filter"))
async def cmd_add_filter(message: Message, state: FSMContext) -> None:
    await state.clear()
    quota = await asyncio.to_thread(db.filters_quota, message.from_user.id)
    if not quota.can_add:
        await message.answer(filters_limit_text(quota.used, quota.limit))
        return
    brands = await asyncio.to_thread(db.list_brands)
    if not brands:
        await message.answer(
            "Каталог марок пока пуст — база еще наполняется. Попробуйте позже."
        )
        return
    await state.set_state(AddFilter.brand)
    await message.answer("Шаг 1/7. Выберите марку:", reply_markup=brands_kb(brands))


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
            "Шаг 3/7. Укажите годы выпуска, например `2015-2020` или `2018`:",
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
            "Шаг 3/7. Укажите годы выпуска, например `2015-2020` или `2018`:",
            reply_markup=skip_kb("years"),
        )
        return
    await state.set_state(AddFilter.model)
    await query.message.edit_text(
        f"Марка: {brand.name}.\n\nШаг 2/7. Выберите модель:",
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
        "Шаг 3/7. Укажите годы выпуска, например `2015-2020` или `2018`:",
        reply_markup=skip_kb("years"),
    )


@router.callback_query(AddFilter.years, SkipCB.filter(F.step == "years"))
async def wizard_years_skip(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(year_from=None, year_to=None)
    await state.set_state(AddFilter.mileage)
    await query.answer()
    await query.message.edit_text(
        "Шаг 4/7. Максимальный пробег в км, например `150000`:",
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
        "Шаг 4/7. Максимальный пробег в км, например `150000`:",
        reply_markup=skip_kb("mileage"),
    )


@router.callback_query(AddFilter.mileage, SkipCB.filter(F.step == "mileage"))
async def wizard_mileage_skip(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(mileage_max_km=None)
    await state.set_state(AddFilter.price)
    await query.answer()
    await query.message.edit_text(
        "Шаг 5/7. Максимальная цена в долларах, например `20000`:",
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
        "Шаг 5/7. Максимальная цена в долларах, например `20000`:",
        reply_markup=skip_kb("price"),
    )


@router.callback_query(AddFilter.price, SkipCB.filter(F.step == "price"))
async def wizard_price_skip(query: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(price_max_usd=None)
    await state.set_state(AddFilter.below_avg)
    await query.answer()
    await query.message.edit_text(
        "Шаг 6/7. Присылать только авто с ценой ниже средней по рынку?",
        reply_markup=yes_no_kb("below_avg"),
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
        "Шаг 6/7. Присылать только авто с ценой ниже средней по рынку?",
        reply_markup=yes_no_kb("below_avg"),
    )


REGION_STEP_TEXT = (
    "Шаг 7/7. Из какого региона показывать авто?\n\n"
    "Выберите область, город или «Зарубежные авто». "
    "«Все авто» — без ограничения по региону.\n"
    "⚠️ У зарубежных цены обычно без растаможки и доставки — выглядят ниже рынка."
)


@router.callback_query(AddFilter.below_avg, BoolCB.filter(F.step == "below_avg"))
async def wizard_below_avg(query: CallbackQuery, callback_data: BoolCB, state: FSMContext) -> None:
    await state.update_data(below_market_only=callback_data.value)
    await query.answer()
    if callback_data.value:
        await state.set_state(AddFilter.below_percent)
        await query.message.edit_text(
            "Насколько ниже средней по рынку должна быть цена?",
            reply_markup=percent_kb("below_percent"),
        )
        return
    await state.update_data(below_market_percent=None)
    await state.set_state(AddFilter.region)
    await query.message.edit_text(REGION_STEP_TEXT, reply_markup=region_kb())


@router.callback_query(AddFilter.below_percent, PercentCB.filter(F.step == "below_percent"))
async def wizard_below_percent(
    query: CallbackQuery, callback_data: PercentCB, state: FSMContext
) -> None:
    await state.update_data(below_market_percent=callback_data.value)
    await state.set_state(AddFilter.region)
    await query.answer()
    await query.message.edit_text(REGION_STEP_TEXT, reply_markup=region_kb())


@router.callback_query(AddFilter.region, RegionCB.filter())
async def wizard_region(query: CallbackQuery, callback_data: RegionCB, state: FSMContext) -> None:
    region = None if callback_data.code == ALL_CARS_CODE else callback_data.code
    if not is_valid_region(region):
        await query.answer("Неизвестный регион, начните заново: /add_filter", show_alert=True)
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    try:
        created = await asyncio.to_thread(
            db.create_filter,
            query.from_user.id,
            brand_id=data["brand_id"],
            car_model_id=data.get("car_model_id"),
            year_from=data.get("year_from"),
            year_to=data.get("year_to"),
            mileage_max_km=data.get("mileage_max_km"),
            price_max_usd=data.get("price_max_usd"),
            below_market_only=data["below_market_only"],
            below_market_percent=data.get("below_market_percent"),
            region=region,
        )
    except db.FilterLimitError as exc:
        await query.answer()
        await query.message.edit_text(filters_limit_text(exc.used, exc.limit))
        return
    except db.DuplicateFilterError:
        await query.answer()
        await query.message.edit_text(duplicate_filter_text())
        return
    await query.answer("Фильтр сохранен")
    summary = format_filter_line(
        brand_name=created.brand_name,
        model_name=created.model_name,
        year_from=created.year_from,
        year_to=created.year_to,
        mileage_max_km=created.mileage_max_km,
        price_max_usd=created.price_max_usd,
        below_market_only=created.below_market_only,
        below_market_percent=created.below_market_percent,
        skip_foreign=created.skip_foreign,
        region=created.region,
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
@router.message(AddFilter.below_percent)
@router.message(AddFilter.region)
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
