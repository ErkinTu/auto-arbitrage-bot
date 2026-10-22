"""Контроль доступа: бот отвечает членам группы, подписчикам и тем, у кого идёт триал.

Доступ = член закрытой группы (TELEGRAM_ACCESS_CHAT_ID) ИЛИ активная платная
подписка ИЛИ активный триал. Команды входа (/start, /subscribe, /status…)
пропускаем всегда, иначе неоплативший не дошёл бы до оплаты. Признак доступа
синхронизируется в access_status (для matching.py, который Telegram не дёргает).

Отказ (`_deny`) про закрытую группу НЕ говорит: для человека без доступа она
не объяснение, а тупик. Вместо этого предлагаем следующий шаг — кнопку триала,
если он не израсходован, иначе оплату или ручное включение.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.db import get_subscription_info, resolve_access
from app.bot.keyboards import trial_kb
from app.bot.menu import menu_command
from app.bot.messages import access_denied_text
from app.core.config import get_settings

logger = logging.getLogger(__name__)

ALLOWED_STATUSES = {"creator", "administrator", "member"}
# Подстрока в тексте 400-й ошибки, означающей проблему с самим чатом, а не с
# юзером: такой ответ приходит на всех сразу и не должен разжаловывать базу.
CHAT_PROBLEM_MARKER = "chat not found"
# Команды, доступные без активного доступа: иначе неоплативший юзер не смог бы
# ни узнать о подписке, ни оплатить её.
ENTRY_COMMANDS = {"start", "help", "subscribe", "status", "cancel", "invite"}
# Callback-и, доступные без активного доступа (префикс aiogram CallbackData):
# кнопка триала в онбординге и выбор тарифа в /subscribe. Без второго меню
# тарифов открывалось (команда — входная), а нажатие на план упиралось в
# «доступ закончился» — оплатить не мог ровно тот, кому это нужнее всего.
ENTRY_CALLBACK_PREFIXES = {"trial", "plan"}
ENTRY_CALLBACK_SEP = ":"


class AccessMiddleware(BaseMiddleware):
    """Пропускает апдейт члену группы или активному подписчику.

    Членство в группе кэшируется на BOT_ACCESS_CACHE_TTL_SECONDS (дорогой вызов
    get_chat_member), подписка читается свежей на каждый апдейт (дёшево) — доступ
    открывается сразу после оплаты.
    """

    def __init__(self) -> None:
        self._member_cache: dict[int, tuple[bool, float]] = {}
        self._warned_no_chat = False

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.is_bot:
            return None

        # Бот работает только в личных сообщениях; апдейты из групп игнорируем,
        # иначе он реагировал бы на команды внутри самой закрытой группы.
        chat = event.chat if isinstance(event, Message) else None
        if isinstance(event, CallbackQuery) and event.message is not None:
            chat = event.message.chat
        if chat is not None and chat.type != "private":
            return None

        is_member = await self._is_group_member(data["bot"], user.id)
        has_access = await asyncio.to_thread(
            resolve_access, user.id, user.username, user.first_name, is_member
        )
        if has_access or self._is_entry(event):
            return await handler(event, data)

        await self._deny(event)
        return None

    async def _is_group_member(self, bot: Any, user_id: int) -> bool | None:
        """True/False — проверено; None — проверить не удалось (статус не трогаем)."""
        settings = get_settings()
        if not settings.telegram_access_chat_id:
            # Гейт по группе выключен (локальная разработка): считаем всех членами
            # → полный доступ, поведение как раньше.
            if not self._warned_no_chat:
                logger.warning("TELEGRAM_ACCESS_CHAT_ID is not set: group gate disabled")
                self._warned_no_chat = True
            return True

        cached = self._member_cache.get(user_id)
        now = time.monotonic()
        if cached is not None and cached[1] > now:
            return cached[0]

        try:
            member = await bot.get_chat_member(settings.telegram_access_chat_id, user_id)
            is_member = member.status in ALLOWED_STATUSES or (
                member.status == "restricted" and getattr(member, "is_member", False)
            )
        except TelegramBadRequest as exc:
            # «user not found» — достоверный отрицательный ответ про юзера. А вот
            # «chat not found» (бота выкинули из группы, опечатка в
            # TELEGRAM_ACCESS_CHAT_ID) прилетает тем же кодом на КАЖДОГО — и
            # разжаловал бы всю базу разом. Такое считаем «неизвестно».
            if CHAT_PROBLEM_MARKER in str(exc).lower():
                logger.error("access chat is unreachable, access checks suspended: %s", exc)
                return None
            is_member = False
        except TelegramAPIError:
            logger.exception("get_chat_member failed for user %s", user_id)
            return None

        self._member_cache[user_id] = (is_member, now + settings.bot_access_cache_ttl_seconds)
        return is_member

    @staticmethod
    def _is_entry(event: TelegramObject) -> bool:
        if isinstance(event, Message) and event.text:
            # Кнопка меню — та же команда: «💎 Подписка» без доступа обязана работать.
            command = menu_command(event.text) or (
                event.text.split(maxsplit=1)[0].lstrip("/").split("@")[0].lower()
            )
            return command in ENTRY_COMMANDS
        # Кнопки входа: «Начать бесплатный период» и выбор тарифа. Доступа у
        # нажимающего ещё НЕТ (он его как раз и получает), поэтому без этой
        # ветки онбординг и оплата упирались бы в «доступ по подписке».
        if isinstance(event, CallbackQuery) and event.data:
            return event.data.split(ENTRY_CALLBACK_SEP)[0] in ENTRY_CALLBACK_PREFIXES
        return False

    async def _deny(self, event: TelegramObject) -> None:
        """Отказ с кнопкой следующего шага, а не с сообщением про группу.

        Кнопку триала шлём отдельным сообщением даже в ответ на callback:
        всплывающий alert клавиатуру нести не умеет, а без кнопки человек
        остаётся ровно там же, где был, — знает про бесплатный период, но
        нажать его не может.
        """
        settings = get_settings()
        user = getattr(event, "from_user", None)
        telegram_id = user.id if user is not None else 0

        info = await asyncio.to_thread(get_subscription_info, telegram_id)
        # Незнакомого юзера (info is None) resolve_access уже завёл, но если
        # его почему-то нет — триал он точно не расходовал.
        trial_available = settings.trial_enabled and (info is None or not info.trial_used)
        text = access_denied_text(
            trial_days=settings.trial_days,
            trial_available=trial_available,
            payments_open=settings.subscription_allowed_for(telegram_id),
        )
        markup = trial_kb(settings.trial_days) if trial_available else None

        try:
            if isinstance(event, CallbackQuery):
                await event.answer()
                if event.message is not None:
                    await event.message.answer(text, reply_markup=markup)
                else:
                    await event.answer(text, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(text, reply_markup=markup)
        except TelegramAPIError:
            logger.debug("failed to deliver access-denied message", exc_info=True)
