"""Генератор дневного HTML-отчёта. Запускается внутри контейнера bot (есть app-код
и сетевой доступ к postgres). Использует реальную market_average() как в боте."""
from __future__ import annotations

import html
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Bishkek")

from sqlalchemy import func, select

from app.bot.matching import is_foreign
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import (
    Brand,
    CarModel,
    DailyMarketStat,
    Listing,
    ListingPriceHistory,
    ListingStatusHistory,
    ParserRun,
    SentNotification,
    Source,
)
from app.services.normalizer import MIN_PLAUSIBLE_PRICE_USD
from app.services.stats import mileage_bucket
from app.services.vehicle_summary import _build_source_url as build_source_url

BISHKEK = "Asia/Bishkek"


def not_sent_reason(session, l, settings) -> str:
    """Почему below-market объявление НЕ ушло ботом (эвристика, совпадает с диагностикой)."""
    if l.source_created_at is not None and (
        l.first_seen_at - l.source_created_at
    ) > timedelta(hours=settings.bot_max_listing_age_hours):
        return "старое: опубл. >12 ч до показа (VIP-поднятие)"
    if not l.is_active:
        return "снято/деактивировано позже"
    first_new_hour = session.scalar(
        select(func.extract("hour", func.timezone(BISHKEK, ListingStatusHistory.observed_at)))
        .where(
            ListingStatusHistory.listing_id == l.id,
            ListingStatusHistory.event_type == "new",
        )
        .order_by(ListingStatusHistory.observed_at)
        .limit(1)
    )
    if first_new_hour is not None and first_new_hour < 9:
        return "показано до 09:00 — статистика ещё не пересчитана"
    price_changes = session.scalar(
        select(func.count())
        .select_from(ListingPriceHistory)
        .where(
            ListingPriceHistory.listing_id == l.id,
            ListingPriceHistory.event_type == "price_changed",
        )
    )
    if price_changes and price_changes > 0:
        return "цена упала уже после показа"
    return "окно новизны 60 мин / статистика была не свежей"


def has_valid_price(l) -> bool:
    return l.current_price_usd is not None and float(l.current_price_usd) >= MIN_PLAUSIBLE_PRICE_USD


def market_avg_and_n(session, l, *, min_samples):
    """Как market_average(), но возвращает (avg, n): средняя по когорте и число
    объявлений в базе, из которых она посчитана. avg=None, если образцов < min_samples."""
    if l.brand_id is None or l.car_model_id is None or l.year is None or l.current_price_usd is None:
        return None, 0
    conditions = [
        DailyMarketStat.brand_id == l.brand_id,
        DailyMarketStat.car_model_id == l.car_model_id,
        DailyMarketStat.year == l.year,
    ]
    if l.mileage_km is not None:
        conditions.append(DailyMarketStat.mileage_bucket == mileage_bucket(l.mileage_km))
    if l.customs_status != "unknown":
        conditions.append(DailyMarketStat.customs_status == l.customs_status)
    latest = session.scalar(select(func.max(DailyMarketStat.stat_date)).where(*conditions))
    if latest is None:
        return None, 0
    rows = session.scalars(
        select(DailyMarketStat).where(*conditions, DailyMarketStat.stat_date == latest)
    ).all()
    n = sum(r.sample_count for r in rows)
    if n < min_samples:
        return None, n
    avg = sum(float(r.avg_price_usd) * r.sample_count for r in rows) / n
    return avg, n


def money(v):
    if v is None:
        return "—"
    return "${:,.0f}".format(float(v)).replace(",", " ")


def bishkek_today(session) -> date:
    return session.scalar(select(func.cast(func.timezone(BISHKEK, func.now()), func.DATE().type)))


