import hashlib

from app.services.freedompay import (
    RESULT_SCRIPT,
    build_callback_response,
    build_signature,
    verify_signature,
)
from app.services.freedompay import _parse_xml


def test_build_signature_orders_by_key_and_wraps_script_and_secret() -> None:
    params = {
        "pg_merchant_id": "1000",
        "pg_order_id": "42",
        "pg_amount": "500.00",
        "pg_salt": "abc",
    }
    # Ключи по алфавиту: pg_amount, pg_merchant_id, pg_order_id, pg_salt ->
    # значения 500.00, 1000, 42, abc; script спереди, secret в конце.
    expected_raw = "init_payment.php;500.00;1000;42;abc;secret"
    expected = hashlib.md5(expected_raw.encode()).hexdigest()
    assert build_signature("init_payment.php", params, "secret") == expected


def test_build_signature_excludes_pg_sig() -> None:
    base = {"pg_a": "1", "pg_b": "2"}
    with_sig = {**base, "pg_sig": "deadbeef"}
    assert build_signature("result", with_sig, "s") == build_signature("result", base, "s")


def test_verify_signature_roundtrip_and_tamper() -> None:
    params = {"pg_order_id": "7", "pg_result": "1", "pg_salt": "xyz"}
    params["pg_sig"] = build_signature(RESULT_SCRIPT, params, "topsecret")
    assert verify_signature(RESULT_SCRIPT, params, "topsecret") is True
    # Подделка суммы/полей ломает подпись.
    tampered = {**params, "pg_result": "0"}
    assert verify_signature(RESULT_SCRIPT, tampered, "topsecret") is False
    # Неверный secret тоже.
    assert verify_signature(RESULT_SCRIPT, params, "wrong") is False


def test_parse_xml_flat_response() -> None:
    payload = (
        b'<?xml version="1.0" encoding="utf-8"?><response>'
        b"<pg_status>ok</pg_status><pg_payment_id>555</pg_payment_id>"
        b"<pg_redirect_url>https://pay/x</pg_redirect_url></response>"
    )
    fields = _parse_xml(payload)
    assert fields["pg_status"] == "ok"
    assert fields["pg_payment_id"] == "555"
    assert fields["pg_redirect_url"] == "https://pay/x"


def test_callback_response_is_valid_signed_xml() -> None:
    xml = build_callback_response("ok", "accepted")
    fields = _parse_xml(xml.encode())
    assert fields["pg_status"] == "ok"
    # Подпись ответа проверяется той же схемой (secret по умолчанию пуст в тестах).
    assert verify_signature(RESULT_SCRIPT, fields, "") is True
