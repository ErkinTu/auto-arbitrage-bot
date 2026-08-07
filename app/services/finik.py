"""Клиент Finik Web SDK (acquiring, finik.kg) для разовой оплаты подписки.

Поток: init_payment (сервер→сервер) отдаёт 302, ссылка на страницу оплаты — в
заголовке `Location` (её кладём в inline-кнопку бота). После оплаты Finik POST-ит
подписанный JSON на наш `webhookUrl` — webhook является ЕДИНСТВЕННЫМ источником
истины (возврат юзера по RedirectUrl оплату не подтверждает).

Подпись — RSA-SHA256(каноническая строка), base64:

    method.lower() \n unquote(path) \n host:<h>&<x-api-*>:<v>… \n [query] \n JSON(тело)

Сортируются ТОЛЬКО ключи верхнего уровня тела (вложенные объекты — как есть),
в подпись входят лишь `Host` и заголовки `x-api-*`. Схема восстановлена из
npm-пакета `@mancho.devs/authorizer` (доки описывают её только словами) и сверена
с ним байт в байт — векторы в `tests/test_finik.py`, разбор в
`docs/PAYMENTS_FINIK_ANALYSIS.md §4`.

Доки: finik.kg/documentation/web-sdk/{,integration,webhooks,reference}
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Mapping

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CARD_TYPE_QR = "FINIK_QR"  # единственное значение CardType для Web SDK
PAYMENT_PATH = "/v1/payment"
WEBHOOK_PATH = "/api/v1/payments/finik"

# Публичные ключи Finik для проверки входящих webhook — РАЗНЫЕ по среде.
# Источник: finik.kg/documentation/web-sdk/reference/ (сняты со страницы, оба
# разбираются как валидные RSA-2048).
FINIK_PUBLIC_KEYS: dict[str, str] = {
    "prod": """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuF/PUmhMPPidcMxhZBPb
BSGJoSphmCI+h6ru8fG8guAlcPMVlhs+ThTjw2LHABvciwtpj51ebJ4EqhlySPyT
hqSfXI6Jp5dPGJNDguxfocohaz98wvT+WAF86DEglZ8dEsfoumojFUy5sTOBdHEu
g94B4BbrJvjmBa1YIx9Azse4HFlWhzZoYPgyQpArhokeHOHIN2QFzJqeriANO+wV
aUMta2AhRVZHbfyJ36XPhGO6A5FYQWgjzkI65cxZs5LaNFmRx6pjnhjIeVKKgF99
4OoYCzhuR9QmWkPl7tL4Kd68qa/xHLz0Psnuhm0CStWOYUu3J7ZpzRK8GoEXRcr8
tQIDAQAB
-----END PUBLIC KEY-----
""",
    "beta": """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwlrlKz/8gLWd1ARWGA/8
