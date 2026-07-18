"""Смоук-проверка парсера lalafo.kg без БД.

    uv run python scripts/lalafo_smoke.py Toyota --limit 5 --details
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from app.parsers.lalafo import LalafoParser


def main() -> None:
    arg_parser = argparse.ArgumentParser(description="lalafo.kg parser smoke test")
    arg_parser.add_argument("brand", help="Марка как в .env, например Toyota или Mercedes-Benz")
    arg_parser.add_argument("--limit", type=int, default=5, help="Сколько объявлений показать")
    arg_parser.add_argument("--details", action="store_true", help="Дотянуть карточку первого объявления")
    args = arg_parser.parse_args()

    parser = LalafoParser()
    try:
        category_id = parser.resolve_category_id(args.brand)
        print(f"brand={args.brand} -> category_id={category_id}")

        page = parser.fetch_listings(
            brand=args.brand,
            model=None,
            created_period=None,
            page=1,
            size=max(args.limit, 20),
            sort_by="newest",
            order="desc",
        )
        print(f"total in category: {page.total}, fetched: {len(page.items)}\n")

        items = sorted(page.items, key=lambda item: item.get("created_time") or 0, reverse=True)
        for item in items[: args.limit]:
            created = datetime.fromtimestamp(item["created_time"], tz=timezone.utc)
            price = f"{item.get('price')} {item.get('currency')}" if item.get("price") else "цена не указана"
            print(f"[{item['id']}] {item.get('title')}")
            print(f"    {price} | {item.get('city')} | создано {created:%Y-%m-%d %H:%M} UTC")
            print(f"    https://lalafo.kg{item.get('url')}")

        if args.details and items:
            detail = parser.fetch_details(items[0]["id"])
            print(f"\nДетали [{detail['id']}]:")
            for param in detail.get("params", []):
                print(f"    {param.get('name')}: {param.get('value')}")
    finally:
        parser.close()


if __name__ == "__main__":
    main()
