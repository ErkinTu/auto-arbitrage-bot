"""Тесты подписи Finik.

Векторы получены из НАСТОЯЩЕЙ библиотеки Finik `@mancho.devs/authorizer@2.12.8`
(её `Signer` подписан тестовым ключом из `package/__test__/testData.js`) и
сверены с нашим портом байт в байт — см. `docs/PAYMENTS_FINIK_ANALYSIS.md §4`.
Если Finik молча поменяет каноникализацию, эти тесты покраснеют раньше, чем
первый платёж отвалится с 401.
"""

from decimal import Decimal

import pytest

from app.services.finik import (
    FINIK_PUBLIC_KEYS,
    _amount_number,
    _locale_like_key,
    canonical,
    js_json,
    sign,
    verify,
)

# Тестовая пара ключей из самой библиотеки (не боевая, публикуется в пакете).
TEST_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEAoq/3UxpzgfXOLrJEZd39TJ7Fc4iRLMcK1Hj563O+sNcP4BnA
eNnRXfCCJCLs+UZxPLxV7f1Zj7zHmgUOsB6rPS3TJwbIIbclDrhoySzPAnTNg+jU
9lpPS99vvT8tJpUb0zAA6sAwFxHUQk43mzgLou04eMlSHZmZ4MFVGzWGakIwDRlP
MQbQCOiU+N/0Kl7qBDwDkn6USwc16MTa2Xm6BAhcwaug0jlhN5NIu7hyK2gk8In1
S0OrxBEK3Z1zShRhixGEhA8Z7NREJCMuqHke09dVK1nVXz6CGTQMafjDKDwgmiBR
7cJo25Icb/OqFs4TzE7XPfU/3gK0+1YdqF4J/wIDAQABAoIBADY3DeTT28pjb+J/
5etMnyz5fDOUP0z8x88mwaKFX/butIuqCUo8zFjR3YzNVst7fiEPYlN9KmuMLbhW
Fx76GXa53rQSvn792YueSM1B8XqQEizzWoT+c46FV2dptlI1PqCSTrvscBpABsxR
5JYFh1s0Uq1t6b+TgIQ16Xa3z6N5AyLVw7MbXEx2iB/bVdivmWNIjLWarr4yJDwA
MgB/dOYtZp80ut8yOYYvaTmUVBUEp4ZHjBN+MgNlMD0zDZR/y+5mr1r3TfuPUsw9
eIDpD6tYwHvDgoneSGi1LtQpuEXTKhwEFezJkLZZnVCtFCdRaKkS607fT37fJOML
DfNOgYECgYEAzpzgjzCz3+2qHJl+ejRtw2cUxCOIvVBAzvbqVk8R/NqZVF2WteUh
x/0lM+FK0/AHpcnpZu79ruvra3yaB/AZuaPOnrsjzLQ2xsJfLpEu0Caoho3jWP7J
FDGhX1CQeRXEQ9VM6l+PbjoKZ4bRO6ktVMuq/D7gBNvh/mNglCziLLsCgYEAyZMw
O91zk+4DJucflLuNKiS56J8Sq71v6j+iFwF6zimYJJAFm7xdQ2vWXSP5jf5SeKVr
Q/aA0ACSi0iYBEzhtQ19cCydjYQKSRvO0olMvRbcJfVgABCN+JtFIaSnWLS/sXST
zcH3vm4FgccRHtt27HQIVCFHgwwVapymAYfnRY0CgYAPKTHNMAyy7NSjvpuqSfiX
8xNyBQ1+nsnypemyJaEzRbMknq11cXfWHfxB31FHVgCpqLRIylaxJDylKYJ//J1W
ou+BdEf/OGYglZi4aQzfV0bcgMLi/+cvZSjrPpUrXW6Gb7tyI0r6EqY6zIjD8PkT
lNJaKh70HFJsAUzP8q8yCwKBgQCFyFJy6P8UZxtgbnTfWbrPBaD9atYRdaEZbzI8
4paGzcRUP+H5AoNDhAa5um6edvR1bhRK/wdvBXI9TujVsdD7QQDHulS237OT4gja
YpWzycBC0R/t6w7OuP6g3Y7TqOKw/BY8sUej85FkGKKcQDwlor0EWTIFH7f3EhB7
Y59y4QKBgB5K2pRrw3wjQOHlssEO/2FqwE1NTYZalgpghPLRO+4Y8qM8hkKsBjgQ
AQhd66mWIbIvuvcGErUrz3qFJHtjTIZHqOSVlW3hiiuMVKzZCoPa8nUJ0l6ylTpz
NIb/T0HUwXgSRJBNhdHNo0yGFisaJpezF/kDUhOCzvTdoNI/M0Zy
-----END RSA PRIVATE KEY-----
"""

TEST_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAoq/3UxpzgfXOLrJEZd39
TJ7Fc4iRLMcK1Hj563O+sNcP4BnAeNnRXfCCJCLs+UZxPLxV7f1Zj7zHmgUOsB6r
PS3TJwbIIbclDrhoySzPAnTNg+jU9lpPS99vvT8tJpUb0zAA6sAwFxHUQk43mzgL
ou04eMlSHZmZ4MFVGzWGakIwDRlPMQbQCOiU+N/0Kl7qBDwDkn6USwc16MTa2Xm6
BAhcwaug0jlhN5NIu7hyK2gk8In1S0OrxBEK3Z1zShRhixGEhA8Z7NREJCMuqHke
09dVK1nVXz6CGTQMafjDKDwgmiBR7cJo25Icb/OqFs4TzE7XPfU/3gK0+1YdqF4J
/wIDAQAB
-----END PUBLIC KEY-----
"""

