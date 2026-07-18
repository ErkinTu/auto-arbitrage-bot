"""Аудит данных lalafo.kg по всем отслеживаемым маркам (read-only, БД не меняет).

Для каждой марки: свежая страница ленты -> карточка каждого объявления ->
нормализация -> сверка модели с каталогом (model_aliases). Отчёт: даты, валюты,
нераспознанные модели (кандидаты в алиасы), расхождения заголовка и параметров.

    PYTHONPATH=. uv run python scripts/lalafo_brand_audit.py --delay 0.8
    PYTHONPATH=. uv run python scripts/lalafo_brand_audit.py --brands Toyota,BYD --per-brand 5
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import Brand, CarModel, ModelAlias
from app.parsers.lalafo import LalafoParser
from app.services.catalog import normalize_alias_text
from app.services.normalizer import normalize_lalafo_listing


def load_alias_map(brand_name: str) -> dict[str, str] | None:
    """normalized_alias -> каноническое имя модели. None, если марки нет в БД."""
    with SessionLocal() as session:
        brand = session.scalar(select(Brand).where(Brand.name == brand_name))
        if brand is None:
            return None
        rows = session.execute(
            select(ModelAlias.normalized_alias, CarModel.name)
            .join(CarModel, CarModel.id == ModelAlias.car_model_id)
            .where(ModelAlias.brand_id == brand.id)
        ).all()
        return {alias: model for alias, model in rows}


def audit_brand(parser: LalafoParser, brand: str, per_brand: int) -> dict:
    alias_map = load_alias_map(brand)
    page = parser.fetch_listings(
        brand=brand, model=None, created_period=None,
        page=1, size=20, sort_by="newest", order="desc",
    )
    items = sorted(page.items, key=lambda item: item.get("created_time") or 0, reverse=True)

    rows = []
    for item in items[:per_brand]:
        try:
            detail = parser.fetch_details(item["id"])
        except Exception as exc:  # noqa: BLE001 - аудит должен дойти до конца
            rows.append({"id": item["id"], "error": repr(exc), "title": item.get("title")})
            continue

        norm = normalize_lalafo_listing(detail, fallback_make=brand)
        model_src = norm.source_model_text
        resolved = None
        if alias_map is not None and model_src:
            resolved = alias_map.get(normalize_alias_text(model_src))

        params_by_name = {p.get("name"): p.get("value") for p in detail.get("params") or []}
        rows.append(
            {
                "id": norm.external_id,
                "title": norm.title,
                "model_src": model_src,
                "model_resolved": resolved,
                "model_in_params": "Модель" in params_by_name,
                "year": norm.year,
                "mileage_km": norm.mileage_km,
                "price_amount": str(norm.price_amount) if norm.price_amount is not None else None,
                "price_currency": norm.price_currency,
                "price_kgs": str(norm.price_kgs) if norm.price_kgs is not None else None,
                "price_usd": str(norm.price_usd) if norm.price_usd is not None else None,
                "created_at": norm.source_created_at.isoformat() if norm.source_created_at else None,
                "city": norm.city,
                "customs": norm.customs_status,
                "gearbox": norm.gearbox,
                "fuel": norm.fuel_type,
                "engine_volume": str(norm.engine_volume) if norm.engine_volume is not None else None,
                "quality_flags": norm.quality_flags,
                "is_vip": bool(item.get("is_vip")),
            }
        )

    return {
        "brand": brand,
        "category_id": parser.resolve_category_id(brand),
        "total_in_category": page.total,
        "brand_in_db": alias_map is not None,
        "alias_count": len(alias_map or {}),
        "rows": rows,
    }


def print_report(results: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    all_unknown: Counter[tuple[str, str]] = Counter()

    for res in results:
        rows = [r for r in res["rows"] if "error" not in r]
        errors = [r for r in res["rows"] if "error" in r]
        currencies = Counter(r["price_currency"] or "нет цены" for r in rows)
        no_params = [r for r in rows if not r["model_in_params"]]
        unknown = [r for r in rows if r["model_src"] and not r["model_resolved"]]
        no_date = [r for r in rows if not r["created_at"]]
        no_year = [r for r in rows if not r["year"]]
        no_mileage = [r for r in rows if r["mileage_km"] is None]
        both_prices = sum(1 for r in rows if r["price_kgs"] and r["price_usd"])

        ages = []
        for r in rows:
            if r["created_at"]:
                ages.append((now - datetime.fromisoformat(r["created_at"])).total_seconds() / 3600)

        print(f"\n=== {res['brand']} (category_id={res['category_id']}, всего {res['total_in_category']}) ===")
        print(
            f"проверено {len(rows)} | валюты: {dict(currencies)} | обе цены (KGS+USD): {both_prices} | "
            f"без даты: {len(no_date)} | без года: {len(no_year)} | без пробега: {len(no_mileage)}"
        )
        if ages:
            print(f"возраст объявлений: свежайшее {min(ages):.1f} ч, старейшее {max(ages) / 24:.1f} дн")
        if not res["brand_in_db"]:
            print("!! марки нет в каталоге БД")
        if no_params:
            print(f"!! без параметра «Модель» ({len(no_params)}):")
            for r in no_params:
                print(f"    [{r['id']}] {r['title']!r} -> из заголовка: {r['model_src']!r}")
        if unknown:
            print(f"!! модели без алиаса ({len(unknown)}):")
            for r in unknown:
                all_unknown[(res["brand"], r["model_src"])] += 1
                print(f"    [{r['id']}] модель={r['model_src']!r} title={r['title']!r}")
        for r in errors:
            print(f"!! ошибка карточки [{r['id']}]: {r['error']}")

    print("\n===== ИТОГ: кандидаты в алиасы =====")
    if not all_unknown:
        print("все модели распознаны существующими алиасами")
    for (brand, model), count in all_unknown.most_common():
        print(f"  {brand}: {model!r} x{count}")


def main() -> None:
    arg_parser = argparse.ArgumentParser(description="lalafo.kg brand data audit")
    arg_parser.add_argument("--brands", help="Список через запятую; по умолчанию ALLOWED_BRANDS из .env")
    arg_parser.add_argument("--per-brand", type=int, default=24, help="Максимум карточек на марку")
    arg_parser.add_argument("--delay", type=float, default=0.8, help="Пауза между запросами, сек")
    arg_parser.add_argument("--json-out", help="Куда сохранить полный JSON")
    args = arg_parser.parse_args()

    settings = get_settings()
    settings.request_delay_seconds = args.delay
    brands = (
        [b.strip() for b in args.brands.split(",") if b.strip()]
        if args.brands
        else settings.allowed_brand_list
    )

    parser = LalafoParser()
    results = []
    try:
        for brand in brands:
            print(f"[{datetime.now():%H:%M:%S}] {brand}...", flush=True)
            try:
                results.append(audit_brand(parser, brand, args.per_brand))
            except Exception as exc:  # noqa: BLE001
                print(f"!! {brand}: аудит не удался: {exc!r}")
    finally:
        parser.close()

    print_report(results)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, ensure_ascii=False, indent=1))
        print(f"\nполный JSON: {args.json_out}")


if __name__ == "__main__":
    main()
