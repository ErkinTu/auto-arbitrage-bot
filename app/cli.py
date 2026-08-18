from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal, init_db
from app.models import Brand, NormalizationIssue, Source, SyncTarget
from app.services.catalog_service import (
    add_generation_alias,
    add_model_alias,
    reprocess_catalog,
    seed_default_catalog,
)
from app.services.lookup import get_or_create_source
from app.services.sources import SOURCE_NAMES, build_parser, source_base_url
from app.services.stats import rebuild_daily_market_stats
from app.services.sync import run_sync


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_seed_data() -> None:
    settings = get_settings()
    with SessionLocal() as session:
        seed_default_catalog(session)
        for source_name in SOURCE_NAMES:
            source = get_or_create_source(session, source_name, source_base_url(source_name))
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
    print(f"Seeded targets for configured brands: {', '.join(SOURCE_NAMES)}.")


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
        source = session.scalar(select(Source).where(Source.name == args.source))
        if source is None:
            raise SystemExit("Run init-db and seed first.")
        seed_default_catalog(session)
        session.commit()
        target = find_or_create_target(session, source, args)
        parser = build_parser(args.source, session)
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
        source = session.scalar(select(Source).where(Source.name == args.source))
        if source is None:
            raise SystemExit("Run init-db and seed first.")
        seed_default_catalog(session)
        session.commit()
        targets = session.scalars(
            select(SyncTarget)
            .where(SyncTarget.enabled.is_(True), SyncTarget.source_id == source.id)
            .order_by(SyncTarget.priority)
        ).all()
        parser = build_parser(args.source, session)
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


def command_ensure_self_aliases(_: argparse.Namespace) -> None:
    """Каждой канонической модели — алиас на её собственное имя.

    mashina присылает модели «грязными», поэтому исторически алиасы есть только
    на грязные варианты. Источники с чистыми именами (lalafo) без самоалиасов
    не резолвятся. Команда идемпотентна."""
    from app.models import Brand, CarModel, ModelAlias
    from app.services.catalog import normalize_alias_text
    from app.services.catalog_service import _upsert_model_alias

    with SessionLocal() as session:
        created = 0
        conflicts = 0
        pairs = session.execute(
            select(CarModel, Brand).join(Brand, Brand.id == CarModel.brand_id)
        ).all()
        for car_model, brand in pairs:
            existing = session.scalar(
                select(ModelAlias).where(
                    ModelAlias.brand_id == brand.id,
                    ModelAlias.normalized_alias == normalize_alias_text(car_model.name),
                )
            )
            if existing is not None:
                if existing.car_model_id != car_model.id:
                    other = session.get(CarModel, existing.car_model_id)
                    print(
                        f"CONFLICT: {brand.name} {car_model.name!r} — алиас уже указывает на "
                        f"{other.name if other else existing.car_model_id!r}, пропущено"
                    )
                    conflicts += 1
                continue
            _upsert_model_alias(
                session, brand=brand, car_model=car_model, alias=car_model.name, source="canonical"
            )
            created += 1
        session.commit()
        print(f"Models: {len(pairs)}, self-aliases created: {created}, conflicts: {conflicts}.")


def command_refresh_rate(_: argparse.Namespace) -> None:
    from app.services.exchange import refresh_usd_rate

    with SessionLocal() as session:
        row = refresh_usd_rate(session)
        session.commit()
        print(f"USD rate for {row.rate_date}: {row.rate_kgs} KGS (nbkr.kg)")


def command_reprocess_catalog(_: argparse.Namespace) -> None:
    with SessionLocal() as session:
        result = reprocess_catalog(session)
        session.commit()
        print(result)


def command_grant_subscription(args: argparse.Namespace) -> None:
    """Продлевает подписку вручную.

    Зачем: у Finik нет эндпоинта «проверь статус платежа», а webhook приходит
    только на успех. Если webhook не доехал совсем (лежал домен/сертификат), а
    платёж виден в кабинете Finik — продлеваем этой командой. Годится и для
    бонусов (рефералка, компенсация).
    """
    from sqlalchemy import select

    from app.models import BotUser
    from app.services.plans import ORIGIN_MANUAL
    from app.services.subscription import extend_paid_access

    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == args.telegram_id))
        if user is None:
            print(f"Bot user with telegram_id={args.telegram_id} not found.")
            return
        # По умолчанию `full`: ручное продление — это компенсация или потерянный
        # webhook, то есть извинение, и выдавать за него медленный тариф странно.
        # Настоящий план оплаченного платежа сверяется по `payments.plan_code`.
        extend_paid_access(
            session,
            user,
            args.days,
            tier=args.tier,
            origin=ORIGIN_MANUAL,
            now=utcnow(),
        )
        session.commit()
        note = f" ({args.note})" if args.note else ""
        print(
            f"telegram_id={args.telegram_id}: +{args.days} days ({args.tier}), "
            f"subscription until {user.subscription_expires_at:%Y-%m-%d %H:%M} UTC{note}"
        )