# --- вектор 1: пример из README библиотеки (юникод в query, лишние заголовки) --
VECTOR_1_REQUEST = dict(
    method="POST",
    path="/services",
    headers={
        "Host": "api.paymentsgateway.averspay.kg",
        "x-api-timestamp": "1636026186643",
        "x-api-key": "h8z1TDStxu5YY2YuN8jUa9hpzIVbfkLT7kPiPiYj",
        "x-api-header": "header value",
        "X-Forwarded-For": "54.187.127.20, 15.158.4.100",
    },
    body={"payment": "payment", "amount": 100},
    query={"from": "0", "size": "10", "best": "тест дата"},
)
VECTOR_1_CANONICAL = (
    "post\n"
    "/services\n"
    "host:api.paymentsgateway.averspay.kg&x-api-header:header value"
    "&x-api-key:h8z1TDStxu5YY2YuN8jUa9hpzIVbfkLT7kPiPiYj&x-api-timestamp:1636026186643\n"
    "best=%D1%82%D0%B5%D1%81%D1%82%20%D0%B4%D0%B0%D1%82%D0%B0&from=0&size=10\n"
    '{"amount":100,"payment":"payment"}'
)
VECTOR_1_SIGNATURE = (
    "etPE40DBxzNIXT9UX8St905X2RijGPLGNw68SEn2vPB5aoCnShnVW1vJJGgKX65wio2g3dwZIGME"
    "+dKH7UVV0kNdx/0WmRcs21I2/4k/qUskxlDGgytGvRD2Ms+eb8h1VE1j5NZLeGwWDE7unXx3Okkj"
    "stDTsbnGK0kw6wuoFJMj60zElREE0853OMLZVsYBDHSDfUhwZopfxnBFl24rZHiN9WKllqiOd0WB"
    "m7+0Rtw+EG4/G8N7/XEDfz3KoSoIM4tyBAU+PSEOxy2VeVO4mXDcHBPZJzcNnGcYKawG1vwRId6d"
    "GS426a8QkE09ZW19baihMJZmp7LN4UWyACGn+w=="
)

# --- вектор 2: реальное тело Finik-платежа (вложенный Data не сортируется) -----
VECTOR_2_REQUEST = dict(
    method="POST",
    path="/v1/payment",
    headers={
        "Host": "api.acquiring.averspay.kg",
        "x-api-key": "test-key",
        "x-api-timestamp": "1737369012345",
    },
    body={
        "RedirectUrl": "https://example.com/success",
        "Amount": 500,
        "CardType": "FINIK_QR",
        "PaymentId": "00000000-0000-0000-0000-000000000000",
        "Data": {
            "webhookUrl": "https://x.kg/hook",
            "accountId": "acc-1",
            "name_en": "Подписка №1",
            "additionalData": [{"fieldId": "order", "value": "abc"}],
        },
    },
)
VECTOR_2_CANONICAL = (
    "post\n"
    "/v1/payment\n"
    "host:api.acquiring.averspay.kg&x-api-key:test-key&x-api-timestamp:1737369012345\n"
    '{"Amount":500,"CardType":"FINIK_QR","Data":{"webhookUrl":"https://x.kg/hook",'
    '"accountId":"acc-1","name_en":"Подписка №1","additionalData":'
    '[{"fieldId":"order","value":"abc"}]},'
    '"PaymentId":"00000000-0000-0000-0000-000000000000",'
    '"RedirectUrl":"https://example.com/success"}'
)
VECTOR_2_SIGNATURE = (
    "NvYRs9F5V33D9xtLzbAZKCyzSzpsl98TruszP8q+GiG7nTiSjPB6x4txN7mJseRLfPp4MJcgKxt8"
    "9dfj3F7zlmWgFkQpEpylrEjTRpbbCW8Iu99KNWPM2vuy+rB8tlyBvX/qQmGygpIx+MVWE3C8fR+K"
    "HNkbA6dLA+DMWfhmz/3l+2egjSXAtbT2LnyR1WX0qD0T/QAO9ckur1i9lsXbQRnd/no9qHT5DtFW"
    "/VAUeHrtNXBBOp1a1lwXfdyvJaHklbjTtRZEY+hmGVwz0EmU8ykN6mfI3ajb4hZ1lUF6kZHY3ADd"
    "/y2AjjpZxKxS4WWD/06c0IIAV0KsFaw5h0fE1w=="
)

