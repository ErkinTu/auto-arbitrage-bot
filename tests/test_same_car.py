"""Хэши фото и метка «Вероятно, та же машина» (план 18.11 № 27 и № 20)."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import httpx
from PIL import Image

from app.models import ListingPhotoHash, ListingSameCar
from app.services import photo_hashes, same_car
from app.services.photo_hashes import dhash, frame_urls, hash_pass
from app.services.same_car import Item, match_pairs
from tests.test_public_feed import _listing, client, db  # noqa: F401

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
A = 0x0F0F_3C3C_5A5A_A5A5  # «информативные» хэши: 6..58 единиц
B = A ^ 0b111  # 3 бита от A — тот же снимок
C = 0x00FF_00FF_F0F0_0F0F  # другой снимок


def _png(pattern: str) -> bytes:
    img = Image.new("RGB", (90, 80), "white")
    for x in range(90):
        for y in range(80):
            if pattern == "stripes" and (x // 10) % 2:
                img.putpixel((x, y), (0, 0, 0))
            if pattern == "grad":
                img.putpixel((x, y), (x * 2, y * 3, 100))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_dhash_stable_under_resize_and_different_for_other_image() -> None:
    big = _png("stripes")
    small = io.BytesIO()
    Image.open(io.BytesIO(big)).resize((45, 40)).save(small, "JPEG", quality=70)
    h1, h2, h3 = dhash(big), dhash(small.getvalue()), dhash(_png("grad"))
    assert h1 is not None and h2 is not None and h3 is not None
    assert photo_hashes.distance(h1, h2) <= 8
    assert photo_hashes.distance(h1, h3) > 8
    assert dhash(b"not an image") is None


def test_frame_urls_skip_placeholder_dedupe_and_shrink_lalafo() -> None:
    cover = "https://img5.lalafo.com/i/posters/original/aa/1.jpeg"
    gallery = [cover, "https://s3.mashina.kg/assets/preview.png", "https://x/2.jpg", "https://x/3.jpg", "https://x/4.jpg"]
    assert frame_urls(cover, gallery, 3) == [
        "https://img5.lalafo.com/i/posters/api/aa/1.jpeg",
        "https://x/2.jpg",
        "https://x/3.jpg",
    ]


def _item(i, *, block=(1, 1, 2019), fuel=None, km=90_000, hashes=(A,)):
    return Item(i, block, fuel, km, tuple(hashes))


def test_match_pairs_rules() -> None:
    items = [
        _item(1),
        _item(2, hashes=(C, B)),  # совпал второй кадр
        _item(3, hashes=(C ^ 0xFFFF_0000,)),  # другой снимок (далеко и от A, и от C)
        _item(4, block=(1, 1, 2020)),  # другой год
        _item(5, fuel="diesel"),
        _item(6, fuel="petrol"),  # 5 и 6 — топливо противоречит
        _item(7, km=200_000),  # пробег противоречит
    ]
    pairs = match_pairs(items, 8)
    assert (1, 2) in pairs and pairs[(1, 2)] == 3
    assert not any(3 in p or 4 in p or 7 in p for p in pairs)
    assert (5, 6) not in pairs
    assert (1, 5) in pairs and (1, 6) in pairs  # NULL-топливо не конфликт


def test_generic_and_crowded_frames_ignored() -> None:
    # Один и тот же кадр у 7 объявлений (баннер салона) — не признак.
    banner = [_item(i, hashes=(A,)) for i in range(1, 8)]
    assert match_pairs(banner, 8) == {}
    # Однотонный кадр (почти все нули) — не сравниваем.
    assert match_pairs([_item(1, hashes=(1,)), _item(2, hashes=(1,))], 8) == {}
    # 7 объявлений с близкими, но не равными кадрами — шаблон дилера: у каждого > 5 пар.
    near = [_item(i, hashes=(A ^ (1 << i),)) for i in range(1, 8)]
    assert match_pairs(near, 8) == {}


class _Transport(httpx.BaseTransport):
    def __init__(self, body: bytes):
        self.body, self.urls = body, []

    def handle_request(self, request):
        self.urls.append(str(request.url))
        if "broken" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=self.body)


class _NoPace:
    def wait(self) -> None:
        pass


def test_hash_pass_then_rebuild_and_api(client) -> None:  # noqa: F811
    api, Factory, ids = client
    with Factory() as s:
        old = _listing(s, ids, image_url="https://x/a1.jpg", is_active=False, deactivated_at=NOW - timedelta(days=5),
                       current_price_usd=21000)
        new = _listing(s, ids, image_url="https://y/b1.jpg", source_id=ids["lalafo"], mileage_km=91_000)
        fresh = _listing(s, ids, image_url="https://x/new.jpg", mileage_km=0)  # новая — не хэшируем
        broken = _listing(s, ids, image_url="https://x/broken.jpg")
        old_id, new_id, fresh_id, broken_id = old.id, new.id, fresh.id, broken.id

    transport = _Transport(_png("stripes"))
    with Factory() as s, httpx.Client(transport=transport) as http:
        stats = hash_pass(s, http, limit=50, now=NOW, pacer=_NoPace())
    assert stats["listings"] == 2  # снятое не хэшируем, новую — тоже; broken — попытка с 0 кадров
    assert not any("new.jpg" in u for u in transport.urls)
    with Factory() as s:
        assert s.get(ListingPhotoHash, broken_id).frames == 0
        assert s.get(ListingPhotoHash, fresh_id) is None
        # снятое объявление хэшировали, пока было активным
        s.add(ListingPhotoHash(listing_id=old_id, cover_url="https://x/a1.jpg",
                               hashes=list(s.get(ListingPhotoHash, new_id).hashes), frames=1, hashed_at=NOW))
        s.commit()
        # повторный проход ничего не делает (broken — повтор только через сутки)
        with httpx.Client(transport=transport) as http:
            assert hash_pass(s, http, limit=50, now=NOW, pacer=_NoPace())["listings"] == 0
        assert same_car.rebuild(s, now=datetime.now(timezone.utc))["pairs"] == 1
        assert s.query(ListingSameCar).count() == 2

    detail = api.get(f"/api/v1/public/listings/{new_id}").json()
    assert [x["id"] for x in detail["same_car"]] == [old_id]
    assert detail["same_car"][0]["is_active"] is False
    assert detail["same_car_count"] == 1
    feed = api.get("/api/v1/public/feed").json()["items"]
    counts = {x["id"]: x["same_car_count"] for x in feed}
    assert counts[new_id] == 1 and counts[broken_id] == 0
