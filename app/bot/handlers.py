"""Команды и FSM-визард настройки фильтров."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InputMediaPhoto, Message

from app.bot import db, photos
from app.bot import filter_panel as fp
from app.bot import menu_screens as ms
from app.bot.keyboards import (
    FilterAddCB,
    FilterEditCB,
    filter_pick_kb,
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
    pay_kb,
    plans_kb,
    presets_kb,
    share_kb,
    trial_kb,
)
from app.bot.messages import (
    filter_updated_text,
    photos_contact_text,
    whatsapp_greeting,
    photos_limit_text,
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
from app.bot import notifier
from app.bot.notifier import send_notifications
from app.bot.showcase import mark_seen, seen_ids
from app.bot.states import FilterPanel
from app.core.config import get_settings
from app.services.phones import whatsapp_url
from app.services.blocklist import MAX_BLOCKS_PER_USER
from app.services.filter_presets import base_preset, find_preset
from app.services.finik import init_payment
from app.services.plans import find_plan

logger = logging.getLogger(__name__)

router = Router()

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
    text, link = await invite_content(message.from_user.id)
    await message.answer(
        text,
        reply_markup=share_kb(link) if link else None,
        disable_web_page_preview=True,
    )


async def invite_content(telegram_id: int) -> tuple[str, str | None]:
    """Текст приглашения и ссылка (None — ссылки нет). Общее для /invite и меню."""
    settings = get_settings()
    if not settings.referral_enabled:
        return "Реферальная программа сейчас недоступна.", None
    if not settings.bot_username:
        # Без username бота deep-link не собрать — молча отдавать «ссылку» вида
        # t.me/None было бы хуже честного отказа.
        return "Реферальные ссылки временно недоступны.", None
    try:
        info = await asyncio.to_thread(db.get_invite_info, telegram_id)
    except LookupError:
        return "Отправьте /start, чтобы начать пользоваться ботом.", None

    link = f"https://t.me/{settings.bot_username.lstrip('@')}?start=ref_{info.invite_code}"
    text = invite_text(
        link=link,
        qualified=info.qualified,
        friends_to_next_bonus=info.friends_to_next_bonus,
        bonus_days=settings.referral_bonus_days,
        friends_per_bonus=settings.referral_friends_per_bonus,
        bonuses_granted=info.bonuses_granted,
        limit_reached=info.limit_reached,
    )
    return text, link


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


async def _send_plans(message: Message, info, telegram_id: int | None = None) -> None:
    """`telegram_id` — когда `message` написал бот (меню): там `from_user` — бот."""
    settings = get_settings()
    telegram_id = telegram_id if telegram_id is not None else message.from_user.id
    if info is not None and info.access_status == "member":
        # Участнику группы платить нечем и незачем — до вайтлиста оплаты не
        # доходим, иначе он получил бы «оплата скоро», хотя доступ у него есть.
        await message.answer(SUBSCRIPTION_MEMBER_TEXT)
        return

    if not settings.subscription_allowed_for(telegram_id):
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
    """Тот же экран, что «💎 Доступ» в /menu — новым сообщением (одна правда)."""
    info = await asyncio.to_thread(db.get_subscription_info, message.from_user.id)
    text, markup = ms.render_access(info)
    await message.answer(text, reply_markup=markup)


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
    return ms.filter_summary(item)


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
    """Тот же экран, что «📋 Фильтры» в /menu — новым сообщением (одна правда)."""
    text, markup = await filters_screen(message.from_user.id, message.chat.id)
    await message.answer(text, reply_markup=markup)


async def filters_screen(telegram_id: int, chat_id: int) -> ms.Screen:
    filters = await asyncio.to_thread(db.list_filters, telegram_id)
    note = FILTERS_PRIORITY_NOTE if _should_show_priority_note(chat_id, filters) else None
    return ms.render_filters(filters, get_settings().bot_max_filters_per_user, note=note)


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
    """Тот же экран, что «🚫 Чёрный список» в /menu — новым сообщением."""
    blocks = await asyncio.to_thread(db.list_blocks, message.from_user.id)
    text, markup = ms.render_blacklist(blocks)
    await message.answer(text, reply_markup=markup)


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
PHOTOS_CONTACT_BUTTON = "💬 Связаться на сайте ↗"  # если номера нет
PHOTOS_WHATSAPP_BUTTON = "💬 Написать в WhatsApp ↗"


@router.callback_query(PhotosCB.filter())
async def photos_cb(query: CallbackQuery, callback_data: PhotosCB) -> None:
    """«📷 Все фото»: альбом(ы) ответом на карточку.

    Галерея — из базы; нет — один запрос к источнику (и запись в базу, второе
    нажатие бесплатное). Снятое альбома не получает: по флагу `is_active` —
    сразу, а давно не виденное (`BOT_PHOTOS_RECHECK_HOURS`) сначала проверяем
    на источнике — фото на CDN переживают объявление. Двойной тап гасит дебаунс.
    """
    listing_id = callback_data.listing_id
    if not photos.debounce(query.from_user.id, listing_id):
        await query.answer("Уже отправляю")
        return
    await query.answer("Загружаю фото…")
    gallery = await asyncio.to_thread(db.listing_gallery, listing_id)
    if gallery is None or not gallery.is_active:
        await query.message.answer(PHOTOS_GONE_TEXT)
        return
    settings = get_settings()
    wait = photos.album_wait_minutes(
        query.from_user.id, limit=settings.bot_photos_albums_per_hour
    )
    if wait:  # до запроса к источнику: лишние нажатия не должны его дёргать
        await query.message.answer(
            photos_limit_text(settings.bot_photos_albums_per_hour, wait),
            reply_markup=pay_kb(gallery.url, "Открыть ↗"),
        )
        return

    urls = gallery.urls
    phone = gallery.seller_phone
    card_fetched = False
    recheck = photos.needs_recheck(gallery.last_seen_at, hours=settings.bot_photos_recheck_hours)
    if (not urls or recheck) and photos.can_fetch(gallery.source_name):
        card = await _fetch_card(query, gallery)
        if card is _GONE:
            return
        if card is not None:
            card_fetched = True
            if card.urls:
                if card.urls != urls:  # продавец мог добавить фото — альбом свежий
                    await asyncio.to_thread(db.save_gallery, listing_id, card.urls)
                urls = card.urls
            phone = await _remember_phone(gallery, card)
    if len(urls) <= 1:
        await query.message.answer(
            PHOTOS_MISSING_TEXT, reply_markup=pay_kb(gallery.url, "Открыть ↗")
        )
        return

    reply_to = query.message.message_id
    try:
        for album in photos.albums(urls):
            await _send_album(query.message, album, reply_to)
    except TelegramAPIError as error:
        # Чаще всего — фото удалили с CDN или Telegram не смог его скачать.
        logger.warning("photos: album for listing %s rejected: %s", listing_id, error)
        await query.message.answer(
            PHOTOS_FAILED_TEXT, reply_markup=pay_kb(gallery.url, "Открыть ↗")
        )
        return
    photos.record_album(query.from_user.id)

    # Номера нет (mashina не отдаёт его в ленте) — один запрос страницы уже
    # ПОСЛЕ альбома: фото не ждут телефона, а номер запоминаем — второе
    # нажатие бесплатное.
    if (
        phone is None
        and not card_fetched
        and settings.bot_seller_phone_enabled
        and photos.can_fetch(gallery.source_name)
    ):
        card = await _fetch_card(query, gallery)
        if card is _GONE:
            return
        if card is not None:
            phone = await _remember_phone(gallery, card)

    # Долистал фото — следующий шаг «связаться». У альбома кнопок не бывает,
    # поэтому одно сообщение, тоже ответом на карточку: номер продавца в
    # под спойлером (позвонить) и кнопка WhatsApp с готовым текстом, а без номера —
    # кнопка на сайт (п. Д/Ж плана 01.11).
    if phone and settings.bot_seller_phone_enabled:
        await query.message.answer(
            photos_contact_text(gallery, phone=phone),
            reply_markup=pay_kb(
                whatsapp_url(phone, whatsapp_greeting(gallery)), PHOTOS_WHATSAPP_BUTTON
            ),
            reply_to_message_id=reply_to,
        )
        return
    await query.message.answer(
        photos_contact_text(gallery),
        reply_markup=pay_kb(gallery.url, PHOTOS_CONTACT_BUTTON),
        reply_to_message_id=reply_to,
    )


_GONE = object()  # маркер «источник сказал: снято» из `_fetch_card`


async def _fetch_card(query: CallbackQuery, gallery: db.Gallery):
    """Страница/карточка источника для «Все фото»: `photos.Card`, `_GONE` или None.

    Снятое — сразу в базу и сообщение человеку (дальше слать нечего). Таймаут,
    5xx, лежащий прокси — это НЕ «снято»: None, работаем с тем, что в базе.
    """
    try:
        return await asyncio.to_thread(
            photos.fetch_card, gallery.source_name, gallery.external_id, gallery.url
        )
    except photos.ListingGone as gone:
        await asyncio.to_thread(
            db.mark_listing_gone, gallery.listing_id, gone.reason, gone.status_id
        )
        await query.message.answer(PHOTOS_GONE_TEXT)
        return _GONE
    except Exception:
        logger.exception("photos: card fetch failed for listing %s", gallery.listing_id)
        return None


async def _remember_phone(gallery: db.Gallery, card: photos.Card) -> str | None:
    """Номер из ответа источника — в базу (скрыт продавцом — стираем). Номер в
    лог не пишем: персональные данные (services/phones.py)."""
    phone = None if card.phone_hidden else card.phone
    if phone is None and not card.phone_hidden:
        return gallery.seller_phone  # в ответе нет — известный не затираем
    if phone != gallery.seller_phone:
        await asyncio.to_thread(db.save_seller_phone, gallery.listing_id, phone)
    return phone


async def _send_album(message: Message, album: list[str], reply_to: int) -> None:
    """Один альбом (или хвост из одного фото) — по file_id из кэша, где он есть.

    Кэш общий с карточками (`notifier`): повторный альбом Telegram не качает с
    CDN. Отвергнутый file_id (Telegram его забыл) — забываем и повторяем по
    ссылкам; ошибка по ссылкам уходит наверх (там «не удалось загрузить»).
    """
    cached = [notifier.cached_photo_id(url) or url for url in album]

    async def send(media: list[str]) -> list:
        if len(media) == 1:  # альбом — минимум 2 фото
            sent = await message.answer_photo(media[0], reply_to_message_id=reply_to)
            return [sent]
        sent = await message.answer_media_group(
            [InputMediaPhoto(media=item) for item in media], reply_to_message_id=reply_to
        )
        return list(sent or [])

    if cached != album:
        try:
            sent = await send(cached)
        except TelegramBadRequest:
            notifier.forget_photo_ids(album)
            sent = await send(album)
    else:
        sent = await send(album)
    for url, item in zip(album, sent):
        sizes = getattr(item, "photo", None)
        if sizes:
            notifier.remember_photo_id(url, sizes[-1].file_id)


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
    """Первый экран: готовые фильтры одной кнопкой и «Свой» — панель фильтра."""
    await state.clear()
    await _send_add_filter_prompt(message, message.from_user.id)


@router.callback_query(FilterAddCB.filter())
async def cb_add_filter(query: CallbackQuery, state: FSMContext) -> None:
    """«➕ Добавить фильтр» под «Мои фильтры»: новым сообщением, список остаётся."""
    await query.answer()
    await state.clear()
    await _send_add_filter_prompt(query.message, query.from_user.id)


async def _send_add_filter_prompt(message: Message, telegram_id: int) -> None:
    """Пресеты + «Свой», а при занятом лимите — сколько удалить (не панель)."""
    quota = await asyncio.to_thread(db.filters_quota, telegram_id)
    if not quota.can_add:
        await message.answer(filters_limit_text(quota.used, quota.limit))
        return
    await message.answer(PRESETS_PROMPT_TEXT, reply_markup=presets_kb())


@router.callback_query(PresetCB.filter())
async def cb_preset(query: CallbackQuery, callback_data: PresetCB, state: FSMContext) -> None:
    if callback_data.code == CUSTOM_PRESET:
        await _start_panel(query, state)
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


# --- Панель фильтра (план 31.10.2026, п. 2.7) ----------------------------------
# Одно сообщение, которое перерисовывается на месте. В FSM: черновик, текущий
# шаг, id сообщения-панели, id фильтра (при правке) и страница моделей.
# Отрисовка и разбор ввода — чистые функции `bot/filter_panel.py`.


async def _panel_markup(data: dict) -> tuple[str, object]:
    """Текст и кнопки панели по данным FSM (марки/модели — из базы по шагу)."""
    draft, step = data["draft"], data["step"]
    editing = data.get("filter_id") is not None
    brands = models = ()
    if step == fp.STEP_BRAND:
        brands = await asyncio.to_thread(db.list_brands)
    elif step == fp.STEP_MODEL and draft.get("brand_id"):
        models = await asyncio.to_thread(db.list_models, draft["brand_id"])
    text = fp.render_text(draft, step, editing=editing, notice=data.get("notice"))
    markup = fp.render_keyboard(
        draft,
        step,
        editing=editing,
        brands=brands,
        models=models,
        model_page=data.get("model_page", 0),
    )
    return text, markup


async def _redraw(bot, chat_id: int, state: FSMContext) -> None:
    """Перерисовать панель на месте. «message is not modified» — не ошибка."""
    data = await state.get_data()
    text, markup = await _panel_markup(data)
    try:
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=data["panel_id"], reply_markup=markup
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            raise


async def _open_panel(message: Message, state: FSMContext, draft: dict, *, filter_id=None,
                      edit_message: bool = False) -> None:
    """Новая панель: правкой текущего сообщения (из кнопки) или новым сообщением."""
    data = {
        "draft": draft,
        "step": fp.STEP_BRAND,
        "filter_id": filter_id,
        "model_page": 0,
        "notice": None,
    }
    text, markup = await _panel_markup(data)
    if edit_message:
        await message.edit_text(text, reply_markup=markup)
        panel_id = message.message_id
    else:
        panel_id = (await message.answer(text, reply_markup=markup)).message_id
    await state.set_state(FilterPanel.active)
    await state.set_data({**data, "panel_id": panel_id})


async def _start_panel(query: CallbackQuery, state: FSMContext) -> None:
    """«Свой фильтр» из экрана пресетов: панель на месте этого сообщения."""
    quota = await asyncio.to_thread(db.filters_quota, query.from_user.id)
    if not quota.can_add:
        await query.answer()
        await query.message.answer(filters_limit_text(quota.used, quota.limit))
        return
    await query.answer()
    await _open_panel(query.message, state, fp.new_draft(), edit_message=True)


async def _start_edit(message: Message, state: FSMContext, telegram_id: int, filter_id: int,
                      *, edit_message: bool = False) -> bool:
    item = await asyncio.to_thread(db.get_filter, telegram_id, filter_id)
    if item is None:
        return False
    await _open_panel(
        message, state, fp.draft_from_filter(item), filter_id=filter_id, edit_message=edit_message
    )
    return True


@router.message(Command("edit_filter"))
async def cmd_edit_filter(message: Message, state: FSMContext) -> None:
    """Список фильтров на выбор; один фильтр — сразу панель."""
    await state.clear()
    filters = await asyncio.to_thread(db.list_filters, message.from_user.id)
    if not filters:
        await message.answer(
            "Фильтров пока нет — менять нечего.\n\n" + PRESETS_PROMPT_TEXT,
            reply_markup=presets_kb(),
        )
        return
    if len(filters) == 1:
        await _start_edit(message, state, message.from_user.id, filters[0].id)
        return
    await message.answer(
        "Какой фильтр изменить?\n\n" + _filters_text(filters).split("\n", 1)[1],
        reply_markup=filter_pick_kb(filters),
    )


@router.callback_query(FilterEditCB.filter())
async def cb_edit_filter(query: CallbackQuery, callback_data: FilterEditCB, state: FSMContext) -> None:
    """✏️ в «Мои фильтры» и выбор в /edit_filter — панель НОВЫМ сообщением
    (список фильтров остаётся в чате: по нему видно, что было до правки)."""
    await state.clear()
    opened = await _start_edit(query.message, state, query.from_user.id, callback_data.filter_id)
    if opened:
        await query.answer()
    else:
        await query.answer("Этот фильтр уже удалён", show_alert=True)


async def _stale_panel(query: CallbackQuery) -> None:
    await query.answer("Эта панель устарела — начните заново: /add_filter", show_alert=True)
    with suppress(TelegramAPIError):
        await query.message.edit_reply_markup(reply_markup=None)


@router.callback_query(FilterPanel.active, fp.PanelCB.filter())
async def cb_panel(query: CallbackQuery, callback_data: fp.PanelCB, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("panel_id") != query.message.message_id:
        # Нажали в старой панели, пока открыта другая.
        await _stale_panel(query)
        return
    action, value = callback_data.action, callback_data.value
    if action == "save":
        await _save_panel(query, state, data)
        return
    if action == "cancel":
        await state.clear()
        await query.answer("Отменено")
        with suppress(TelegramAPIError):
            await query.message.edit_text(
                "✖ Отменено — фильтр не сохранён." if data.get("filter_id") is None
                else "✖ Отменено — фильтр остался прежним.",
                reply_markup=ms.back_to_filters_kb(),
            )
        return
    if action == "noop":
        await query.answer()
        return
    if action == "input":
        await query.answer("Напишите значение сообщением в чат", show_alert=False)
        return

    updates: dict = {"notice": None}
    if action == "step" and value.isdigit() and int(value) in fp.STEPS:
        updates["step"] = int(value)
    elif action == "mpage" and value.lstrip("-").isdigit():
        updates["model_page"] = int(value)
    else:
        draft, advance = fp.apply_choice(data["draft"], action, value)
        if action == "brand" and draft.get("brand_id"):
            brands = await asyncio.to_thread(db.list_brands)
            brand = next((b for b in brands if b.id == draft["brand_id"]), None)
            if brand is None:
                await query.answer("Такой марки больше нет", show_alert=True)
                return
            draft["brand_name"] = brand.name
            updates["model_page"] = 0
        if action == "model" and draft.get("car_model_id"):
            models = await asyncio.to_thread(db.list_models, draft.get("brand_id") or 0)
            model = next((m for m in models if m.id == draft["car_model_id"]), None)
            if model is None:
                await query.answer("Такой модели нет", show_alert=True)
                return
            draft["model_name"] = model.name
        updates["draft"] = draft
        if advance:
            step = data["step"]
            # «Все марки» и группа марок — модель выбирать не из чего, сразу к годам.
            if action in ("brand", "group") and not draft.get("brand_id"):
                step = fp.STEP_MODEL
            updates["step"] = fp.next_step(step)
    await state.update_data(**updates)
    await query.answer()
    await _redraw(query.bot, query.message.chat.id, state)


async def _save_panel(query: CallbackQuery, state: FSMContext, data: dict) -> None:
    fields = fp.filter_fields(data["draft"])
    filter_id = data.get("filter_id")
    try:
        if filter_id is None:
            saved = await asyncio.to_thread(db.create_filter, query.from_user.id, **fields)
        else:
            saved = await asyncio.to_thread(
                db.update_filter, query.from_user.id, filter_id, **fields
            )
    except db.DuplicateFilterError:
        # Панель не закрываем: человек поправит шаг и сохранит снова.
        await state.update_data(notice="Такой фильтр у вас уже есть — измените что-нибудь.")
        await query.answer("Такой фильтр уже есть")
        await _redraw(query.bot, query.message.chat.id, state)
        return
    except db.FilterLimitError as exc:
        await state.clear()
        await query.answer()
        await query.message.edit_text(filters_limit_text(exc.used, exc.limit))
        return
    except LookupError:
        await state.clear()
        await query.answer("Фильтр не найден — возможно, его уже удалили", show_alert=True)
        with suppress(TelegramAPIError):
            await query.message.edit_reply_markup(reply_markup=None)
        return
    await state.clear()
    await query.answer("Фильтр сохранен")
    back = ms.back_to_filters_kb()  # панель могли открыть из /menu — путь назад
    if filter_id is None:
        await query.message.edit_text(
            filter_created_text(html.escape(_filter_summary(saved))), reply_markup=back
        )
        await _send_filter_preview(query.message, saved.id)
    else:
        await query.message.edit_text(
            filter_updated_text(html.escape(_filter_summary(saved))), reply_markup=back
        )


@router.message(FilterPanel.active, F.text, ~F.text.startswith("/"))
async def panel_text(message: Message, state: FSMContext) -> None:
    """Ручной ввод (годы, пробег, цена): разобрать, удалить сообщение, перерисовать.

    Сообщение человека удаляем, чтобы чат не засорялся — панель остаётся
    единственным следом настройки. Удалить можно не всегда (старше 48 ч,
    нет прав) — тогда просто оставляем.
    """
    data = await state.get_data()
    draft, notice = fp.apply_text(data["draft"], data["step"], message.text)
    updates = {"draft": draft, "notice": notice}
    if notice is None:
        updates["step"] = fp.next_step(data["step"])
    await state.update_data(**updates)
    with suppress(TelegramAPIError):
        await message.delete()
    try:
        await _redraw(message.bot, message.chat.id, state)
    except TelegramAPIError:
        # Панель удалили или она слишком старая для правки — откроем новую.
        fresh = await state.get_data()
        text, markup = await _panel_markup(fresh)
        sent = await message.answer(text, reply_markup=markup)
        await state.update_data(panel_id=sent.message_id)


@router.message(FilterPanel.active)
async def panel_other_message(message: Message, state: FSMContext) -> None:
    """Фото, стикер и т.п. внутри панели — подсказка в самой панели."""
    await state.update_data(notice="Выберите вариант кнопкой или напишите значение текстом.")
    with suppress(TelegramAPIError):
        await _redraw(message.bot, message.chat.id, state)


@router.callback_query(fp.PanelCB.filter())
async def cb_panel_stale(query: CallbackQuery) -> None:
    """Кнопка панели без активной панели: рестарт бота (FSM в памяти), выход
    командой, уже сохранённый фильтр."""
    await _stale_panel(query)




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
