"""Меню в стиле BotFather (план 01.11.2026, п. В): одно сообщение, по которому
ходят кнопками; каждое нажатие перерисовывает его на месте.

Это ДОПОЛНИТЕЛЬНЫЙ вход ко всему боту, не замена: нижняя клавиатура
(`bot/menu.py` — не путать) и команды остаются.

Навигация без FSM: экран закодирован в самой кнопке (`m:<экран>:<аргумент>`),
каждое нажатие заново читает базу. Поэтому меню переживает рестарт бота, а
старое сообщение-меню в истории чата продолжает работать и показывает свежие
данные. FSM нужен только там, где вводят текст, — это панель фильтра, она
открывается из меню как есть.

Здесь — чистые функции «данные → (текст, кнопки)», без aiogram-хендлеров и
базы (их держит `bot/menu_handlers.py`), чтобы экраны тестировались без бота.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.messages import (
    BLOCK_LIST_EMPTY_TEXT,
    HELP_TEXT,
    _local_dt,
    format_filter_line,
    status_text,
)
from app.services.plans import TIER_FULL

# Экраны. Коротко: callback_data ≤ 64 байт, а «m:fdy:2147483647» — 16.
HOME = "home"
FILTERS = "fl"
FILTER = "f"  # карточка фильтра, аргумент — id
FILTER_DELETE = "fd"  # подтверждение удаления
FILTER_DELETE_OK = "fdy"
FILTER_EDIT = "fe"  # → панель фильтра в этом же сообщении
FILTER_ADD = "fa"  # → пресеты + «Свой»
BLACKLIST = "bl"
BLACKLIST_REMOVE = "blr"  # аргумент — id записи
NOTIFY = "nt"
PAUSE = "ps"
PLAY = "pl"
ACCESS = "ac"
PLANS = "pn"  # → тарифы отдельным сообщением (там кнопки оплаты)
INVITE = "iv"
HELP = "hp"
CLOSE = "x"

# Экраны, которым нужен доступ: без него — «нужен доступ» + «💎 Доступ», а не
# глухой отказ middleware (меню — входной префикс, см. access.py).
NEEDS_ACCESS = frozenset(
    {FILTERS, FILTER, FILTER_DELETE, FILTER_DELETE_OK, FILTER_EDIT, FILTER_ADD, BLACKLIST, BLACKLIST_REMOVE}
)

BUTTON_LABEL_MAX = 40  # длиннее Telegram обрежет «…» сам, но некрасиво


class MenuCB(CallbackData, prefix="m"):
    s: str  # экран
    a: int = 0  # аргумент (id фильтра / записи чёрного списка)


Screen = tuple[str, InlineKeyboardMarkup]


def _button(builder: InlineKeyboardBuilder, text: str, screen: str, arg: int = 0) -> None:
    builder.button(text=text, callback_data=MenuCB(s=screen, a=arg))


def _back(builder: InlineKeyboardBuilder, screen: str = HOME, text: str = "◀ Назад") -> None:
    _button(builder, text, screen)


def back_to_filters_kb() -> InlineKeyboardMarkup:
    """После сохранения/отмены панели: вернуться к списку (п. В, этап 1)."""
    builder = InlineKeyboardBuilder()
    _button(builder, "◀ К фильтрам", FILTERS)
    return builder.as_markup()


def filter_summary(item) -> str:
    """Итог фильтра одной строкой (`db.FilterDTO`)."""
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
        fuel_groups=item.fuel_groups,
    )


def _short(text: str) -> str:
    return text if len(text) <= BUTTON_LABEL_MAX else text[: BUTTON_LABEL_MAX - 1] + "…"


# --- главная -------------------------------------------------------------------------


def access_line(info) -> str:
    """«Доступ: подписка до 30.11 · полная скорость» — одной строкой для сводки."""
    if info is None:
        return "Доступ: нет"
    if info.access_status == "member":
        return "Доступ: участник группы"
    if info.trial_active and info.trial_expires_at is not None:
        return f"Доступ: пробный период до {_local_dt(info.trial_expires_at)}"
    if info.active and info.expires_at is not None:
        speed = "полная скорость" if info.tier == TIER_FULL else "с отставанием ~1 ч"
        return f"Доступ: подписка до {_local_dt(info.expires_at)} · {speed}"
    return "Доступ: закончился"


@dataclass(slots=True)
class HomeData:
    info: object | None  # db.SubscriptionInfo
    filters_used: int
    filters_limit: int
    blocks: int


def render_home(data: HomeData) -> Screen:
    paused = data.info is not None and data.info.paused_at is not None
    lines = [
        "<b>⚙️ Меню</b>",
        html.escape(access_line(data.info)),
        f"Уведомления: {'на паузе' if paused else 'включены'} · "
        f"Фильтров: {data.filters_used} из {data.filters_limit} · Скрыто: {data.blocks}",
    ]
    builder = InlineKeyboardBuilder()
    _button(builder, f"📋 Фильтры ({data.filters_used}/{data.filters_limit})", FILTERS)
    _button(builder, f"🚫 Чёрный список ({data.blocks})", BLACKLIST)
    _button(builder, "🔔 Уведомления" + (" ⏸" if paused else ""), NOTIFY)
    _button(builder, "💎 Доступ", ACCESS)
    _button(builder, "👥 Пригласить", INVITE)
    _button(builder, "❓ Помощь", HELP)
    _button(builder, "✖ Закрыть", CLOSE)
    builder.adjust(2, 2, 2, 1)
    return "\n".join(lines), builder.as_markup()


def render_no_access(title: str) -> Screen:
    builder = InlineKeyboardBuilder()
    _button(builder, "💎 Доступ", ACCESS)
    _back(builder)
    builder.adjust(1)
    return (
        f"<b>{title}</b>\nЭтот раздел работает при активном доступе: пробный период, "
        "подписка или участие в группе.",
        builder.as_markup(),
    )


# --- фильтры -----------------------------------------------------------------------


def render_filters(filters: list, limit: int, *, note: str | None = None) -> Screen:
    builder = InlineKeyboardBuilder()
    if not filters:
        _button(builder, "➕ Добавить фильтр", FILTER_ADD)
        _back(builder)
        builder.adjust(1)
        return (
            "<b>📋 Фильтры — 0 из {limit}</b>\nФильтров пока нет — поэтому я молчу.".format(limit=limit),
            builder.as_markup(),
        )
    lines = [f"<b>📋 Фильтры — {len(filters)} из {limit}</b>"]
    for index, item in enumerate(filters, start=1):
        summary = filter_summary(item)
        lines.append(f"{index}. {html.escape(summary)}")
        _button(builder, _short(f"{index}. {summary}"), FILTER, item.id)
    if note:
        lines.append(f"\n{html.escape(note)}")
    _button(builder, "➕ Добавить фильтр", FILTER_ADD)
    _back(builder)
    builder.adjust(1)
    return "\n".join(lines), builder.as_markup()


@dataclass(slots=True)
class FilterStats:
    sent: int  # уведомлений по фильтру за период
    best_percent: float | None  # лучшее «ниже рынка» среди них
    days: int = 7


def render_filter(item, index: int, stats: FilterStats) -> Screen:
    if stats.sent:
        word = _plural(stats.sent, "объявление", "объявления", "объявлений")
        sent = f"За {stats.days} дней прислал: {stats.sent} {word}"
        if stats.best_percent:
            sent += f" (лучшее — ниже рынка на {round(stats.best_percent)}%)"
    else:
        sent = f"За {stats.days} дней ничего не прислал — возможно, фильтр слишком узкий."
    text = f"<b>📋 Фильтр {index}</b>\n{html.escape(filter_summary(item))}\n\n{sent}"
    builder = InlineKeyboardBuilder()
    _button(builder, "✏️ Изменить", FILTER_EDIT, item.id)
    _button(builder, "🗑 Удалить", FILTER_DELETE, item.id)
    _back(builder, FILTERS, "◀ К фильтрам")
    builder.adjust(2, 1)
    return text, builder.as_markup()


def render_filter_gone() -> Screen:
    builder = InlineKeyboardBuilder()
    _back(builder, FILTERS, "◀ К фильтрам")
    return "Фильтр уже удалён.", builder.as_markup()


def render_filter_delete(item) -> Screen:
    builder = InlineKeyboardBuilder()
    _button(builder, "Да, удалить", FILTER_DELETE_OK, item.id)
    _button(builder, "◀ Нет", FILTER, item.id)
    builder.adjust(2)
    return f"Удалить фильтр?\n{html.escape(filter_summary(item))}", builder.as_markup()


# --- чёрный список -----------------------------------------------------------------


def render_blacklist(blocks: list) -> Screen:
    builder = InlineKeyboardBuilder()
    if not blocks:
        _back(builder)
        return BLOCK_LIST_EMPTY_TEXT, builder.as_markup()
    lines = ["<b>🚫 Чёрный список</b> — не приходит ни по одному фильтру:"]
    for index, item in enumerate(blocks, start=1):
        lines.append(f"{index}. {html.escape(item.label)}")
        _button(builder, _short(f"↩️ Вернуть: {item.label}"), BLACKLIST_REMOVE, item.id)
    _back(builder)
    builder.adjust(1)
    return "\n".join(lines), builder.as_markup()


# --- уведомления, доступ, приглашения, помощь ------------------------------------------


def render_notify(paused_at) -> Screen:
    builder = InlineKeyboardBuilder()
    if paused_at is not None:
        text = (
            f"<b>🔔 Уведомления: на паузе</b> с {_local_dt(paused_at)}\n"
            "Фильтры и доступ сохранены — включите, и пуши пойдут снова."
        )
        _button(builder, "▶️ Включить", PLAY)
    else:
        text = (
            "<b>🔔 Уведомления: включены</b>\n"
            "Пауза останавливает пуши, фильтры и доступ сохраняются. "
            "Раз в день напомню, что вы на паузе."
        )
        _button(builder, "⏸ Поставить на паузу", PAUSE)
    _back(builder)
    builder.adjust(1)
    return text, builder.as_markup()


def render_access(info) -> Screen:
    builder = InlineKeyboardBuilder()
    if info is None:
        text = "<b>💎 Доступ</b>\nДоступа пока нет."
    else:
        text = "<b>💎 Доступ</b>\n" + status_text(
            access_status=info.access_status,
            expires_at=info.expires_at,
            active=info.active,
            trial_active=info.trial_active,
            trial_expires_at=info.trial_expires_at,
            schedule=info.schedule,
            tier=info.tier,
            paused_at=info.paused_at,
        )
    if info is None or info.access_status != "member":
        _button(builder, "💳 Тарифы", PLANS)  # оплата — отдельным сообщением
    _back(builder)
    builder.adjust(1)
    return text, builder.as_markup()


def render_invite(text: str | None, share_url: str | None) -> Screen:
    """`text` — `messages.invite_text` или None (рефералка выключена)."""
    builder = InlineKeyboardBuilder()
    if share_url:
        builder.button(text="📨 Отправить другу", url=share_url)
    _back(builder)
    builder.adjust(1)
    return text or "Реферальная программа сейчас недоступна.", builder.as_markup()


def render_help() -> Screen:
    builder = InlineKeyboardBuilder()
    _back(builder)
    return html.escape(HELP_TEXT), builder.as_markup()


def _plural(count: int, one: str, few: str, many: str) -> str:
    if 11 <= count % 100 <= 14:
        return many
    last = count % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many
