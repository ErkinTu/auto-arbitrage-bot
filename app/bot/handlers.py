"""Команды и FSM-визард настройки фильтров."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InputMediaPhoto, Message

from app.bot import db, photos
from app.bot.keyboards import (
    PhotosCB,
    PlayCB,
    BlockCB,
    BlockDeleteCB,
    BestCB,
    BlockMenuCB,
    BoolCB,
    BrandCB,
    DigestCB,
    FilterDeleteCB,
    HideCB,
    ModelCB,
    CUSTOM_PRESET,
    PercentCB,
    PlanCB,
    PresetCB,
    RegionCB,
    SkipCB,
    TrialCB,
    UnhideCB,
    best_more_kb,
    block_choice_kb,
    blocks_kb,
    brands_kb,
    card_kb,
    has_photos_button,
    digest_kb,
    hidden_kb,
    markup_url,
    more_rows,
    with_rows,
    without_best_rows,
    without_digest_rows,
    filters_kb,
    models_kb,
    pay_kb,
    percent_kb,
    plans_kb,
    presets_kb,
    region_kb,
    share_kb,
    skip_kb,
    trial_kb,
    yes_no_kb,
)
from app.bot.messages import (
    pause_already_text,
    pause_on_text,
    play_already_text,
    play_on_text,
    BLOCK_LIST_EMPTY_TEXT,
    DIGEST_EMPTY_TEXT,
    DIGEST_EXPIRED_TEXT,
    DIGEST_GONE_TEXT,
    DEMO_UNAVAILABLE_TEXT,
    FILTERS_PRIORITY_NOTE,
    HELP_TEXT,
    PRESETS_PROMPT_TEXT,
    SHOWCASE_EMPTY_TEXT,
    SHOWCASE_INTRO_TEXT,
    START_SHOWCASE_INTRO_TEXT,
    START_TRIAL_PROMPT_TEXT,
    SUBSCRIPTION_MEMBER_TEXT,
    SUBSCRIPTION_REQUIRED_TEXT,
    SUBSCRIPTION_SOON_TEXT,
    SUBSCRIPTION_UNAVAILABLE_TEXT,
    admin_login_text,
    block_added_text,
    block_limit_text,
    blocks_text,
    duplicate_filter_text,
    filter_created_text,
    filters_limit_text,
    format_filter_line,
    invite_text,
    onboarding_text,
    preview_intro_text,
    referral_bonus_text,
    plan_confirm_text,
    start_text,
    status_text,
    subscribe_error_text,
    subscribe_horizon_text,
    subscribe_menu_text,
    trial_already_used_text,
    trial_started_text,
)
from app.bot.matching import (
    PRIORITY_ALL_CARS,
    PRIORITY_BELOW_MARKET,
    PRIORITY_SPECIFIC,
)
from app.bot.menu import (
    LEGACY_MENU_HELP,
    LEGACY_MENU_SUBSCRIBE,
    MENU_ACCESS,
    MENU_ADD_FILTER,
    MENU_BEST,
    MENU_FILTERS,
    main_menu_kb,
)
from app.bot.notifier import send_notifications
from app.bot.showcase import mark_seen, seen_ids
from app.bot.states import AddFilter
from app.core.config import get_settings
from app.services.blocklist import MAX_BLOCKS_PER_USER
from app.services.filter_presets import base_preset, find_preset
from app.services.finik import init_payment
from app.services.plans import find_plan
from app.services.regions import ALL_CARS_CODE, is_valid_region

logger = logging.getLogger(__name__)

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
    """Онбординг: приветствие → как работает → витрина (3 карточки) → кнопка триала.

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

    # Постоянное меню едет с первым же сообщением: reply-клавиатура держится в
    # чате, пока её не заменят, так что повторять её в каждом ответе незачем.
    # Витрина — всем и при каждом /start (решение владельца 22.10.2026): три
    # живые карточки объясняют бота лучше любого текста.
    if info.access_status == "member" or info.subscription_active or info.trial_active:
        await message.answer(start_text(), reply_markup=main_menu_kb())
        await _send_start_showcase(message)
        return

    if info.trial_used or not settings.trial_enabled:
        # Триал израсходован (или выключен) — витрина и сразу к оплате.
        payments_open = settings.subscription_allowed_for(message.from_user.id)
        await message.answer(start_text(), reply_markup=main_menu_kb())
        await _send_start_showcase(message)
        await message.answer(
            trial_already_used_text(payments_open)
            if info.trial_used
            else SUBSCRIPTION_REQUIRED_TEXT
        )
        return

    await message.answer(onboarding_text(settings.trial_days), reply_markup=main_menu_kb())
    shown = await _send_start_showcase(message)
    # Кнопка триала — отдельным сообщением после карточек: под каждой карточкой
    # свои кнопки («Не то», «Открыть»), а без витрины — под честным «примера нет».
    await message.answer(
        START_TRIAL_PROMPT_TEXT if shown else DEMO_UNAVAILABLE_TEXT,
        reply_markup=trial_kb(settings.trial_days),
    )