def command_funnel(args: argparse.Namespace) -> None:
    """Воронка: старты → триалы → пейвол → платежи → оплаты.

    Считается по `user_events` (журнал переходов), а не по `bot_users`: там
    только текущее состояние, и «отвал по дням» по нему не построить. Даты — в
    UTC, как хранятся; расхождение с бишкекским днём (+6) для суточных
    когорт несущественно, а зависимость от таймзоны запуска — вредна.
    """
    from sqlalchemy import func

    from app.models import BotUser, UserEvent
    from app.services.events import (
        EVENT_PAID,
        EVENT_START,
        EVENT_TRIAL_EXPIRED,
        EVENT_TRIAL_STARTED,
        FUNNEL_EVENTS,
    )
    from app.services.funnel import funnel_steps
    from app.services.subscription import is_subscription_active
    from app.services.trial import is_trial_active

    since = utcnow() - timedelta(days=args.days)

    with SessionLocal() as session:
        # Считает services/funnel.py — тем же кодом, что и админ-панель.
        steps = funnel_steps(session, days=args.days)

        print(f"Воронка за последние {args.days} дн. (с {since:%Y-%m-%d %H:%M} UTC)")
        print("-" * 52)
        for step in steps:
            of_start = (
                f"{step['of_start_percent']:5.1f}% от стартов"
                if step["of_start_percent"] is not None
                else "     —"
            )
            of_prev = (
                f", {step['of_previous_percent']:5.1f}% от пред."
                if step["of_previous_percent"] is not None
                else ""
            )
            print(f"{step['title']:<22} {step['users']:>6}   {of_start}{of_prev}")

        by_day = session.execute(
            select(
                func.date(UserEvent.created_at).label("day"),
                UserEvent.event,
                func.count(func.distinct(UserEvent.user_id)),
            )
            .where(UserEvent.event.in_(FUNNEL_EVENTS), UserEvent.created_at >= since)
            .group_by("day", UserEvent.event)
        ).all()
        if by_day:
            per_day: dict[str, dict[str, int]] = {}
            for day, event, value in by_day:
                per_day.setdefault(str(day), {})[event] = value
            print("\nПо дням (старты / триалы / пейвол / оплаты)")
            print("-" * 52)
            for day in sorted(per_day):
                row = per_day[day]
                print(
                    f"{day}  {row.get(EVENT_START, 0):>4} / {row.get(EVENT_TRIAL_STARTED, 0):>4}"
                    f" / {row.get(EVENT_TRIAL_EXPIRED, 0):>4} / {row.get(EVENT_PAID, 0):>4}"
                )

        # Срез «прямо сейчас» — по состоянию, а не по журналу.
        users = session.scalars(select(BotUser)).all()
        subscribers = sum(1 for u in users if is_subscription_active(u))
        trials = sum(1 for u in users if is_trial_active(u))
        members = sum(1 for u in users if u.access_status == "member")
        print("\nСейчас")
        print("-" * 52)
        print(f"Всего пользователей   {len(users):>6}")
        print(f"Члены группы          {members:>6}")
        print(f"Активная подписка     {subscribers:>6}")
        print(f"Активный триал        {trials:>6}")


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
    sync.add_argument("--source", choices=list(SOURCE_NAMES), default="mashina.kg")
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

    sync_all = sub.add_parser("sync-all", help="Run all enabled persistent targets of one source")
    sync_all.add_argument("--source", choices=list(SOURCE_NAMES), default="mashina.kg")
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

    self_aliases = sub.add_parser(
        "ensure-self-aliases",
        help="Create an alias equal to each canonical model name (clean-name sources need them)",
    )
    self_aliases.set_defaults(func=command_ensure_self_aliases)

    refresh_rate = sub.add_parser("refresh-rate", help="Fetch today's official USD/KGS rate from nbkr.kg")
    refresh_rate.set_defaults(func=command_refresh_rate)

    grant = sub.add_parser(
        "grant-subscription",
        help="Extend a user's subscription manually (lost webhook, referral bonus, goodwill)",
    )
    grant.add_argument("--telegram-id", type=int, required=True)
    grant.add_argument("--days", type=int, required=True)
    grant.add_argument(
        "--tier",
        choices=("full", "delayed"),
        default="full",
        help="Notification speed for the granted days (default: full)",
    )
    grant.add_argument("--note", default="", help="Free-text reason, printed to the log only")
    grant.set_defaults(func=command_grant_subscription)

    funnel = sub.add_parser("funnel", help="Bot funnel: starts -> trials -> payments -> paid")
    funnel.add_argument("--days", type=int, default=30)
    funnel.set_defaults(func=command_funnel)
    return main


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
