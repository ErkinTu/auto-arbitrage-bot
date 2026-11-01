"""Хендлеры меню `/menu` (план 01.11.2026, п. В). Экраны — `menu_screens.py`.

Один хендлер на префикс `m:` — разбирает экран из кнопки, читает базу и
перерисовывает ТО ЖЕ сообщение. Меню — входной префикс в `AccessMiddleware`
(открывается и без доступа), а гейт — внутри: «Фильтры» и «Чёрный список» без
доступа показывают «нужен доступ» + «💎 Доступ», а не глухой отказ.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from urllib.parse import quote

from aiogram import Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from app.bot import db
from app.bot import handlers as h
from app.bot import menu_screens as ms
from app.bot.keyboards import presets_kb, with_rows
from app.bot.messages import PRESETS_PROMPT_TEXT, filters_limit_text, pause_on_text, play_on_text

logger = logging.getLogger(__name__)

router = Router(name="menu")

_TITLES = {ms.FILTERS: "📋 Фильтры", ms.BLACKLIST: "🚫 Чёрный список"}


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await asyncio.to_thread(db.log_menu_open, message.from_user.id)
    text, markup = await _home(message.from_user.id)
    await message.answer(text, reply_markup=markup)


async def _home(telegram_id: int) -> ms.Screen:
    info, quota, blocks = await asyncio.gather(
        asyncio.to_thread(db.get_subscription_info, telegram_id),
        asyncio.to_thread(db.filters_quota, telegram_id),
        asyncio.to_thread(db.list_blocks, telegram_id),
    )
    return ms.render_home(
        ms.HomeData(info=info, filters_used=quota.used, filters_limit=quota.limit, blocks=len(blocks))
    )


async def _show(query: CallbackQuery, screen: ms.Screen) -> None:
    """Перерисовать сообщение-меню. «message is not modified» (двойной тап по
    тому же экрану) — не ошибка."""
    text, markup = screen
    try:
        await query.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


@router.callback_query(ms.MenuCB.filter())
async def cb_menu(
    query: CallbackQuery, callback_data: ms.MenuCB, state: FSMContext, has_access: bool = True
) -> None:
    user_id = query.from_user.id
    screen, arg = callback_data.s, callback_data.a

    if screen in ms.NEEDS_ACCESS and not has_access:
        await query.answer()
        title = _TITLES.get(ms.BLACKLIST if screen.startswith("bl") else ms.FILTERS)
        await _show(query, ms.render_no_access(title))
        return

    if screen == ms.CLOSE:
        await query.answer()
        with suppress(TelegramAPIError):
            await query.message.delete()
        return

    if screen == ms.FILTER_EDIT:
        # Панель фильтра — в этом же сообщении; после «💾 Сохранить» под итогом
        # будет «◀ К фильтрам» (handlers._save_panel).
        await state.clear()
        opened = await h._start_edit(query.message, state, user_id, arg, edit_message=True)
        if opened:
            await query.answer()
        else:
            await query.answer("Фильтр уже удалён")
            await _show(query, ms.render_filter_gone())
        return

    if screen == ms.FILTER_ADD:
        await query.answer()
        await state.clear()
        quota = await asyncio.to_thread(db.filters_quota, user_id)
        back = [[_back_button(ms.FILTERS, "◀ К фильтрам")]]
        if not quota.can_add:
            await _show(query, (filters_limit_text(quota.used, quota.limit), with_rows(None, back)))
        else:
            await _show(query, (PRESETS_PROMPT_TEXT, with_rows(presets_kb(), back)))
        return

    if screen == ms.PLANS:
        # Тарифы — отдельным сообщением: ссылку на оплату Finik нельзя
        # «перерисовать» в меню без потери, а под ней свои кнопки.
        await query.answer()
        info = await asyncio.to_thread(db.get_subscription_info, user_id)
        await h._send_plans(query.message, info, telegram_id=user_id)
        return

    toast = None
    if screen == ms.FILTER_DELETE_OK:
        deleted = await asyncio.to_thread(db.delete_filter, user_id, arg)
        toast = "Фильтр удалён" if deleted else "Фильтр уже удалён"
        screen = ms.FILTERS
    elif screen == ms.BLACKLIST_REMOVE:
        deleted = await asyncio.to_thread(db.delete_block, user_id, arg)
        toast = "Вернул в выдачу" if deleted else "Уже удалено"
        screen = ms.BLACKLIST
    elif screen in (ms.PAUSE, ms.PLAY):
        result = await asyncio.to_thread(db.set_paused, user_id, screen == ms.PAUSE)
        if result is not None and result.changed:
            toast = pause_on_text() if screen == ms.PAUSE else play_on_text()
        screen = ms.NOTIFY

    await query.answer(_first_line(toast) if toast else None)
    await _show(query, await _render(screen, arg, user_id, query.message.chat.id))


async def _render(screen: str, arg: int, user_id: int, chat_id: int) -> ms.Screen:
    if screen == ms.FILTERS:
        return await h.filters_screen(user_id, chat_id)
    if screen in (ms.FILTER, ms.FILTER_DELETE):
        filters = await asyncio.to_thread(db.list_filters, user_id)
        index, item = next(((i, f) for i, f in enumerate(filters, 1) if f.id == arg), (0, None))
        if item is None:
            return ms.render_filter_gone()
        if screen == ms.FILTER_DELETE:
            return ms.render_filter_delete(item)
        sent, best = await asyncio.to_thread(db.filter_sent_stats, user_id, item.id)
        return ms.render_filter(item, index, ms.FilterStats(sent=sent, best_percent=best))
    if screen == ms.BLACKLIST:
        return ms.render_blacklist(await asyncio.to_thread(db.list_blocks, user_id))
    if screen == ms.NOTIFY:
        info = await asyncio.to_thread(db.get_subscription_info, user_id)
        return ms.render_notify(info.paused_at if info is not None else None)
    if screen == ms.ACCESS:
        return ms.render_access(await asyncio.to_thread(db.get_subscription_info, user_id))
    if screen == ms.INVITE:
        text, link = await h.invite_content(user_id)
        share = f"https://t.me/share/url?url={quote(link, safe='')}" if link else None
        return ms.render_invite(text, share)
    if screen == ms.HELP:
        return ms.render_help()
    return await _home(user_id)  # HOME и незнакомый экран (старая кнопка)


def _back_button(screen: str, text: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=ms.MenuCB(s=screen).pack())


def _first_line(text: str) -> str:
    """Всплывашка `answerCallbackQuery` — до 200 символов, без HTML."""
    line = text.split("\n", 1)[0]
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        line = line.replace(tag, "")
    return line[:200]
