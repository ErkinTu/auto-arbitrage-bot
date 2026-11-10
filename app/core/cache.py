"""TTL-кэш ответов публичных ручек в памяти процесса.

Зачем: справочники и агрегаты (`catalog/*`, `dashboard/*`, `market/*`) одинаковы
для всех и меняются раз в цикл бота (10 мин), а считаются тяжело —
`dashboard/summary` это `percentile_cont` по всей `listings`. Без кэша каждый
заход на будущий сайт — полный проход по таблице, и сотня пользователей разом
кладёт базу, которую делят бот и worker.

Защита от «стада» (stampede): на промахе по одному ключу считает один поток,
остальные ждут его результат, а не идут в базу параллельно.

Ограничения осознанные: кэш на процесс (контейнер `api` один); ошибки
(`LookupError` → 404) не кэшируются. Переход на Redis — `docs/api/PUBLIC_API.md`.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, TypeVar

from app.core.config import get_settings

T = TypeVar("T")


class TTLCache:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._data: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._key_locks: dict[tuple, threading.Lock] = {}
        self._clock = clock
        self.hits = 0
        self.misses = 0

    def get_or_compute(
        self, key: tuple, compute: Callable[[], T], *, ttl: float, max_entries: int
    ) -> T:
        if ttl <= 0:
            return compute()
        cached = self._get(key)
        if cached is not _MISS:
            return cached  # type: ignore[return-value]

        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            # Пока ждали лок, значение мог посчитать соседний поток.
            cached = self._get(key)
            if cached is not _MISS:
                return cached  # type: ignore[return-value]
            value = compute()
            with self._lock:
                self.misses += 1
                self._data[key] = (self._clock() + ttl, value)
                self._data.move_to_end(key)
                while len(self._data) > max(max_entries, 1):
                    old_key, _ = self._data.popitem(last=False)
                    self._key_locks.pop(old_key, None)
            return value

    def _get(self, key: tuple) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return _MISS
            expires_at, value = entry
            if expires_at <= self._clock():
                del self._data[key]
                return _MISS
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._key_locks.clear()
            self.hits = self.misses = 0

    def __len__(self) -> int:
        return len(self._data)


_MISS = object()


def _freeze(value: Any) -> Any:
    """Списки/датаклассы фильтра — в хэшируемый вид для ключа кэша."""
    if isinstance(value, list):
        return tuple(value)
    if hasattr(value, "__dataclass_fields__"):
        return tuple((name, _freeze(getattr(value, name))) for name in value.__dataclass_fields__)
    return value

response_cache = TTLCache()


def cached_call(
    namespace: str,
    fn: Callable[..., T],
    session: Any,
    *,
    ttl: float | None = None,
    **params: Any,
) -> T:
    """`fn(session, **params)` через кэш. Ключ — namespace + отсортированные
    параметры; сессия в ключ не входит (у каждого запроса своя)."""
    settings = get_settings()
    key = (namespace, tuple(sorted((k, _freeze(v)) for k, v in params.items())))
    return response_cache.get_or_compute(
        key,
        lambda: fn(session, **params),
        ttl=settings.api_cache_ttl_seconds if ttl is None else ttl,
        max_entries=settings.api_cache_max_entries,
    )