VECTORS = [
    pytest.param(VECTOR_1_REQUEST, VECTOR_1_CANONICAL, VECTOR_1_SIGNATURE, id="readme-example"),
    pytest.param(VECTOR_2_REQUEST, VECTOR_2_CANONICAL, VECTOR_2_SIGNATURE, id="finik-payment"),
]


@pytest.mark.parametrize("request_data,expected_canonical,_signature", VECTORS)
def test_canonical_matches_reference_vector(request_data, expected_canonical, _signature) -> None:
    assert canonical(**request_data) == expected_canonical


@pytest.mark.parametrize("request_data,_canonical,expected_signature", VECTORS)
def test_signature_matches_reference_vector(request_data, _canonical, expected_signature) -> None:
    data = canonical(**request_data)
    assert sign(data, TEST_PRIVATE_KEY) == expected_signature
    assert verify(data, expected_signature, TEST_PUBLIC_KEY) is True


def test_verify_rejects_tampered_data_and_garbage_signature() -> None:
    data = canonical(**VECTOR_2_REQUEST)
    assert verify(data + " ", VECTOR_2_SIGNATURE, TEST_PUBLIC_KEY) is False
    assert verify(data, "not-base64!!", TEST_PUBLIC_KEY) is False
    assert verify(data, "", TEST_PUBLIC_KEY) is False


def test_nested_object_keys_are_not_sorted() -> None:
    """Сортируется только верхний уровень — вложенный Data идёт как есть."""
    body = {"B": 1, "A": {"z": 1, "a": 2}}
    assert js_json(body) == '{"A":{"z":1,"a":2},"B":1}'


def test_only_host_and_x_api_headers_are_signed() -> None:
    """content-type и X-Forwarded-* не должны влиять на подпись."""
    base = {"Host": "h", "x-api-key": "k"}
    noisy = {**base, "content-type": "application/json", "X-Forwarded-Proto": "https"}
    args = dict(method="POST", path="/v1/payment", body={"a": 1})
    assert canonical(headers=base, **args) == canonical(headers=noisy, **args)


def test_missing_host_header_is_an_error() -> None:
    with pytest.raises(ValueError):
        canonical(method="POST", path="/x", headers={"x-api-key": "k"})


def test_integral_floats_serialize_like_js() -> None:
    """Грабля: JS печатает 1500.00 как 1500, Python — как 1500.0.

    Без нормализации подпись webhook с «ровной» суммой не сходится.
    """
    assert js_json({"amount": 1500.00, "fee": 0.1}) == '{"amount":1500,"fee":0.1}'
    # Вложенные числа (webhook.fields.amount) тоже нормализуются.
    assert js_json({"fields": {"amount": 2000.0}}) == '{"fields":{"amount":2000}}'


def test_our_payload_keys_sort_identically_in_both_orders() -> None:
    """Фиксирует допущение: для НАШИХ тел codepoint-сортировка == localeCompare.

    JS сортирует ключи через localeCompare; для PascalCase-запроса и
    camelCase-webhook порядки совпадают, поэтому основной путь — обычный sorted().
    Если Finik добавит ключ с подчёркиванием/иным регистром, тест покраснеет, а
    проверка webhook переживёт это за счёт фолбэка в _candidate_bodies.
    """
    request_keys = ["Amount", "CardType", "Data", "PaymentId", "RedirectUrl"]
    webhook_keys = [
        "amount",
        "fields",
        "id",
        "requestDate",
        "service",
        "status",
        "transactionDate",
        "transactionId",
    ]
    for keys in (request_keys, webhook_keys):
        assert sorted(keys) == sorted(keys, key=_locale_like_key)


def test_locale_like_key_reproduces_js_locale_compare() -> None:
    """Фолбэк-порядок совпадает с тем, что даёт JS localeCompare (замер в доке)."""
    keys = ["Amount", "accountId", "Zebra", "_x", "name_en", "nameEn"]
    assert sorted(keys, key=_locale_like_key) == [
        "_x",
        "accountId",
        "Amount",
        "name_en",
        "nameEn",
        "Zebra",
    ]


def test_amount_number_drops_trailing_zeros() -> None:
    assert _amount_number(Decimal("2000.00")) == 2000
    assert isinstance(_amount_number(Decimal("2000.00")), int)
    assert _amount_number(Decimal("1.50")) == 1.5


def test_finik_public_keys_are_valid_rsa_for_both_envs() -> None:
    """Ключи Finik сняты со страницы справочника — они должны разбираться."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    for env, pem in FINIK_PUBLIC_KEYS.items():
        key = load_pem_public_key(pem.encode())
        assert isinstance(key, rsa.RSAPublicKey), env
        assert key.key_size == 2048, env
