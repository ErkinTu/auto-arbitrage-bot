"""Клиент FreedomPay (freedompay.kg) для разовой оплаты подписки.

Поток: init_payment (сервер→сервер) отдаёт pg_redirect_url, куда ведём юзера;
после оплаты FreedomPay POST-ит Result URL (webhook), на который отвечаем
подписанным XML. Recurring-профили НЕ используем — оплата разовая.

Подпись pg_sig: md5 от `script_name` + значений всех pg-полей (кроме pg_sig),
отсортированных по имени ключа, и secret_key — всё через ';', в нижнем hex.
Одна схема для формирования исходящих запросов и проверки входящего webhook.
Доки: freedompay.kz/docs-en/merchant-api/{intro,pay} (движок PayBox, .kg — те же).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping
from xml.sax.saxutils import escape

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

INIT_SCRIPT = "init_payment.php"
# Имя «скрипта» для подписи webhook = последний сегмент нашего Result URL
# (.../api/v1/payments/result). FreedomPay берёт его из настроенного result_url.
RESULT_SCRIPT = "result"


def _salt() -> str:
    return secrets.token_hex(8)


def build_signature(script_name: str, params: Mapping[str, object], secret_key: str) -> str:
    """md5(script_name; <значения pg-полей кроме pg_sig, по алфавиту ключей>; secret)."""
    parts = [script_name]
    for key in sorted(params):
        if key == "pg_sig":
            continue
        parts.append(str(params[key]))
    parts.append(secret_key)
    raw = ";".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def verify_signature(script_name: str, params: Mapping[str, object], secret_key: str) -> bool:
    """Проверяет pg_sig во входящем сообщении FreedomPay (const-time сравнение)."""
    received = str(params.get("pg_sig", ""))
    expected = build_signature(script_name, params, secret_key)
    return secrets.compare_digest(received, expected)


def _parse_xml(payload: bytes) -> dict[str, str]:
    """Плоский XML-ответ FreedomPay -> {tag: text} по прямым детям <response>."""
    root = ET.fromstring(payload)
    return {child.tag: (child.text or "") for child in root}


@dataclass(slots=True)
class InitPaymentResult:
    ok: bool
    payment_id: str | None = None
    redirect_url: str | None = None
    error_code: str | None = None
    error_description: str | None = None


def init_payment(
    *,
    order_id: str,
    amount: Decimal,
    description: str,
    currency: str = "KGS",
) -> InitPaymentResult:
    """Создаёт платёж в FreedomPay, возвращает redirect_url для оплаты.

    Result/success/failure URL и креды берутся из настроек. Синхронный httpx —
    вызывается из бот-цикла через asyncio.to_thread (как exchange.py).
    """
    settings = get_settings()
    params: dict[str, object] = {
        "pg_merchant_id": settings.freedompay_merchant_id,
        "pg_order_id": order_id,
        "pg_amount": f"{amount:.2f}",
        "pg_currency": currency,
        "pg_description": description,
        "pg_result_url": settings.freedompay_result_url,
        "pg_success_url": settings.freedompay_success_url,
        "pg_failure_url": settings.freedompay_failure_url,
        "pg_lifetime": str(settings.freedompay_payment_lifetime_seconds),
        "pg_testing_mode": "1" if settings.freedompay_testing_mode else "0",
        "pg_salt": _salt(),
    }
    params["pg_sig"] = build_signature(INIT_SCRIPT, params, settings.freedompay_secret_key)

    url = f"{settings.freedompay_base_url.rstrip('/')}/{INIT_SCRIPT}"
    try:
        response = httpx.post(url, data=params, timeout=settings.request_timeout_seconds)
        response.raise_for_status()
        fields = _parse_xml(response.content)
    except httpx.HTTPError as exc:
        logger.exception("FreedomPay init_payment HTTP error for order %s", order_id)
        return InitPaymentResult(ok=False, error_description=str(exc))
    except ET.ParseError:
        logger.exception("FreedomPay init_payment bad XML for order %s", order_id)
        return InitPaymentResult(ok=False, error_description="bad response")

    if fields.get("pg_status") != "ok":
        logger.warning(
            "FreedomPay init_payment rejected order %s: %s %s",
            order_id,
            fields.get("pg_error_code"),
            fields.get("pg_error_description"),
        )
        return InitPaymentResult(
            ok=False,
            error_code=fields.get("pg_error_code"),
            error_description=fields.get("pg_error_description"),
        )
    return InitPaymentResult(
        ok=True,
        payment_id=fields.get("pg_payment_id"),
        redirect_url=fields.get("pg_redirect_url"),
    )


def build_callback_response(status: str, description: str) -> str:
    """Подписанный XML-ответ на Result-URL webhook (<response>…</response>).

    status: 'ok' — платёж принят, 'rejected' — отклоняем (если pg_can_reject=1),
    'error' — временная ошибка (FreedomPay повторит вызов).
    """
    secret_key = get_settings().freedompay_secret_key
    body: dict[str, object] = {
        "pg_salt": _salt(),
        "pg_status": status,
        "pg_description": description,
    }
    body["pg_sig"] = build_signature(RESULT_SCRIPT, body, secret_key)
    fields = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in body.items())
    return f'<?xml version="1.0" encoding="utf-8"?><response>{fields}</response>'
