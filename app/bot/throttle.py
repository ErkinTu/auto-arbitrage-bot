"""Защита от частых нажатий (план 01.11.2026, п. К).

Один человек частыми нажатиями не должен нагружать бота для всех. Здесь —
три примитива без aiogram (тестируются без бота) и middleware поверх них:

- `TokenBucket` — общий ограничитель апдейтов на человека
  (`ThrottlingMiddleware`, стоит ПЕРЕД `AccessMiddleware`: лишний апдейт не
  ходит даже в базу за проверкой доступа);
- `RateWindow` — кулдаун + потолок за окно для конкретного действия (витрина,
  превью, дебаунсы кнопок);
- `exclusive` — «операция в процессе»: пока у человека идёт тяжёлое действие
  (альбом, витрина, дайджест), второе не начинается.

Всё в памяти процесса, как дебаунс «Все фото»: рестарт обнуляет — не страшно.
Однопоточный asyncio — блокировок не нужно (вызывается только из хендлеров).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Hashable, Iterator
from contextlib import contextmanager
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

logger = logging.getLogger(__name__)

TOO_FAST_TEXT = "Слишком часто - подождите пару секунд"
BUSY_TEXT = "Секунду, ещё отправляю предыдущее"
# Всплывашку «слишком часто» показываем не чаще раза в столько секунд: на
# каждое лишнее нажатие её показывать — тот же флуд, только в другую сторону.
TOO_FAST_NOTICE_SECONDS = 10.0
# Память процесса не растёт вечно: при стольких ключах чистим протухшие.
_MAX_KEYS = 5000


class TokenBucket:
    """«Ведро токенов» на ключ: `burst` действий подряд, дальше `rate` в секунду."""

    def __init__(self, burst: int, rate: float) -> None:
        self.burst = burst
        self.rate = rate
        self._state: dict[Hashable, tuple[float, float]] = {}  # ключ → (токены, момент)

    @property
    def enabled(self) -> bool:
        return self.burst > 0 and self.rate > 0

    def allow(self, key: Hashable, *, now: float | None = None) -> bool:
        if not self.enabled:
            return True
        moment = time.monotonic() if now is None else now
        tokens, at = self._state.get(key, (float(self.burst), moment))
        tokens = min(float(self.burst), tokens + (moment - at) * self.rate)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self._state[key] = (tokens, moment)
        if len(self._state) > _MAX_KEYS:
            self._prune(moment)
        return allowed

    def _prune(self, moment: float) -> None:
        # Полное ведро ничем не отличается от отсутствующего ключа.
        full_after = self.burst / self.rate
        for key in [k for k, (_, at) in self._state.items() if moment - at >= full_after]:
            self._state.pop(key, None)


class RateWindow:
    """Кулдаун `min_interval` между действиями и не больше `max_count` за `window`.

    `wait(key)` — 0, если можно (и действие засчитывается), иначе сколько
    секунд ждать. Засчитывается сразу, ДО самого действия: иначе два нажатия
    внахлёст оба увидели бы «можно» (гонка лимита альбомов, п. К1).
    """

    def __init__(self, min_interval: float = 0.0, max_count: int = 0, window: float = 0.0) -> None:
        self.min_interval = min_interval
        self.max_count = max_count
        self.window = window
        self._hits: dict[Hashable, list[float]] = {}

    def wait(self, key: Hashable, *, now: float | None = None) -> float:
        moment = time.monotonic() if now is None else now
        span = max(self.min_interval, self.window)
        hits = [t for t in self._hits.get(key, ()) if moment - t < span]
        delay = 0.0
        if hits and self.min_interval > 0:
            delay = max(delay, hits[-1] + self.min_interval - moment)
        if self.max_count > 0 and self.window > 0:
            in_window = [t for t in hits if moment - t < self.window]
            if len(in_window) >= self.max_count:
                delay = max(delay, in_window[-self.max_count] + self.window - moment)
        if delay <= 0:
            hits.append(moment)
            delay = 0.0
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        if len(self._hits) > _MAX_KEYS:
            for stale in [k for k, ts in self._hits.items() if moment - ts[-1] >= span]:
                self._hits.pop(stale, None)
        return delay

    def forget(self, key: Hashable) -> None:
        """Отменить последнее засчитанное действие (оно не состоялось)."""
        hits = self._hits.get(key)
        if hits:
            hits.pop()
            if not hits:
                self._hits.pop(key, None)

    def clear(self) -> None:
        self._hits.clear()


_busy: set[Hashable] = set()


@contextmanager
def exclusive(key: Hashable) -> Iterator[bool]:
    """`with exclusive(user_id) as ok:` — False, если у ключа уже идёт операция."""
    if key in _busy:
        yield False
        return
    _busy.add(key)
    try:
        yield True
    finally:
        _busy.discard(key)


class ThrottlingMiddleware(BaseMiddleware):
    """Общий ограничитель апдейтов на человека (п. К12).

    Сверх лимита апдейт отбрасывается: на нажатие кнопки — всплывашка «слишком
    часто» (не чаще раза в TOO_FAST_NOTICE_SECONDS, иначе — молчаливый ответ,
    чтобы у кнопки не крутились «часики»), на сообщение — тишина.
    """

    def __init__(self, burst: int, rate: float) -> None:
        self.bucket = TokenBucket(burst, rate)
        self._notices = RateWindow(min_interval=TOO_FAST_NOTICE_SECONDS)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or self.bucket.allow(user.id):
            return await handler(event, data)
        logger.info("throttled update from %s", user.id)
        if isinstance(event, CallbackQuery):
            text = TOO_FAST_TEXT if self._notices.wait(user.id) == 0 else None
            try:
                await event.answer(text)
            except Exception:  # старый callback — Telegram уже не ждёт ответа
                pass
        return None


def reset() -> None:
    """Сбросить «операции в процессе» (тесты)."""
    _busy.clear()


def minutes(seconds: float) -> int:
    """Сколько минут ждать — для текста (минимум 1)."""
    return max(1, int((seconds + 59) // 60))