async def _send_start_showcase(message: Message) -> bool:
    """Первая страница витрины (3 карточки) в /start; False — показать нечего.

    Не пишется в sent_notifications — как и /best: это не рассылка. Первое
    место витрины — бывший «демо-лот». Ошибка витрины /start не ломает.
    """
    settings = get_settings()
    try:
        page, _ = await asyncio.to_thread(db.get_showcase, message.chat.id, 0)
    except Exception:
        logger.exception("start showcase failed for chat %s", message.chat.id)
        return False
    if not page:
        return False
    await message.answer(START_SHOWCASE_INTRO_TEXT)
    await send_notifications(
        message.bot,
        page,
        rich_cards=settings.bot_rich_cards_per_cycle,
        interactive=True,
    )
    mark_seen(message.chat.id, [note.listing_id for note in page])
    return True


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
        await callback.message.answer(
            trial_already_used_text(settings.subscription_allowed_for(callback.from_user.id))
        )
        return

    await callback.answer()
    # Кнопку убираем: повторные нажатия всё равно ничего не дадут (триал
    # одноразовый), но выглядели бы как поломка.
    with suppress(TelegramAPIError):
        await callback.message.edit_reply_markup(reply_markup=None)
    # Та же кнопка ставит базовый фильтр — без подтверждения (решение владельца
    # 21.10.2026): триал без фильтра — это три дня тишины, у 23% так и вышло.
    # Если свои фильтры уже есть, их не трогаем.
    base_filter = await asyncio.to_thread(db.ensure_base_filter, callback.from_user.id)
    await callback.message.answer(
        trial_started_text(
            result.expires_at,
            settings.trial_days,
            base_filter=base_preset().summary if base_filter else None,
        ),
        reply_markup=main_menu_kb(),
    )
    if base_filter is not None:
        await _send_filter_preview(callback.message, base_filter.id)

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
@router.message(F.text == LEGACY_MENU_HELP)
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("subscribe"))
@router.message(F.text == LEGACY_MENU_SUBSCRIBE)
async def cmd_subscribe(message: Message) -> None:
    info = await asyncio.to_thread(db.get_subscription_info, message.from_user.id)
    await _send_plans(message, info)


@router.message(F.text == MENU_ACCESS)
async def menu_access(message: Message) -> None:
    """«💎 Доступ»: сначала что есть сейчас (статус), сразу под ним — тарифы.

    Работает и без доступа (в `access.py` кнопка = входная команда subscribe):
    тому, у кого триал кончился, экран обязан вести к оплате, а не в тупик.
    """
    info = await asyncio.to_thread(db.get_subscription_info, message.from_user.id)
    if info is not None and info.access_status != "member":
        await message.answer(_status_text(info))
    await _send_plans(message, info)


async def _send_plans(message: Message, info) -> None:
    settings = get_settings()
    if info is not None and info.access_status == "member":
        # Участнику группы платить нечем и незачем — до вайтлиста оплаты не
        # доходим, иначе он получил бы «оплата скоро», хотя доступ у него есть.
        await message.answer(SUBSCRIPTION_MEMBER_TEXT)
        return

    if not settings.subscription_allowed_for(message.from_user.id):
        # Обкатка платежей на проде: контур включён и проверяется тестировщиком,
        # остальным честно говорим «скоро», а не «сломалось».
        await message.answer(
            SUBSCRIPTION_SOON_TEXT
            if settings.subscription_testing
            else SUBSCRIPTION_UNAVAILABLE_TEXT
        )
        return

    # Первый шаг — сетка тарифов: условия человек узнаёт ДО оплаты, а точные
    # даты — на экране подтверждения (второй шаг, cb_plan_selected).
    await message.answer(subscribe_menu_text(), reply_markup=plans_kb())


