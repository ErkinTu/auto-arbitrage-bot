from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal, init_db
from app.models import Brand, NormalizationIssue, Source, SyncTarget
from app.parsers.mashina import MashinaParser
from app.services.catalog_service import (
    add_generation_alias,
    add_model_alias,
    reprocess_catalog,
    seed_default_catalog,
)
from app.services.lookup import get_or_create_source
from app.services.stats import rebuild_daily_market_stats
from app.services.sync import run_sync


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_seed_data() -> None:
    settings = get_settings()
    with SessionLocal() as session:
        source = get_or_create_source(session, "mashina.kg", settings.mashina_api_url)
        seed_default_catalog(session)
        for priority, brand in enumerate(settings.allowed_brand_list, start=1):
            target = session.scalar(
                select(SyncTarget).where(
                    SyncTarget.source_id == source.id,
                    SyncTarget.brand_filter == brand,
                    SyncTarget.model_filter.is_(None),
                    SyncTarget.generation_filter.is_(None),
                )
            )
            if target is None:
                session.add(
                    SyncTarget(
                        source_id=source.id,
                        brand_filter=brand,
                        enabled=True,
                        priority=priority,
                        fresh_period="1d",
                        page_size=settings.default_page_size,
                        max_pages=settings.default_max_pages,
                        max_items=settings.default_max_items,
                        sync_interval_minutes=settings.default_sync_interval_minutes,
                    )
                )
        session.commit()


def find_or_create_target(session, source: Source, args) -> SyncTarget:
    target = session.scalar(
        select(SyncTarget).where(
            SyncTarget.source_id == source.id,
            SyncTarget.brand_filter == args.brand,
            SyncTarget.model_filter == args.model,
            SyncTarget.generation_filter == args.generation,
        )
    )
    if target is None:
        settings = get_settings()
        target = SyncTarget(
            source_id=source.id,
            brand_filter=args.brand,
            model_filter=args.model,
            generation_filter=args.generation,
            enabled=True,
            priority=100,
            fresh_period=args.period or "1d",
            page_size=args.page_size or settings.default_page_size,
            max_pages=args.max_pages or settings.default_max_pages,
            max_items=args.max_items or settings.default_max_items,
            sync_interval_minutes=settings.default_sync_interval_minutes,
        )
        session.add(target)
        session.commit()
        session.refresh(target)
    return target


def command_init_db(_: argparse.Namespace) -> None:
    init_db()
    print("Database tables are ready.")


def command_seed(_: argparse.Namespace) -> None:
    init_db()
    ensure_seed_data()
    print("Seeded mashina.kg targets for configured brands.")


def command_targets(_: argparse.Namespace) -> None:
    with SessionLocal() as session:
        targets = session.scalars(select(SyncTarget).order_by(SyncTarget.priority, SyncTarget.id)).all()
        for target in targets:
            print(
                f"#{target.id} {target.brand_filter} {target.model_filter or '*'} "
                f"enabled={target.enabled} every={target.sync_interval_minutes}m "
                f"max_items={target.max_items}"
            )


def command_add_target(args: argparse.Namespace) -> None:
    with SessionLocal() as session:
        source = session.scalar(select(Source).where(Source.name == "mashina.kg"))
        if source is None:
            raise SystemExit("Run init-db and seed first.")
        target = find_or_create_target(session, source, args)
        target.enabled = True
        target.priority = args.priority
        target.fresh_period = args.period
        target.page_size = args.page_size
        target.max_pages = args.max_pages
        target.max_items = args.max_items
        target.sync_interval_minutes = args.interval
        session.commit()
        print(f"Target #{target.id} saved.")


def command_sync(args: argparse.Namespace) -> None:
    with SessionLocal() as session:
        source = session.scalar(select(Source).where(Source.name == "mashina.kg"))
        if source is None:
            raise SystemExit("Run init-db and seed first.")
        seed_default_catalog(session)
        session.commit()
        target = find_or_create_target(session, source, args)
        parser = MashinaParser()
        try:
            summary = run_sync(
                session,
                parser,
                source,
                target,
                mode=args.mode,
                period_override=args.period,
                page_size_override=args.page_size,
                max_pages_override=args.max_pages,
                max_items_override=args.max_items,
                start_page_override=args.start_page,
            )
        finally:
            parser.close()
        print(summary)


def command_sync_all(args: argparse.Namespace) -> None:
    with SessionLocal() as session:
        source = session.scalar(select(Source).where(Source.name == "mashina.kg"))
        if source is None:
            raise SystemExit("Run init-db and seed first.")
        seed_default_catalog(session)
        session.commit()
        targets = session.scalars(
            select(SyncTarget).where(SyncTarget.enabled.is_(True)).order_by(SyncTarget.priority)
        ).all()
        parser = MashinaParser()
        try:
            for target in targets:
                summary = run_sync(session, parser, source, target, mode=args.mode)
                print(f"{target.brand_filter}: {summary}")
        finally:
            parser.close()


