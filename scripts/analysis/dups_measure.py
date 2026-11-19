"""Замер доли дублей на копии прод-базы (план 18.11 № 8 / DU-0). Только чтение."""
import json
import sys
from collections import Counter, defaultdict
import psycopg

# Временный Postgres с дампом: docker run -d --name dupdb -e POSTGRES_PASSWORD=dup -e POSTGRES_DB=dup -p 127.0.0.1:5442:5432 postgres:16-alpine
DSN = "postgresql://postgres:dup@127.0.0.1:5442/dup"
OUT = sys.argv[1]

PAIRS_SQL = r"""
with a as (
  select l.id, s.name src, l.brand_id, l.car_model_id, l.year, l.fuel_group, l.mileage_km km,
         l.current_price_usd::float price, l.first_seen_at fs, l.seller_phone_hash ph,
         regexp_replace(regexp_replace(coalesce(l.image_url,''), '^.*/', ''), '(_medium|_large|_small|_thumb)', '') img,
         case when length(coalesce(l.description,'')) >= 40
              then md5(regexp_replace(lower(l.description), '\s+', ' ', 'g')) end dsc
  from listings l join sources s on s.id = l.source_id
  where l.is_active and l.brand_id is not null and l.car_model_id is not null and l.year is not null
    and l.current_price_usd is not null and l.current_price_usd >= 500
    and coalesce(l.price_suspect, false) = false
)
select x.id, y.id, x.src, y.src, x.km, y.km, x.price, y.price,
       (x.ph is not null and x.ph = y.ph) phone,
       (x.img <> '' and x.img = y.img) photo,
       (x.dsc is not null and x.dsc = y.dsc) descr,
       abs(extract(epoch from x.fs - y.fs))/3600 hours_apart
from a x join a y
  on x.brand_id = y.brand_id and x.car_model_id = y.car_model_id and x.year = y.year and x.id < y.id
 and (x.fuel_group is null or y.fuel_group is null or x.fuel_group = y.fuel_group)
 and abs(x.price - y.price) <= 0.07 * greatest(x.price, y.price)
 and abs(extract(epoch from x.fs - y.fs)) <= 90*86400
 and (
      (x.km is not null and y.km is not null and abs(x.km - y.km) <= greatest(1000, 0.015 * greatest(x.km, y.km)))
   or (x.ph is not null and x.ph = y.ph)
   or (x.img <> '' and x.img = y.img)
   or (x.dsc is not null and x.dsc = y.dsc)
 )
"""

with psycopg.connect(DSN) as con:
    active = dict(con.execute("select s.name, count(*) from listings l join sources s on s.id=l.source_id where is_active group by 1").fetchall())
    total_active = sum(active.values())
    exact = con.execute("""select coalesce(sum(n-1),0) from (select count(*) n from listings where is_active and mileage_km is not null
                           group by brand_id, car_model_id, year, mileage_km, round(current_price_usd)) t where n>1""").fetchone()[0]
    rows = con.execute(PAIRS_SQL).fetchall()

pairs = []
for a, b, sa, sb, ka, kb, pa, pb, phone, photo, descr, hours in rows:
    km_close = ka is not None and kb is not None and abs(ka - kb) <= max(1000, 0.015 * max(ka, kb))
    strong = [n for n, f in (("телефон", phone), ("фото", photo), ("описание", descr)) if f]
    if km_close and strong:
        level = "вероятно"
    elif km_close:
        level = "возможно"
    elif len(strong) >= 2:
        level = "вероятно"   # пробега нет, но два сильных признака
    else:
        level = None         # один сильный признак без пробега — не метим (дилер с одним телефоном)
    kind = "-".join(sorted([sa.split(".")[0], sb.split(".")[0]]))
    pairs.append(dict(a=a, b=b, kind=kind, km_close=km_close, strong=strong, level=level,
                      km=(ka, kb), price=(pa, pb), hours=round(float(hours), 1)))

def groups(selected):
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for p in selected:
        ra, rb = find(p["a"]), find(p["b"])
        if ra != rb:
            parent[ra] = rb
    members = defaultdict(set)
    for x in parent:
        members[find(x)].add(x)
    return list(members.values())

report = {"active": active, "total_active": total_active, "exact_dup_key_extra": int(exact)}
for lvl in ("вероятно", "возможно"):
    sel = [p for p in pairs if p["level"] == lvl]
    report[lvl] = {"pairs": len(sel), "by_kind": Counter(p["kind"] for p in sel),
                   "by_signal": Counter(s for p in sel for s in p["strong"]),
                   "listings": len({x for p in sel for x in (p["a"], p["b"])})}
marked = [p for p in pairs if p["level"]]
g = groups(marked)
extra = sum(len(m) - 1 for m in g)
g_prob = groups([p for p in pairs if p["level"] == "вероятно"])
report["groups_any"] = {"groups": len(g), "listings_in_groups": sum(len(m) for m in g), "extra_listings": extra,
                        "share_of_active_pct": round(100 * extra / total_active, 1),
                        "max_group": max((len(m) for m in g), default=0),
                        "size_hist": Counter(min(len(m), 6) for m in g)}
report["groups_probable"] = {"groups": len(g_prob), "extra_listings": sum(len(m) - 1 for m in g_prob),
                             "share_of_active_pct": round(100 * sum(len(m) - 1 for m in g_prob) / total_active, 1)}
json.dump({"report": report, "pairs": pairs}, open(OUT, "w"), ensure_ascii=False, default=str, indent=1)
print(json.dumps(report, ensure_ascii=False, default=str, indent=1))