def main() -> None:
    settings = get_settings()
    factor = settings.bot_below_avg_factor
    min_samples = settings.bot_below_avg_min_samples

    with SessionLocal() as session:
        today = session.scalar(
            select(func.date(func.timezone(BISHKEK, func.now())))
        )
        gen_ts = session.scalar(select(func.to_char(func.timezone(BISHKEK, func.now()), 'DD.MM.YYYY HH24:MI')))

        # --- Парс-раны за сегодня ---
        run_rows = session.execute(
            select(
                Source.name,
                ParserRun.mode,
                func.count().label("runs"),
                func.min(func.timezone(BISHKEK, ParserRun.started_at)).label("first_start"),
                func.max(func.timezone(BISHKEK, func.coalesce(ParserRun.finished_at, ParserRun.started_at))).label("last_end"),
                func.sum(ParserRun.new_count).label("new"),
                func.sum(ParserRun.processed_count).label("processed"),
                func.sum(ParserRun.error_count).label("err"),
            )
            .join(Source, Source.id == ParserRun.source_id)
            .where(func.date(func.timezone(BISHKEK, ParserRun.started_at)) == today)
            .group_by(Source.name, ParserRun.mode)
            .order_by(Source.name, ParserRun.mode)
        ).all()

        # --- Объявления, впервые увиденные сегодня ---
        listings = session.scalars(
            select(Listing).where(
                func.date(func.timezone(BISHKEK, Listing.first_seen_at)) == today
            )
        ).all()

        brand_name = {b.id: b.name for b in session.scalars(select(Brand))}
        model_name = {m.id: m.name for m in session.scalars(select(CarModel))}
        src_name = {s.id: s.name for s in session.scalars(select(Source))}

        total = len(listings)
        foreign = [l for l in listings if is_foreign(l.city)]
        local = [l for l in listings if not is_foreign(l.city)]
        local_priced = [l for l in local if has_valid_price(l)]  # порог MIN_PLAUSIBLE_PRICE_USD, как у бота
        junk_or_noprice = len(local) - len(local_priced)

        # Средняя/медиана по местным с ценой
        avg_all = (sum(float(l.current_price_usd) for l in local_priced) / len(local_priced)) if local_priced else None
        prices_sorted = sorted(float(l.current_price_usd) for l in local_priced)
        median_all = prices_sorted[len(prices_sorted) // 2] if prices_sorted else None

        # --- Средняя по рынку + число похожих в базе для КАЖДОГО местного авто ---
        # avg_n_map[listing_id] = (avg|None, n). Считается один раз, переиспользуется
        # и в «ниже рынка», и в общем списке (рядом со средней).
        avg_n_map: dict[int, tuple[float | None, int]] = {}
        for l in local_priced:
            avg_n_map[l.id] = market_avg_and_n(session, l, min_samples=min_samples)

        # --- Ниже рынка (местные) — как считает бот ---
        below = []  # (listing, avg, pct, n)
        for l in local_priced:
            avg, n = avg_n_map[l.id]
            if avg is None or avg <= 0:
                continue
            pct = round((1 - float(l.current_price_usd) / avg) * 100, 1)
            # порог бота по умолчанию: цена <= avg*factor  (factor=0.95 → ≥5% ниже)
            if float(l.current_price_usd) <= avg * factor:
                below.append((l, avg, pct, n))
        below.sort(key=lambda t: t[2], reverse=True)

        # --- Что реально отправил бот сегодня (sent_notifications) ---
        sent_rows = session.execute(
            select(
                SentNotification.listing_id,
                func.min(func.timezone(BISHKEK, SentNotification.sent_at)),
                func.count(func.distinct(SentNotification.user_id)),
            )
            .where(func.date(func.timezone(BISHKEK, SentNotification.sent_at)) == today)
            .group_by(SentNotification.listing_id)
        ).all()
        sent_map = {lid: (ts, users) for lid, ts, users in sent_rows}
        sent_rows_total = session.scalar(
            select(func.count()).select_from(SentNotification).where(
                func.date(func.timezone(BISHKEK, SentNotification.sent_at)) == today
            )
        )
        # Отправленные объявления (объекты) — все они first_seen сегодня.
        sent_listings = sorted(
            (l for l in local if l.id in sent_map), key=lambda x: sent_map[x.id][0]
        )
        # Причина «не отправлено» для каждого below-market, которого нет в отправленных.
        reason_map = {
            l.id: not_sent_reason(session, l, settings)
            for l, _avg, _pct, _n in below
            if l.id not in sent_map
        }

        # Средние по маркам (местные с ценой)
        by_brand = defaultdict(list)
        for l in local_priced:
            by_brand[brand_name.get(l.brand_id, "— (не распознан)")].append(float(l.current_price_usd))
        brand_stats = []
        for bn, ps in by_brand.items():
            ps2 = sorted(ps)
            brand_stats.append((bn, len(ps2), sum(ps2) / len(ps2), ps2[len(ps2) // 2]))
        brand_stats.sort(key=lambda t: t[1], reverse=True)

        # Группировка местных для списка (по марке → строки)
        local_by_brand = defaultdict(list)
        for l in local:
            local_by_brand[brand_name.get(l.brand_id, "— (марка не распознана)")].append(l)

    print(render(
        today=today, gen_ts=gen_ts, run_rows=run_rows,
        total=total, local=local, foreign=foreign, local_priced=local_priced,
        junk_or_noprice=junk_or_noprice,
        avg_all=avg_all, median_all=median_all, below=below, factor=factor,
        min_samples=min_samples, brand_stats=brand_stats, local_by_brand=local_by_brand,
        brand_name=brand_name, model_name=model_name, src_name=src_name,
        sent_map=sent_map, sent_rows_total=sent_rows_total, sent_listings=sent_listings,
        avg_n_map=avg_n_map, reason_map=reason_map,
    ))


def sent_badge(l, sent_map, reason_map=None):
    """Метка: отправлено ботом (и во сколько / скольким) или нет + причина."""
    info = sent_map.get(l.id)
    if info:
        ts, users = info
        who = f" · {users} польз." if users and users > 1 else ""
        return f'<span class="sent yes">📨 отправлено {ts.strftime("%H:%M")}{who}</span>'
    reason = (reason_map or {}).get(l.id)
    tail = f' · {html.escape(reason)}' if reason else ""
    return f'<span class="sent no">не отправлялось{tail}</span>'


def pub_date(l):
    if l.source_created_at is None:
        return "дата публ. —"
    return "опубл. " + l.source_created_at.astimezone(_TZ).strftime("%d.%m")


def car_line(l, model_name, src_name):
    m = model_name.get(l.car_model_id) or (l.source_model_text or "—")
    yr = l.year or "—"
    mil = "{:,} км".format(l.mileage_km).replace(",", " ") if l.mileage_km else "пробег —"
    city = html.escape(l.city or "—")
    src = src_name.get(l.source_id, "")
    price = money(l.current_price_usd) if has_valid_price(l) else "цена н/д"
    url = build_source_url(l)
    return m, yr, mil, city, src, price, url


def render(**d):
    today = d["today"]; gen_ts = d["gen_ts"]
    parts = []
    P = parts.append

    P(HEAD.format(date=today.strftime("%d.%m.%Y")))
    P('<div class="wrap">')

    # header
    P(f'''<header>
      <button class="toggle" id="themeBtn" aria-label="Сменить тему" title="Светлая / тёмная тема">◐</button>
      <div class="eyebrow">Skupka · дневная сводка · местный рынок</div>
      <h1>Что собрали за {today.strftime("%d.%m.%Y")}</h1>
      <div class="sub">Новые объявления, впервые увиденные за сегодня по Бишкеку. Зарубежные («на заказ») в списки не входят — только местный рынок. Сформировано <b>{gen_ts}</b> на прод-копии базы.</div>
    </header>''')

    # --- Сбор данных ---
    P('<section><div class="sec-head"><h2>Сбор данных сегодня</h2><span class="line"></span></div>')
    P('<div class="card acc"><table class="tbl"><thead><tr><th>Источник</th><th>Циклов</th><th>Окно (Бишкек)</th><th>Новых</th><th>Обработано</th><th>Ошибок</th></tr></thead><tbody>')
    for r in d["run_rows"]:
        win = f'{r.first_start.strftime("%H:%M")} – {r.last_end.strftime("%H:%M")}'
        P(f'<tr><td><b>{html.escape(r.name)}</b> <span class="c">{r.mode}</span></td><td>{r.runs}</td><td>{win}</td><td class="pos">{r.new}</td><td>{r.processed}</td><td>{r.err}</td></tr>')
    P('</tbody></table>')
    P('<div class="note">Fresh-циклы бот запускает каждые ~10 минут в окне 08:00–24:00. Каждый цикл обходит все марки постранично; «обработано» — сколько карточек просмотрено, «новых» — сколько раньше не встречалось.</div>')
    P('</div></section>')

    # --- Итоги ---
    below = d["below"]
    P('<section><div class="sec-head"><h2>Итоги за день</h2><span class="line"></span></div>')
    P('<div class="stats">')
    for label, val, sub in [
        ("Всего получено", d["total"], "новых карточек"),
        ("Местный рынок", len(d["local"]), "без зарубежа"),
        ("Зарубежные", len(d["foreign"]), "«на заказ»"),
        ("С ценой ≥$500", len(d["local_priced"]), "валидная цена"),
        ("Ниже рынка", len(below), f"≥{round((1-d['factor'])*100)}% ниже (логика)"),
        ("📨 Отправлено ботом", len(d["sent_map"]), f'{d["sent_rows_total"]} пушей · вкл. зарубеж'),
        ("Средняя цена", money(d["avg_all"]), "местные"),
        ("Медиана", money(d["median_all"]), "местные"),
    ]:
        P(f'<div class="tile"><div class="tlab">{label}</div><div class="tval">{val}</div><div class="tsub">{sub}</div></div>')
    P('</div>')
    P(f'<div class="note">Порог достоверной цены — <b>${MIN_PLAUSIBLE_PRICE_USD}</b> (как у бота): <b>{d["junk_or_noprice"]}</b> местных объявлений с ценой ниже порога или без цены исключены из средних и списка «ниже рынка» (в общем списке показаны как «цена н/д»).</div>')
    P('</section>')

    # --- Средние по маркам ---
    P('<section><div class="sec-head"><h2>Средние цены по маркам (местный рынок)</h2><span class="line"></span></div>')
    P('<div class="card"><table class="tbl"><thead><tr><th>Марка</th><th>Кол-во</th><th>Средняя</th><th>Медиана</th></tr></thead><tbody>')
    for bn, n, avg, med in d["brand_stats"]:
        P(f'<tr><td><b>{html.escape(bn)}</b></td><td>{n}</td><td>{money(avg)}</td><td>{money(med)}</td></tr>')
    P('</tbody></table><div class="note">Средняя и медиана — по объявлениям с указанной ценой в USD, впервые увиденным сегодня.</div></div></section>')

    # --- Ниже рынка ---
    sent_map = d["sent_map"]; reason_map = d["reason_map"]
    P('<section><div class="sec-head"><h2>⚑ Ниже рынка (местный рынок)</h2><span class="line"></span></div>')
    P(f'<p class="lead">Считается точно как в боте: цена ниже средней по рынку (daily_market_stats) для когорты «марка+модель+год» минимум на <b>{round((1-d["factor"])*100)}%</b>, при наличии ≥<b>{d["min_samples"]}</b> образцов. Сортировка — от самых выгодных. «Похожих в базе» — сколько объявлений этой когорты формируют среднюю. Метка показывает, ушло ли уведомление в бота (бот шлёт только опубликованные ≤12 ч назад и совпавшие с фильтром).</p>')
    if below:
        P('<div class="disp-list">')
        for l, avg, pct, n in below:
            m, yr, mil, city, src, price, url = car_line(l, d["model_name"], d["src_name"])
            bn = d["brand_name"].get(l.brand_id, "—")
            P(f'''<div class="drow">
              <div class="dname"><a href="{html.escape(url)}">{html.escape(bn)} {html.escape(str(m))} {yr}</a> {sent_badge(l, sent_map, reason_map)}</div>
              <div class="dwhy"><span class="opt">−{pct}%</span> цена <b>{price}</b> · рынок ≈ <b>{money(avg)}</b> · <b>{n}</b> похожих в базе <span class="c">· {mil} · {city} · {html.escape(src)} · {pub_date(l)}</span></div>
            </div>''')
        P('</div>')
    else:
        P('<div class="card"><p class="lead" style="margin:0">Сегодня объявлений ниже рынка (по порогу бота) не найдено.</p></div>')
    P('</section>')

    # --- Полный список местных ---
    avg_n_map = d["avg_n_map"]
    P('<section><div class="sec-head"><h2>Все местные авто за сегодня</h2><span class="line"></span></div>')
    P(f'<p class="lead"><b>{len(d["local"])}</b> объявлений, сгруппированы по марке, внутри — по возрастанию цены. Слева ссылка на объявление, справа — <b>рыночная средняя</b> для его когорты (── если образцов мало). Зарубежные исключены.</p>')
    for bn in sorted(d["local_by_brand"], key=lambda k: -len(d["local_by_brand"][k])):
        items = d["local_by_brand"][bn]
        P(f'<div class="obl"><h3>{html.escape(bn)} <span class="tot">{len(items)}</span></h3><div class="body"><div class="carrows">')
        for l in sorted(items, key=lambda x: (not has_valid_price(x), float(x.current_price_usd) if has_valid_price(x) else 0)):
            m, yr, mil, city, src, price, url = car_line(l, d["model_name"], d["src_name"])
            avg = avg_n_map.get(l.id, (None, 0))[0]
            avg_txt = f'рынок {money(avg)}' if avg else '<span class="faint">рынок ──</span>'
            P(f'<a class="car" href="{html.escape(url)}"><span class="cm">{html.escape(str(m))} <span class="cyr">{yr}</span></span><span class="cprice">{price}</span><span class="cavg">{avg_txt}</span></a>')
        P('</div></div></div>')
    P('</section>')

    P(f'''<div class="foot">Skupka · дневная сводка «местный рынок» · только новые за {today.strftime("%d.%m.%Y")}<br>Сформировано <b>{gen_ts}</b> на прод-копии базы</div>''')
    P('</div>')
    P(SCRIPT)
    return "".join(parts)


HEAD = '''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Skupka — сводка за {date}</title>
<style>
  :root{{
    --bg:#f5f7f8; --surface:#ffffff; --surface-2:#f0f3f4;
    --text:#12171c; --muted:#5c6772; --faint:#8b96a1;
    --border:#e2e7ea; --border-strong:#d2d9dd;
    --accent:#0f7a6e; --accent-soft:#0f7a6e1a; --accent-ink:#0b5c53;
    --warn:#b4690e; --warn-soft:#b4690e18; --pos:#0f7a6e;
    --shadow:0 1px 2px rgba(16,32,40,.04), 0 6px 20px rgba(16,32,40,.05);
    --radius:14px;
    --mono:"SF Mono",ui-monospace,"JetBrains Mono","Roboto Mono",Menlo,Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",system-ui,Roboto,"Helvetica Neue",sans-serif;
  }}
  @media (prefers-color-scheme:dark){{
    :root{{
      --bg:#0d1114; --surface:#151b1f; --surface-2:#1b2328;
      --text:#e8edef; --muted:#9aa6ad; --faint:#6c7981;
      --border:#242e34; --border-strong:#2f3b42;
      --accent:#2bb6a6; --accent-soft:#2bb6a61f; --accent-ink:#7fded2;
      --warn:#e6a94b; --warn-soft:#e6a94b1f; --pos:#2bb6a6;
      --shadow:0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.35);
    }}
  }}
  :root[data-theme="light"]{{
    --bg:#f5f7f8; --surface:#ffffff; --surface-2:#f0f3f4;
    --text:#12171c; --muted:#5c6772; --faint:#8b96a1;
    --border:#e2e7ea; --border-strong:#d2d9dd;
    --accent:#0f7a6e; --accent-soft:#0f7a6e1a; --accent-ink:#0b5c53;
    --warn:#b4690e; --warn-soft:#b4690e18; --pos:#0f7a6e;
    --shadow:0 1px 2px rgba(16,32,40,.04),0 6px 20px rgba(16,32,40,.05);
  }}
  :root[data-theme="dark"]{{
    --bg:#0d1114; --surface:#151b1f; --surface-2:#1b2328;
    --text:#e8edef; --muted:#9aa6ad; --faint:#6c7981;
    --border:#242e34; --border-strong:#2f3b42;
    --accent:#2bb6a6; --accent-soft:#2bb6a61f; --accent-ink:#7fded2;
    --warn:#e6a94b; --warn-soft:#e6a94b1f; --pos:#2bb6a6;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.35);
  }}
  *{{box-sizing:border-box}}
  html{{-webkit-text-size-adjust:100%}}
  body{{margin:0; background:var(--bg); color:var(--text); font-family:var(--sans); line-height:1.5; font-size:16px; -webkit-font-smoothing:antialiased;
    padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);}}
  .wrap{{max-width:720px; margin:0 auto; padding:20px 16px 56px}}
  header{{position:relative; margin-bottom:22px}}
  .eyebrow{{font-size:12px; letter-spacing:.14em; text-transform:uppercase; color:var(--accent); font-weight:700;}}
  h1{{font-size:26px; line-height:1.15; margin:.28em 0 .2em; font-weight:800; letter-spacing:-.02em; text-wrap:balance;}}
  .sub{{color:var(--muted); font-size:14px}} .sub b{{color:var(--text); font-weight:600}}
  .toggle{{position:absolute; top:0; right:0; width:38px; height:38px; border-radius:11px; border:1px solid var(--border); background:var(--surface); color:var(--muted); font-size:17px; cursor:pointer; display:grid; place-items:center; box-shadow:var(--shadow);}}
  .toggle:focus-visible{{outline:2px solid var(--accent); outline-offset:2px}}
  section{{margin-top:26px}}
  .sec-head{{display:flex; align-items:baseline; gap:8px; margin-bottom:12px}}
  .sec-head h2{{font-size:13px; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); font-weight:700; margin:0}}
  .sec-head .line{{flex:1; height:1px; background:var(--border)}}
  .card{{background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); box-shadow:var(--shadow); padding:16px}}
  .card.acc{{border-left:3px solid var(--accent)}}
  .lead{{font-size:14px; color:var(--muted); margin:0 0 10px}} .lead b{{color:var(--text); font-weight:600}}
  .note{{margin-top:10px; font-size:12.5px; color:var(--muted)}} .note b{{color:var(--text)}}
  a{{color:var(--accent); text-decoration:none}}
  .c{{font-family:var(--mono); font-size:11.5px; color:var(--faint); font-weight:400}}
  /* tables */
  .tbl{{width:100%; border-collapse:collapse; font-size:13.5px}}
  .tbl th{{text-align:left; font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--faint); font-weight:700; padding:0 8px 8px; border-bottom:1px solid var(--border)}}
  .tbl td{{padding:8px; border-bottom:1px solid var(--border)}}
  .tbl tr:last-child td{{border-bottom:none}}
  .tbl .pos{{color:var(--pos); font-weight:700}}
  /* stat tiles */
  .stats{{display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px}}
  .tile{{background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); box-shadow:var(--shadow); padding:14px}}
  .tlab{{font-size:11.5px; letter-spacing:.06em; text-transform:uppercase; color:var(--faint); font-weight:700}}
  .tval{{font-size:26px; font-weight:800; letter-spacing:-.02em; margin:4px 0 2px}}
  .tsub{{font-size:12px; color:var(--muted)}}
  /* oblast-like brand blocks */
  .obl{{background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); box-shadow:var(--shadow); overflow:hidden; margin-bottom:12px}}
  .obl h3{{margin:0; padding:12px 14px; font-size:14.5px; font-weight:800; display:flex; align-items:baseline; justify-content:space-between; gap:10px; background:var(--surface-2); border-bottom:1px solid var(--border)}}
  .obl h3 .tot{{font-family:var(--mono); font-size:12px; font-weight:600; color:var(--accent)}}
  .obl .body{{padding:6px 8px}}
  .carrows{{display:flex; flex-direction:column}}
  .car{{display:flex; align-items:baseline; gap:10px; padding:8px; border-radius:8px}}
  .car:hover{{background:var(--surface-2)}}
  .car .cm{{font-weight:600; color:var(--text); flex:1; min-width:0}}
  .car:hover .cm{{text-decoration:underline; text-decoration-color:var(--accent); text-underline-offset:2px}}
  .car .cyr{{font-family:var(--mono); font-size:12px; font-weight:400; color:var(--faint)}}
  .car .cprice{{font-family:var(--mono); font-size:13px; font-weight:700; color:var(--accent); flex:0 0 auto}}
  .car .cavg{{font-family:var(--mono); font-size:12px; color:var(--muted); flex:0 0 auto; min-width:96px; text-align:right}}
  .faint{{color:var(--faint)}}
  /* sent badge */
  .sent{{font-size:11px; font-weight:600; border-radius:6px; padding:1px 8px; white-space:nowrap; flex:0 1 auto}}
  .sent.yes{{background:var(--accent-soft); color:var(--accent-ink)}}
  .sent.no{{background:var(--surface-2); color:var(--warn); white-space:normal}}
  /* below-market list */
  .disp-list{{background:var(--surface); border:1px solid var(--border); border-left:3px solid var(--warn); border-radius:var(--radius); box-shadow:var(--shadow); overflow:hidden}}
  .drow{{padding:11px 14px; border-top:1px solid var(--border)}} .drow:first-child{{border-top:none}}
  .dname{{font-size:14px; font-weight:700; display:flex; align-items:baseline; justify-content:space-between; gap:8px; flex-wrap:wrap; row-gap:5px}}
  .dname a:hover{{text-decoration:underline}} .dname .c{{margin-left:6px; font-weight:400}}
  .dwhy{{font-size:12.8px; color:var(--muted); margin-top:5px}} .dwhy b{{color:var(--text)}}
  .opt{{display:inline-block; font-size:12px; background:var(--warn-soft); color:var(--warn); border-radius:6px; padding:1px 7px; margin-right:6px; font-weight:700}}
  .foot{{margin-top:34px; padding-top:16px; border-top:1px solid var(--border); color:var(--faint); font-size:12px; text-align:center}} .foot b{{color:var(--muted); font-weight:600}}
  @media (max-width:420px){{ h1{{font-size:22px}} .car{{flex-wrap:wrap; gap:4px}} .car .cm{{min-width:0}} }}
</style>
</head>
<body>'''

SCRIPT = '''<script>
(function(){var root=document.documentElement, btn=document.getElementById('themeBtn');
btn.addEventListener('click', function(){var cur=root.getAttribute('data-theme');
if(!cur){cur = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark':'light';}
root.setAttribute('data-theme', cur==='dark'?'light':'dark');});})();
</script>
</body>
</html>'''


if __name__ == "__main__":
    main()
