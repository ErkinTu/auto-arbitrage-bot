"""Меню в стиле BotFather (`/menu`, план 01.11.2026, п. В): экраны одного сообщения.

Чистые функции «данные → (текст, кнопки)», как `filter_panel.py`; хендлер
(`handlers.py`, префикс `m:`) читает базу и перерисовывает сообщение на месте.
Экран закодирован в самой кнопке (`m:<экран>:<аргумент>`), FSM нет, поэтому
меню переживает рестарт бота, а старое сообщение-меню в чате продолжает
работать и показывает свежие данные.

Это дополнительный вход, а не замена: нижняя клавиатура (`bot/menu.py`) и все
команды остаются как были.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Экраны (значение `MenuCB.screen`). Коротко: callback_data ≤ 64 байт.
HOME = "home"
FILTERS = "fl"
FILTER = "f"  # карточка фильтра, arg = id
FILTER_DELETE = "fdel"  # подтверждение удаления, arg = id
FILTER_DELETE_YES = "fdy"  # удалить, arg = id
FILTER_EDIT = "fed"  # открыть панель фильтра, arg = id
FILTER_ADD = "fadd"
BLACKLIST = "bl"
BLOCK_RETURN = "blr"  # вернуть запись чёрного списка, arg = id
NOTIFY = "nt"
PAUSE = "pz"
PLAY = "pl"
ACCESS = "ac"
PLANS = "pn"  # тарифы отдельным сообщением (ссылка оплаты - кнопка-URL)
INVITE = "inv"
HELP = "hlp"
CLOSE = "x"

MENU_TITLE = "⚙️ Меню"
BACK = "◀ Назад"


class MenuCB(CallbackData, prefix="m"):
    screen: str
    arg: int = 0


@dataclass(frozen=True, slots=True)
class FilterRow:
    """Фильтр для меню: id и готовая строка-итог (`format_filter_line`)."""

    id: int
    summary: str


@dataclass(frozen=True, slots=True)
class BlockRow:
    id: int
    label: str


@dataclass(frozen=True, slots=True)
class HomeData:
    has_access: bool
    access_line: str  # «подписка до 30.11 · полная скорость» и т.п.
    paused_at: datetime | None
    filters_used: int
    filters_limit: int
    blocks: int


Screen = tuple[str, InlineKeyboardMarkup]


def _btn(text: str, screen: str, arg: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=MenuCB(screen=screen, arg=arg).pack())


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def _back(screen: str = HOME, text: str = BACK) -> list[InlineKeyboardButton]:
    return [_btn(text, screen)]


def home_screen(data: HomeData, *, paused_text: str | None = None) -> Screen:
    """Главная: сводка одной строкой на тему + разделы."""
    notify = f"на паузе с {paused_text}" if data.paused_at is not None and paused_text else (
        "на паузе" if data.paused_at is not None else "включены"
    )
    text = (
        f"<b>{MENU_TITLE}</b>\n\n"
        f"Доступ: {data.access_line}\n"
        f"Уведомления: {notify}\n"
        f"Фильтров: {data.filters_used} из {data.filters_limit} · Скрыто: {data.blocks}"
    )
    markup = _kb(
        [
            _btn(f"📋 Фильтры ({data.filters_used}/{data.filters_limit})", FILTERS),
            _btn(f"🚫 Чёрный список ({data.blocks})", BLACKLIST),
        ],
        [_btn("🔔 Уведомления", NOTIFY), _btn("💎 Доступ", ACCESS)],
        [_btn("👥 Пригласить", INVITE), _btn("❓ Помощь", HELP)],
        [_btn("✖ Закрыть", CLOSE)],
    )
    return text, markup


NO_ACCESS_TEXT = (
    "🔒 Этот раздел работает при активном доступе.\n\n"
    "Включить бесплатный период или выбрать тариф - в разделе «💎 Доступ»."
)


def no_access_screen() -> Screen:
    return NO_ACCESS_TEXT, _kb([_btn("💎 Доступ", ACCESS)], _back())


def filters_screen(filters: list[FilterRow], limit: int) -> Screen:
    if not filters:
        text = (
            f"📋 <b>Фильтры - 0 из {limit}</b>\n\n"
            "Фильтров пока нет - поэтому я молчу. Добавьте первый: готовый "
            "в одно нажатие или свой."
        )
        return text, _kb([_btn("➕ Добавить фильтр", FILTER_ADD)], _back())
    lines = [f"📋 <b>Фильтры - {len(filters)} из {limit}</b>\n"]
    lines += [f"{index}. {item.summary}" for index, item in enumerate(filters, start=1)]
    lines.append("\nНажмите на фильтр, чтобы изменить или удалить.")
    rows = [
        [_btn(_short(f"{index}. {item.summary}"), FILTER, item.id)]
        for index, item in enumerate(filters, start=1)
    ]
    if len(filters) < limit:
        rows.append([_btn("➕ Добавить фильтр", FILTER_ADD)])
    rows.append(_back())
    return "\n".join(lines), _kb(*rows)


def _short(text: str, limit: int = 48) -> str:
    """Подпись кнопки: Telegram обрезает сам, но некрасиво - режем по слову."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ·,")
    return cut + "…"