@router.callback_query(PlanCB.filter())
async def cb_plan_selected(callback: CallbackQuery, callback_data: PlanCB) -> None:
    """Второй шаг /subscribe: выбранный тариф → даты по расписанию → ссылка Finik."""
    settings = get_settings()
    plan = find_plan(callback_data.code)
    if plan is None:
        # Кнопка из истории чата после смены сетки: молча вести на оплату не того
        # тарифа нельзя, поэтому честный отказ и свежее меню.
        await callback.answer("Такого тарифа больше нет", show_alert=True)
        await callback.message.answer(subscribe_menu_text(), reply_markup=plans_kb())
        return
    if not settings.subscription_allowed_for(callback.from_user.id):
        await callback.answer()
        await callback.message.answer(
            SUBSCRIPTION_SOON_TEXT
            if settings.subscription_testing
            else SUBSCRIPTION_UNAVAILABLE_TEXT
        )
        return

    await callback.answer()
    try:
        draft = await asyncio.to_thread(
            db.create_pending_payment, callback.from_user.id, plan_code=plan.code
        )
    except LookupError:
        await callback.message.answer(subscribe_error_text())
        return

    if not draft.ok:
        # Отказ по горизонту: доступ уже оплачен слишком далеко вперёд. Говорим
        # это ДО платежа — «деньги взяли, дни не встали» повторять нельзя.
        await callback.message.answer(
            subscribe_horizon_text(draft.horizon_until, settings.subscription_max_horizon_days)
        )
        return

    result = await asyncio.to_thread(
        init_payment,
        order_id=draft.order_id,
        amount=plan.price_kgs,
        description=f"Доступ: {plan.title}",
    )
    if not result.ok or not result.redirect_url:
        await callback.message.answer(subscribe_error_text())
        return

    await callback.message.answer(
        plan_confirm_text(plan, draft),
        reply_markup=pay_kb(result.redirect_url, f"💳 Оплатить {plan.price} сом"),
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    info = await asyncio.to_thread(db.get_subscription_info, message.from_user.id)
    if info is None:
        await message.answer("❌ Подписка неактивна. Оформить: /subscribe")
        return
    await message.answer(_status_text(info))


def _status_text(info) -> str:
    return status_text(
        access_status=info.access_status,
        expires_at=info.expires_at,
        active=info.active,
        trial_active=info.trial_active,
        trial_expires_at=info.trial_expires_at,
        schedule=info.schedule,
        tier=info.tier,
        paused_at=info.paused_at,
    )


NOT_STARTED_TEXT = "Сначала отправьте /start."


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    """Пауза пушей до /play (решение владельца 31.10.2026: без сроков)."""
    result = await asyncio.to_thread(db.set_paused, message.from_user.id, True)
    if result is None:
        await message.answer(NOT_STARTED_TEXT)
    elif result.changed:
        await message.answer(pause_on_text())
    else:
        await message.answer(pause_already_text(result.paused_at))


async def _resume(telegram_id: int) -> str:
    result = await asyncio.to_thread(db.set_paused, telegram_id, False)
    if result is None:
        return NOT_STARTED_TEXT
    return play_on_text() if result.changed else play_already_text()


@router.message(Command("play"))
async def cmd_play(message: Message) -> None:
    await message.answer(await _resume(message.from_user.id))


@router.callback_query(PlayCB.filter(F.action == "resume"))
async def cb_play(callback: CallbackQuery) -> None:
    """Кнопка под напоминанием о паузе. Кнопку снимаем: второе нажатие не нужно."""
    text = await _resume(callback.from_user.id)
    await callback.answer()
    if callback.message is not None:
        with suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(text)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено. /add_filter — начать заново.")


# --- /filters ---------------------------------------------------------------
# Зарегистрирован раньше шагов визарда, чтобы команда работала и внутри него.


def _filter_summary(item: db.FilterDTO) -> str:
    return format_filter_line(
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
        brand_group=item.brand_group,
        max_age_years=item.max_age_years,
    )


def _filters_text(filters: list[db.FilterDTO], *, note: bool = False) -> str:
    limit = get_settings().bot_max_filters_per_user
    lines = [f"📋 Ваши фильтры ({len(filters)} из {limit}):\n"]
    for index, item in enumerate(filters, start=1):
        lines.append(f"{index}. {_filter_summary(item)}")
    if note:
        lines.append(f"\n{FILTERS_PRIORITY_NOTE}")
    return "\n".join(lines)


def _priority_level(item: db.FilterDTO) -> int:
    """Уровень фильтра — зеркало `matching.filter_priority`, но по DTO."""
    if item.below_market_only:
        return PRIORITY_BELOW_MARKET
    if item.brand_name is not None or item.model_name is not None or item.brand_group:
        return PRIORITY_SPECIFIC
    return PRIORITY_ALL_CARS


# Сноску про порядок показываем не чаще раза в сутки на чат: при каждом
# /filters она превращается в шум, который перестают читать. Память процесса,
# а не БД: после рестарта бота лишний раз показать подсказку не страшно.
PRIORITY_NOTE_INTERVAL_SECONDS = 24 * 3600
_priority_note_shown: dict[int, float] = {}


def _should_show_priority_note(chat_id: int, filters: list[db.FilterDTO]) -> bool:
    if len({_priority_level(item) for item in filters}) < 2:
        return False  # один уровень — порядок ничего не меняет
    now = time.monotonic()
    shown_at = _priority_note_shown.get(chat_id)
    if shown_at is not None and now - shown_at < PRIORITY_NOTE_INTERVAL_SECONDS:
        return False
    _priority_note_shown[chat_id] = now
    return True


@router.message(Command("filters"))
@router.message(F.text == MENU_FILTERS)
async def cmd_filters(message: Message) -> None:
    filters = await asyncio.to_thread(db.list_filters, message.from_user.id)
    if not filters:
        await message.answer(
            "У вас пока нет фильтров — поэтому я молчу.\n\n" + PRESETS_PROMPT_TEXT,
            reply_markup=presets_kb(),
        )
        return
    note = _should_show_priority_note(message.chat.id, filters)
    await message.answer(_filters_text(filters, note=note), reply_markup=filters_kb(filters))


@router.callback_query(FilterDeleteCB.filter())
async def delete_filter_cb(query: CallbackQuery, callback_data: FilterDeleteCB) -> None:
    deleted = await asyncio.to_thread(
        db.delete_filter, query.from_user.id, callback_data.filter_id
    )
    await query.answer("Фильтр удален" if deleted else "Фильтр уже удален")
    filters = await asyncio.to_thread(db.list_filters, query.from_user.id)
    if not filters:
        await query.message.edit_text(
            "Фильтров не осталось — без них я молчу.\n\n" + PRESETS_PROMPT_TEXT,
            reply_markup=presets_kb(),
        )
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


@router.callback_query(HideCB.filter())
async def hide_cb(query: CallbackQuery, callback_data: HideCB) -> None:
    """«🚫 Не то»: одним тапом скрыть модель и сразу дать «Вернуть».

    Клавиатуру карточки заменяем на «↩️ Вернуть …» + прежнюю ссылку: подпись
    к фото edit_text не меняется, а кнопки — одинаково и у фото, и у текста.
    """
    try:
        result = await asyncio.to_thread(
            db.hide_from_listing, query.from_user.id, callback_data.listing_id
        )
    except LookupError:
        await query.answer("Отправьте /start, чтобы начать пользоваться ботом", show_alert=True)
        return

    if result.limit_reached:
        await query.answer()
        await query.message.answer(block_limit_text(MAX_BLOCKS_PER_USER))
        return
    if result.unavailable or result.block_id is None:
        await query.answer("По этому объявлению так не получится", show_alert=True)
        return
    await query.answer(f"Скрыто: {result.label}. Больше не покажу")
    # Кнопка дайджеста могла переехать под эту карточку — не теряем её.
    with suppress(TelegramAPIError):
        await query.message.edit_reply_markup(
            reply_markup=with_rows(
                hidden_kb(
                    result.label,
                    result.block_id,
                    callback_data.listing_id,
                    markup_url(query.message.reply_markup),
                    photos=has_photos_button(query.message.reply_markup),
                ),
                more_rows(query.message.reply_markup),
            )
        )


@router.callback_query(UnhideCB.filter())
async def unhide_cb(query: CallbackQuery, callback_data: UnhideCB) -> None:
    """«↩️ Вернуть»: снять запрет и вернуть карточке кнопки «Не то»/«Открыть»."""
    deleted = await asyncio.to_thread(db.delete_block, query.from_user.id, callback_data.block_id)
    await query.answer("Вернул в выдачу" if deleted else "Уже возвращено")
    url = markup_url(query.message.reply_markup)
    had_photos = has_photos_button(query.message.reply_markup)
    with suppress(TelegramAPIError):
        await query.message.edit_reply_markup(
            reply_markup=with_rows(
                card_kb(callback_data.listing_id, url, photos=had_photos)
                if url
                else None,
                more_rows(query.message.reply_markup),
            )
        )


PHOTOS_GONE_TEXT = "Объявление снято — фото больше недоступны."
PHOTOS_MISSING_TEXT = "Других фото у меня нет — все фото на сайте объявления."
PHOTOS_FAILED_TEXT = "Не удалось загрузить фото с сайта — откройте объявление."


@router.callback_query(PhotosCB.filter())
async def photos_cb(query: CallbackQuery, callback_data: PhotosCB) -> None:
    """«📷 Все фото»: альбом(ы) ответом на карточку.

    Галерея — из базы; нет — у lalafo один запрос карточки (и запись в базу,
    второе нажатие бесплатное). Двойной тап гасит дебаунс.
    """
    listing_id = callback_data.listing_id
    if not photos.debounce(query.from_user.id, listing_id):
        await query.answer("Уже отправляю")
        return
    await query.answer("Загружаю фото…")
    gallery = await asyncio.to_thread(db.listing_gallery, listing_id)
    if gallery is None:
        await query.message.answer(PHOTOS_GONE_TEXT)
        return

    urls = gallery.urls
    if not urls and photos.can_fetch(gallery.source_name):
        try:
            urls = await asyncio.to_thread(
                photos.fetch_gallery, gallery.source_name, gallery.external_id
            )
        except photos.ListingGone:
            await query.message.answer(PHOTOS_GONE_TEXT)
            return
        except Exception:
            logger.exception("photos: card fetch failed for listing %s", listing_id)
            urls = []
        if urls:
            await asyncio.to_thread(db.save_gallery, listing_id, urls)
    if len(urls) <= 1:
        await query.message.answer(
            PHOTOS_MISSING_TEXT, reply_markup=pay_kb(gallery.url, "Открыть ↗")
        )
        return

    reply_to = query.message.message_id
    try:
        for album in photos.albums(urls):
            if len(album) == 1:  # альбом — минимум 2 фото
                await query.message.answer_photo(album[0], reply_to_message_id=reply_to)
                continue
            await query.message.answer_media_group(
                [InputMediaPhoto(media=url) for url in album], reply_to_message_id=reply_to
            )
    except TelegramAPIError as error:
        # Чаще всего — фото удалили с CDN или Telegram не смог его скачать.
        logger.warning("photos: album for listing %s rejected: %s", listing_id, error)
        await query.message.answer(
            PHOTOS_FAILED_TEXT, reply_markup=pay_kb(gallery.url, "Открыть ↗")
        )


@router.callback_query(DigestCB.filter())
async def digest_more_cb(query: CallbackQuery, callback_data: DigestCB) -> None:
    """«📷 Показать с фото»: следующие 3 карточки, кнопка переезжает под последнюю.

    Кнопку снимаем с нажатого сообщения (сам список или прошлая карточка) ДО
    отправки: двойное нажатие иначе прислало бы те же карточки дважды. Новые
    карточки уходят с ней же строкой под последней — кнопка всегда внизу чата,
    а не над присланным. Последняя порция её не несёт; список устарел или всё
    снято — честно говорим.
    """
    page = await asyncio.to_thread(
        db.digest_page, query.from_user.id, callback_data.batch, callback_data.offset
    )
    with suppress(TelegramAPIError):
        await query.message.edit_reply_markup(
            reply_markup=without_digest_rows(query.message.reply_markup)
        )
    if page.status != db.DIGEST_OK:
        text = {
            db.DIGEST_EXPIRED: DIGEST_EXPIRED_TEXT,
            db.DIGEST_GONE: DIGEST_GONE_TEXT,
        }.get(page.status, DIGEST_EMPTY_TEXT)
        await query.answer(text, show_alert=True)
        return

    await query.answer()
    await send_notifications(
        query.message.bot,
        page.notes,
        rich_cards=len(page.notes),
        tail_markup=digest_kb(callback_data.batch, page.next_offset, page.remaining)
        if page.next_offset is not None
        else None,
        interactive=True,
    )


# --- Витрина «лучшие сейчас» -------------------------------------------------


@router.message(Command("best"))
@router.message(F.text == MENU_BEST)
async def cmd_best(message: Message) -> None:
    # fresh: повторное нажатие даёт следующие машины, а не те же три.
    await _send_showcase_page(message, offset=0, fresh=True)


@router.callback_query(BestCB.filter())
async def cb_best_more(query: CallbackQuery, callback_data: BestCB) -> None:
    await query.answer()
    # Кнопка стоит под карточкой: снимаем только её строку, «Не то»/«Открыть» остаются.
    with suppress(TelegramAPIError):
        await query.message.edit_reply_markup(
            reply_markup=without_best_rows(query.message.reply_markup)
        )
    await _send_showcase_page(query.message, offset=callback_data.offset)


async def _send_showcase_page(message: Message, *, offset: int, fresh: bool = False) -> None:
    """3 карточки витрины, под последней — «Показать ещё 3 лучших».

    Не пишется в sent_notifications: это не рассылка, и показанная здесь машина
    всё равно придёт по фильтру, если подходит. Показанное запоминается на 30 минут
    (`showcase.mark_seen`), чтобы превью фильтра и повторное «Лучшее сейчас» не
    повторяли те же машины; `fresh` — начать с первой непоказанной.
    """
    settings = get_settings()
    page, next_offset = await asyncio.to_thread(
        db.get_showcase, message.chat.id, offset, skip_seen=fresh
    )
    if not page:
        await message.answer(SHOWCASE_EMPTY_TEXT)
        return
    if offset == 0:
        await message.answer(SHOWCASE_INTRO_TEXT)
    await send_notifications(
        message.bot,
        page,
        group_min=settings.bot_group_min_listings,
        rich_cards=settings.bot_rich_cards_per_cycle,
        interactive=True,
        tail_markup=best_more_kb(next_offset) if next_offset is not None else None,
    )
    mark_seen(message.chat.id, [note.listing_id for note in page])


# --- Визард /add_filter -----------------------------------------------------


@router.message(Command("add_filter"))
@router.message(F.text == MENU_ADD_FILTER)
async def cmd_add_filter(message: Message, state: FSMContext) -> None:
    """Первый экран: готовые фильтры одной кнопкой и «Свой» — пошаговый визард."""
    await state.clear()
    quota = await asyncio.to_thread(db.filters_quota, message.from_user.id)
    if not quota.can_add:
        await message.answer(filters_limit_text(quota.used, quota.limit))
        return
    await message.answer(PRESETS_PROMPT_TEXT, reply_markup=presets_kb())


@router.callback_query(PresetCB.filter())
async def cb_preset(query: CallbackQuery, callback_data: PresetCB, state: FSMContext) -> None:
    if callback_data.code == CUSTOM_PRESET:
        await _start_wizard(query, state)
        return
    if find_preset(callback_data.code) is None:
        # Кнопка из истории чата после смены реестра пресетов.
        await query.answer("Такого готового фильтра больше нет", show_alert=True)
        await query.message.answer(PRESETS_PROMPT_TEXT, reply_markup=presets_kb())
        return
    await state.clear()
    try:
        created = await asyncio.to_thread(
            db.create_preset_filter, query.from_user.id, callback_data.code
        )
    except db.FilterLimitError as exc:
        await query.answer()
        await query.message.answer(filters_limit_text(exc.used, exc.limit))
        return
    except db.DuplicateFilterError:
        await query.answer()
        await query.message.answer(duplicate_filter_text())
        return
    except LookupError:
        await query.answer("Отправьте /start, чтобы начать пользоваться ботом", show_alert=True)
        return
    await query.answer("Фильтр сохранен")
    with suppress(TelegramAPIError):
        await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(filter_created_text(_filter_summary(created)))
    await _send_filter_preview(query.message, created.id)


async def _start_wizard(query: CallbackQuery, state: FSMContext) -> None:
    quota = await asyncio.to_thread(db.filters_quota, query.from_user.id)
    if not quota.can_add:
        await query.answer()
        await query.message.answer(filters_limit_text(quota.used, quota.limit))
        return
    brands = await asyncio.to_thread(db.list_brands)
    if not brands:
        await query.answer()
        await query.message.answer(
            "Каталог марок пока пуст — база еще наполняется. Попробуйте позже."
        )
        return
    await state.set_state(AddFilter.brand)
    await state.update_data(prompt_id=query.message.message_id)
    await query.answer()
    await query.message.edit_text("Шаг 1/7. Выберите марку:", reply_markup=brands_kb(brands))


# Текстовые шаги (годы, пробег, цена) отвечают НОВЫМ сообщением — иначе ответ
# человека оказался бы выше вопроса. Но предыдущий вопрос с кнопкой
# «Пропустить» тогда висел бы в чате мёртвым. Поэтому id текущего вопроса
# храним в FSM и при переходе дальше превращаем его в строку-итог без кнопок
# («✅ Пробег: до 150 000 км»): история настройки читается, неактивных кнопок нет.


async def _ask(message: Message, state: FSMContext, text: str, reply_markup=None) -> None:
    """Задаёт вопрос новым сообщением и запоминает его как текущий."""
    sent = await message.answer(text, reply_markup=reply_markup)
    await state.update_data(prompt_id=sent.message_id)


async def _close_prompt(message: Message, state: FSMContext, summary: str | None = None) -> None:
    """Гасит кнопки текущего вопроса; summary — заменить текст итогом шага.

    Best-effort: вопрос могли удалить, а Telegram не даёт править сообщения
    старше 48 ч — ошибка здесь не повод ломать визард.
    """
    prompt_id = (await state.get_data()).get("prompt_id")
    if prompt_id is None:
        return
    with suppress(TelegramAPIError):
        if summary is None:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=prompt_id, reply_markup=None
            )
        else:
            await message.bot.edit_message_text(
                summary, chat_id=message.chat.id, message_id=prompt_id
            )