def command_rebuild_stats(_: argparse.Namespace) -> None:
    with SessionLocal() as session:
        count = rebuild_daily_market_stats(session)
        print(f"Daily market statistic rows written: {count}")


def command_catalog_issues(args: argparse.Namespace) -> None:
    with SessionLocal() as session:
        query = (
            select(NormalizationIssue, Brand.name)
            .join(Brand, Brand.id == NormalizationIssue.brand_id)
            .where(NormalizationIssue.status == args.status)
            .order_by(NormalizationIssue.occurrence_count.desc(), NormalizationIssue.id)
        )
        if args.brand:
            query = query.where(Brand.name == args.brand)
        rows = session.execute(query.limit(args.limit)).all()
        if not rows:
            print("No normalization issues found.")
            return
        for issue, brand_name in rows:
            print(
                f"#{issue.id} {brand_name} | {issue.source_model_text!r} | "
                f"count={issue.occurrence_count} status={issue.status}"
            )


def command_add_model_alias(args: argparse.Namespace) -> None:
    with SessionLocal() as session:
        alias = add_model_alias(
            session,
            brand_name=args.brand,
            canonical_model_name=args.model,
            alias=args.alias,
        )
        session.commit()
        print(f"Model alias #{alias.id} saved: {args.brand} {args.alias!r} -> {args.model!r}")


def command_add_generation_alias(args: argparse.Namespace) -> None:
    with SessionLocal() as session:
        alias = add_generation_alias(
            session,
            brand_name=args.brand,
            canonical_model_name=args.model,
            canonical_generation_name=args.generation,
            alias=args.alias,
        )
        session.commit()
        print(
            f"Generation alias #{alias.id} saved: "
            f"{args.brand} {args.model} {args.alias!r} -> {args.generation!r}"
        )


def command_reprocess_catalog(_: argparse.Namespace) -> None:
    with SessionLocal() as session:
        result = reprocess_catalog(session)
        session.commit()
        print(result)


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description="Car Analytics MVP CLI")
    sub = main.add_subparsers(dest="command", required=True)

    for name, func in (("init-db", command_init_db), ("seed", command_seed), ("targets", command_targets), ("rebuild-stats", command_rebuild_stats)):
        child = sub.add_parser(name)
        child.set_defaults(func=func)

    add = sub.add_parser("add-target", help="Create or update a persistent sync target")
    add.add_argument("--brand", required=True)
    add.add_argument("--model")
    add.add_argument("--generation")
    add.add_argument("--period", default="1d")
    add.add_argument("--page-size", type=int, default=100)
    add.add_argument("--max-pages", type=int, default=5)
    add.add_argument("--max-items", type=int, default=500)
    add.add_argument("--interval", type=int, default=60)
    add.add_argument("--priority", type=int, default=100)
    add.set_defaults(func=command_add_target)

    sync = sub.add_parser("sync", help="Run one selected brand/model sync")
    sync.add_argument("--brand", required=True)
    sync.add_argument("--model")
    sync.add_argument("--generation")
    sync.add_argument("--mode", choices=["fresh", "full"], default="fresh")
    sync.add_argument("--period")
    sync.add_argument("--page-size", type=int)
    sync.add_argument("--max-pages", type=int)
    sync.add_argument("--max-items", type=int)
    sync.add_argument("--start-page", type=int, default=1)
    sync.set_defaults(func=command_sync)

    sync_all = sub.add_parser("sync-all", help="Run all enabled persistent targets")
    sync_all.add_argument("--mode", choices=["fresh", "full"], default="fresh")
    sync_all.set_defaults(func=command_sync_all)

    issues = sub.add_parser("catalog-issues", help="List unknown source models waiting for an alias")
    issues.add_argument("--brand")
    issues.add_argument("--status", default="open", choices=["open", "resolved", "ignored"])
    issues.add_argument("--limit", type=int, default=100)
    issues.set_defaults(func=command_catalog_issues)

    model_alias = sub.add_parser("add-model-alias", help="Map a source model spelling to a canonical model")
    model_alias.add_argument("--brand", required=True)
    model_alias.add_argument("--model", required=True, help="Canonical model name")
    model_alias.add_argument("--alias", required=True, help="Source spelling from a title")
    model_alias.set_defaults(func=command_add_model_alias)

    generation_alias = sub.add_parser(
        "add-generation-alias", help="Map a source generation spelling to a canonical generation"
    )
    generation_alias.add_argument("--brand", required=True)
    generation_alias.add_argument("--model", required=True, help="Canonical model name")
    generation_alias.add_argument("--generation", required=True, help="Canonical generation name")
    generation_alias.add_argument("--alias", required=True, help="Source spelling from a title")
    generation_alias.set_defaults(func=command_add_generation_alias)

    reprocess = sub.add_parser(
        "reprocess-catalog", help="Apply current aliases to already saved listings without downloading again"
    )
    reprocess.set_defaults(func=command_reprocess_catalog)
    return main


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