o3a3Qy8G+hPifyqiPosiTY6nCHovANMIJXk6DH4qAqqZeLu8pLGxudkPbv8dSyG7
F9PZEAryMPzjoB/9P/F6g0W46K/FHDtwTM3YIVvstbEbL19m8yddv/xCT9JPPJTb
LsSTVZq5zCqvKzpupwlGS3Q3oPyLAYe+ZUn4Bx2J1WQrBu3b08fNaR3E8pAkCK27
JqFnP0eFfa817VCtyVKcFHb5ij/D0eUP519Qr/pgn+gsoG63W4pPHN/pKwQUUiAy
uLSHqL5S2yu1dffyMcMVi9E/Q2HCTcez5OvOllgOtkNYHSv9pnrMRuws3u87+hNT
ZwIDAQAB
-----END PUBLIC KEY-----
""",
}

# Символы, которые JS encodeURI() НЕ кодирует (нужно для канона query-строки).
_ENCODE_URI_SAFE = "!#$&'()*+,/:;=?@[]~-_.*"


# --------------------------------------------------------------------------
# Каноникализация (порт Signer.getData() из @mancho.devs/authorizer)
# --------------------------------------------------------------------------


def _js_number(value: Any) -> Any:
    """Приводит число к тому виду, как его печатает JSON.stringify в JS.

    Ключевая грабля: JSON.parse("1500.00") -> 1500 -> stringify -> "1500", а
    Python даёт 1500.0 -> "1500.0" и подпись webhook не сходится. Поэтому
    целочисленный float печатаем как int (как и JS).
    """
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return int(value)
    return value


def _normalize_numbers(value: Any) -> Any:
    """Рекурсивно нормализует числа (вложенные `fields` тоже подписываются)."""
    if isinstance(value, dict):
        return {k: _normalize_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_numbers(v) for v in value]
    return _js_number(value)


def _locale_like_key(name: str) -> tuple:
    """Ключ сортировки, приближающий JS String.localeCompare (ICU).

    JS сортирует ключи тела через localeCompare, а не по кодпоинтам:
    `_x, accountId, Amount, name_en, nameEn, Zebra` вместо
    `Amount, Zebra, _x, accountId, nameEn, name_en`. Для НАШИХ тел (PascalCase в
    запросе, camelCase в webhook) оба порядка совпадают, поэтому основной путь —
    обычный sorted(); эта функция нужна лишь как фолбэк проверки webhook, если
    Finik добавит поле с подчёркиванием/иным регистром.
    """
    return ([(0,) if not ch.isalnum() else (1, ch.lower()) for ch in name], name)


def js_json(body: Mapping[str, Any], *, key=None) -> str:
    """Сериализует тело так, как это делает JS-подписант Finik.

    Отсортированы ТОЛЬКО ключи верхнего уровня; вложенные объекты сохраняют
    исходный порядок. Без пробелов, юникод не эскейпится.
    """
    ordered = {k: _normalize_numbers(body[k]) for k in sorted(body, key=key)}
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


def canonical(
    *,
    method: str,
    path: str,
    headers: Mapping[str, Any],
    body: Mapping[str, Any] | None = None,
    query: Mapping[str, str] | None = None,
    body_json: str | None = None,
) -> str:
    """Собирает строку, которая подписывается/проверяется (см. докстринг модуля)."""
    parts = [method.lower(), urllib.parse.unquote(path)]

    host = headers.get("Host") or headers.get("host")
    if not host:
        raise ValueError("Header 'Host' is required")
    header_parts = [f"host:{host}"]
    for key in sorted(k for k in headers if k.lower().startswith("x-api-")):
        header_parts.append(f"{key.lower()}:{headers[key]}")
    parts.append("&".join(header_parts))

    if query:
        # JS: encodeURI(decodeURI(k)) + '=' + encodeURI(decodeURI(v)), ключи sort()
        pairs = []
        for key in sorted(query):
            value = query[key] or ""
            pairs.append(
                f"{urllib.parse.quote(urllib.parse.unquote(key), safe=_ENCODE_URI_SAFE)}="
                f"{urllib.parse.quote(urllib.parse.unquote(value), safe=_ENCODE_URI_SAFE)}"
            )
        joined = "&".join(pairs)
        if joined:
            parts.append(joined)

    if body_json is not None:
        parts.append(body_json)
    else:
        parts.append(js_json(body) if body else "")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Подпись
# --------------------------------------------------------------------------


def sign(data: str, private_key_pem: str) -> str:
    """RSA-SHA256 (PKCS#1 v1.5) нашим приватным ключом -> base64."""
    import base64

    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature = key.sign(data.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def verify(data: str, signature_b64: str, public_key_pem: str) -> bool:
    """Проверяет base64-подпись публичным ключом (без исключений наружу)."""
    import base64
    import binascii

    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError):
        return False
    key = serialization.load_pem_public_key(public_key_pem.encode())
    try:
        key.verify(signature, data.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True


@lru_cache(maxsize=1)
def load_private_key() -> str:
    """Читает приватный ключ с диска (монтируется в контейнер `bot`)."""
    path = Path(get_settings().finik_private_key_path)
    return path.read_text(encoding="utf-8")


def _timestamp_ms() -> str:
    """x-api-timestamp: UNIX в миллисекундах (у Finik живёт ~10 секунд)."""
    return str(int(time.time() * 1000))


def _amount_number(amount: Decimal) -> int | float:
    """Amount числом: целое — без дробной части (как печатает JS)."""
    if amount == amount.to_integral_value():
        return int(amount)
    return float(amount)


# --------------------------------------------------------------------------
# Создание платежа
# --------------------------------------------------------------------------


@dataclass(slots=True)
class InitPaymentResult:
    ok: bool
    redirect_url: str | None = None
    status_code: int | None = None
    error: str | None = None


def init_payment(
    *,
    order_id: str,
    amount: Decimal,
    description: str,
) -> InitPaymentResult:
    """Создаёт платёж в Finik, возвращает ссылку на страницу оплаты.

    Синхронный httpx — вызывается из бот-цикла через asyncio.to_thread (как
    exchange.py). Редиректы НЕ следуем: ссылка нужна из заголовка Location,
    иначе получили бы HTML страницы оплаты.
    """
    settings = get_settings()
    host = urllib.parse.urlsplit(settings.finik_base_url).netloc
    timestamp = _timestamp_ms()

    # additionalData не отправляем: связь с заказом даёт fields.paymentId, а
    # точный формат своих полей у Finik не задокументирован (вопрос в поддержку).
    body: dict[str, Any] = {
        "Amount": _amount_number(amount),
        "CardType": CARD_TYPE_QR,
        "PaymentId": order_id,
        "RedirectUrl": settings.finik_redirect_url,
        "Data": {
            "accountId": settings.finik_account_id,
            "name_en": settings.finik_qr_name,
            "webhookUrl": settings.finik_webhook_url,
            "description": description,
            "endDate": int((time.time() + settings.finik_payment_lifetime_seconds) * 1000),
        },
    }
    signed_headers = {
        "Host": host,
        "x-api-key": settings.finik_api_key,
        "x-api-timestamp": timestamp,
    }
    payload = js_json(body)

    try:
        signature = sign(
            canonical(
                method="POST",
                path=PAYMENT_PATH,
                headers=signed_headers,
                body_json=payload,
            ),
            load_private_key(),
        )
    except (OSError, ValueError) as exc:  # нет файла ключа / битый PEM
        logger.exception("Finik: cannot sign request for order %s", order_id)
        return InitPaymentResult(ok=False, error=f"signing failed: {exc}")

    url = f"{settings.finik_base_url.rstrip('/')}{PAYMENT_PATH}"
    try:
        response = httpx.post(
            url,
            content=payload.encode("utf-8"),  # ровно то, что подписали
            headers={
                "content-type": "application/json",
                "x-api-key": settings.finik_api_key,
                "x-api-timestamp": timestamp,
                "signature": signature,
            },
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        logger.exception("Finik init_payment HTTP error for order %s", order_id)
        return InitPaymentResult(ok=False, error=str(exc))

    location = response.headers.get("location")
    if response.is_redirect and location:
        return InitPaymentResult(ok=True, redirect_url=location, status_code=response.status_code)

    logger.warning(
        "Finik init_payment rejected order %s: HTTP %s %s",
        order_id,
        response.status_code,
        response.text[:500],
    )
    return InitPaymentResult(
        ok=False,
        status_code=response.status_code,
        error=response.text[:500] or f"unexpected HTTP {response.status_code}",
    )


# --------------------------------------------------------------------------
# Проверка webhook
# --------------------------------------------------------------------------


def _candidate_bodies(body: Mapping[str, Any]) -> Iterator[str]:
    """Варианты сериализации тела для проверки подписи, от самого вероятного.

    1) ключи верхнего уровня по кодпоинтам — так делает наш порт и так совпадает
       с JS для всех известных payload-ов Finik;
    2) порядок, приближающий localeCompare — страховка, если Finik добавит поле
       с подчёркиванием/другим регистром (см. _locale_like_key);
    3) порядок «как пришло» — если подписывающая сторона не сортировала вовсе.
    Подпись всё равно должна сойтись, поэтому перебор ничего не ослабляет.
    """
    seen: set[str] = set()
    for key in (None, _locale_like_key):
        candidate = js_json(body, key=key)
        if candidate not in seen:
            seen.add(candidate)
            yield candidate
    as_is = json.dumps(
        _normalize_numbers(dict(body)), separators=(",", ":"), ensure_ascii=False
    )
    if as_is not in seen:
        yield as_is


def finik_public_key() -> str:
    """Публичный ключ Finik для текущей среды (`FINIK_ENV`)."""
    env = get_settings().finik_env
    try:
        return FINIK_PUBLIC_KEYS[env]
    except KeyError:  # неизвестная среда — не молчим, иначе примем что угодно
        raise ValueError(f"unknown FINIK_ENV={env!r}, expected one of {sorted(FINIK_PUBLIC_KEYS)}")


def verify_webhook(
    *,
    method: str,
    path: str,
    headers: Mapping[str, Any],
    body: Mapping[str, Any],
    signature: str,
    query: Mapping[str, str] | None = None,
) -> bool:
    """Проверяет подпись входящего webhook публичным ключом Finik.

    Проверять приходится по ПЕРЕСОБРАННОМУ JSON (в канон входит тело с
    отсортированным верхним уровнем, поэтому сырые байты не годятся).
    x-api-timestamp у webhook НЕ проверяем: ретраи Finik идут сутками с тем же
    подписанным телом. Защита от повторного проигрывания — идемпотентность
    обработчика по order_id.
    """
    if not signature:
        return False
    public_key = finik_public_key()
    for body_json in _candidate_bodies(body):
        data = canonical(
            method=method,
            path=path,
            headers=headers,
            body_json=body_json,
            query=query,
        )
        if verify(data, signature, public_key):
            return True
    return False