async def _reask(message: Message, state: FSMContext, text: str, reply_markup=None) -> None:
    """Неверный ввод: старый вопрос без кнопок, подсказка с кнопками — новым."""
    await _close_prompt(message, state)
    await _ask(message, state, text, reply_markup)


def _years_label(year_from: int, year_to: int) -> str:
    return str(year_from) if year_from == year_to else f"{year_from}–{year_to}"


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
        await _reask(
            message,
            state,
            "Не понял 🤔 Напишите диапазон, например `2015-2020`, один год `2018` "
            "или нажмите «Пропустить».",
            skip_kb("years"),
        )
        return
    year_from = int(match.group(1))
    year_to = int(match.group(2)) if match.group(2) else year_from
    if not (MIN_YEAR <= year_from <= MAX_YEAR) or not (MIN_YEAR <= year_to <= MAX_YEAR):
        await _reask(
            message,
            state,
            f"Год должен быть между {MIN_YEAR} и {MAX_YEAR}. Попробуйте еще раз:",
            skip_kb("years"),
        )
        return
    if year_from > year_to:
        year_from, year_to = year_to, year_from
    await state.update_data(year_from=year_from, year_to=year_to)
    await state.set_state(AddFilter.mileage)
    await _close_prompt(message, state, f"✅ Годы: {_years_label(year_from, year_to)}")
    await _ask(
        message,
        state,
        "Шаг 4/7. Максимальный пробег в км, например `150000`:",
        skip_kb("mileage"),
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
        await _reask(
            message,
            state,
            "Нужно число в км, например `150000` (или «Пропустить»):",
            skip_kb("mileage"),
        )
        return
    await state.update_data(mileage_max_km=value)
    await state.set_state(AddFilter.price)
    await _close_prompt(message, state, f"✅ Пробег: до {value:,} км".replace(",", " "))
    await _ask(
        message,
        state,
        "Шаг 5/7. Максимальная цена в долларах, например `20000`:",
        skip_kb("price"),
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
        await _reask(
            message,
            state,
            "Нужно число в долларах, например `20000` (или «Пропустить»):",
            skip_kb("price"),
        )
        return
    await state.update_data(price_max_usd=float(value))
    await state.set_state(AddFilter.below_avg)
    await _close_prompt(message, state, f"✅ Цена: до ${value:,}".replace(",", " "))
    await _ask(
        message,
        state,
        "Шаг 6/7. Присылать только авто с ценой ниже средней по рынку?",
        yes_no_kb("below_avg"),
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
    await query.message.edit_text(filter_created_text(_filter_summary(created)))
    await _send_filter_preview(query.message, created.id)


async def _send_filter_preview(message: Message, filter_id: int) -> None:
    """Разовая подборка «что уже есть» сразу после создания фильтра.

    Противовес точке отсчёта в matching: та шлёт только появившееся ПОСЛЕ
    фильтра, и без превью человек остаётся в тишине до первой новой машины —
    на триале это читается как «бот не работает». Отправляем тем же путём, что
    и обычную рассылку: те же карточки, та же пауза 1 с на чат, та же кнопка
    чёрного списка.
    """
    settings = get_settings()
    try:
        # Уже показанное в этом чате (витрина в /start) не повторяем.
        preview = await asyncio.to_thread(
            db.preview_for_filter, filter_id, seen_ids(message.chat.id)
        )
    except Exception:
        logger.exception("filter preview failed for filter %s", filter_id)
        return
    if not preview:
        return
    await message.answer(preview_intro_text(len(preview)))
    await send_notifications(
        message.bot,
        preview,
        group_min=settings.bot_group_min_listings,
        rich_cards=settings.bot_rich_cards_per_cycle,
        interactive=True,
    )
    mark_seen(message.chat.id, [note.listing_id for note in preview])


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
async def wizard_years_fallback(message: Message, state: FSMContext) -> None:
    await _reask(
        message,
        state,
        "Отправьте годы текстом, например `2015-2020`, или нажмите «Пропустить».",
        skip_kb("years"),
    )


@router.message(AddFilter.mileage)
async def wizard_mileage_fallback(message: Message, state: FSMContext) -> None:
    await _reask(
        message,
        state,
        "Отправьте число в км, например `150000`, или нажмите «Пропустить».",
        skip_kb("mileage"),
    )


@router.message(AddFilter.price)
async def wizard_price_fallback(message: Message, state: FSMContext) -> None:
    await _reask(
        message,
        state,
        "Отправьте число в долларах, например `20000`, или нажмите «Пропустить».",
        skip_kb("price"),
    )


# Кнопки шагов визарда без активного шага: визард бросили (команда, кнопка
# меню, рестарт бота — состояние в памяти), а последний вопрос с кнопками
# остался в чате. Без этого хендлера нажатие ни на что не отвечает и Telegram
# крутит «часики». Регистрируется после всех шагов — ловит только «осиротевшие».
@router.callback_query(BrandCB.filter())
@router.callback_query(ModelCB.filter())
@router.callback_query(SkipCB.filter())
@router.callback_query(BoolCB.filter())
@router.callback_query(PercentCB.filter())
@router.callback_query(RegionCB.filter())
async def wizard_stale_button(query: CallbackQuery) -> None:
    await query.answer("Этот шаг уже неактуален — начните заново: /add_filter", show_alert=True)
    with suppress(TelegramAPIError):
        await query.message.edit_reply_markup(reply_markup=None)


# --- /admin ------------------------------------------------------------------
# Регистрируется ПОСЛЕДНИМ, уже после фолбэков визарда, и это не стилистика.
# Не-админу команда обязана быть неотличима от несуществующей, иначе сам факт
# наличия панели становится публичным. Неизвестная команда остаётся без ответа —
# и мы отвечаем тем же молчанием. Внутри визарда это тоже держится:
# `WizardExitMiddleware` (bot/wizard.py) сбрасывает состояние на любой команде,
# так что до фолбэков визарда команда не доходит. Регистрируется ПОСЛЕДНИМ,
# после фолбэков, чтобы не перехватывать их сообщения.


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    token = await asyncio.to_thread(db.issue_admin_login_token, message.from_user.id)
    if token is None:
        return  # не админ, либо панель выключена (ADMIN_PANEL_URL пуст)

    settings = get_settings()
    await message.answer(
        admin_login_text(settings.admin_login_token_ttl_minutes),
        reply_markup=pay_kb(settings.admin_login_url(token), "🔐 Открыть панель"),
    )