def filter_screen(
    item: FilterRow, index: int, *, sent_7d: int, best_percent: float | None
) -> Screen:
    """Карточка фильтра: полный итог и сколько он прислал за неделю."""
    if sent_7d:
        best = f" (лучшее - ниже рынка на {round(best_percent)}%)" if best_percent else ""
        stats = f"За 7 дней прислал: {sent_7d} {_objects(sent_7d)}{best}"
    else:
        stats = "За 7 дней ничего не прислал - возможно, фильтр слишком узкий."
    text = f"📋 <b>Фильтр {index}</b>\n\n{item.summary}\n\n{stats}"
    markup = _kb(
        [_btn("✏️ Изменить", FILTER_EDIT, item.id), _btn("🗑 Удалить", FILTER_DELETE, item.id)],
        _back(FILTERS, "◀ К фильтрам"),
    )
    return text, markup


def _objects(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "объявление"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "объявления"
    return "объявлений"


def filter_delete_screen(item: FilterRow) -> Screen:
    text = f"🗑 Удалить фильтр?\n\n{item.summary}"
    markup = _kb(
        [_btn("Да, удалить", FILTER_DELETE_YES, item.id), _btn("◀ Нет", FILTER, item.id)]
    )
    return text, markup


FILTER_GONE_TEXT = "Этот фильтр уже удалён."


def filter_gone_screen() -> Screen:
    return FILTER_GONE_TEXT, _kb(_back(FILTERS, "◀ К фильтрам"))


def blacklist_screen(blocks: list[BlockRow]) -> Screen:
    if not blocks:
        text = (
            "🚫 <b>Чёрный список пуст</b>\n\n"
            "Он работает поверх всех фильтров: что сюда попало - не придёт никогда.\n"
            "Добавить проще всего кнопкой «🚫 Не то» под любым объявлением."
        )
        return text, _kb(_back())
    lines = ["🚫 <b>Чёрный список</b> (не приходит ни по одному фильтру):\n"]
    lines += [f"{index}. {item.label}" for index, item in enumerate(blocks, start=1)]
    lines.append("\nНажмите, чтобы вернуть в выдачу.")
    rows = [[_btn(_short(f"↩️ Вернуть: {item.label}"), BLOCK_RETURN, item.id)] for item in blocks]
    rows.append(_back())
    return "\n".join(lines), _kb(*rows)


def notify_screen(paused_at: datetime | None, paused_text: str | None = None) -> Screen:
    if paused_at is None:
        text = (
            "🔔 <b>Уведомления: включены</b>\n\n"
            "Пауза останавливает пуши, а фильтры и доступ сохраняются.\n"
            "Раз в день напомню, что вы на паузе."
        )
        return text, _kb([_btn("⏸ Поставить на паузу", PAUSE)], _back())
    since = f" с {paused_text}" if paused_text else ""
    text = (
        f"🔔 <b>Уведомления: на паузе{since}</b>\n\n"
        "Новые объявления не присылаю. Срок доступа на паузе идёт как обычно."
    )
    return text, _kb([_btn("▶️ Включить", PLAY)], _back())


def access_screen(status: str, *, can_buy: bool) -> Screen:
    rows = [[_btn("💳 Тарифы", PLANS)]] if can_buy else []
    return f"💎 <b>Доступ</b>\n\n{status}", _kb(*rows, _back())


def invite_screen(text: str, share_url: str | None) -> Screen:
    rows = [[InlineKeyboardButton(text="📨 Отправить другу", url=share_url)]] if share_url else []
    return text, _kb(*rows, _back())


def help_screen(help_text: str) -> Screen:
    return help_text, _kb(_back())


def menu_button() -> InlineKeyboardMarkup:
    """Кнопка «⚙️ Меню» под приветствием /start."""
    return _kb([_btn(MENU_TITLE, HOME)])
