"""Проверка правил на выборке пар: совпадение фото по dHash (прокси «та же машина»).

Запуск: uv run --with pillow python scripts/analysis/dups_validate.py pairs.json sample.json
(pairs.json — выход dups_measure.py)."""
import io
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, defaultdict
import httpx
import psycopg
from PIL import Image

random.seed(18)
DSN = "postgresql://postgres:dup@127.0.0.1:5442/dup"
d = json.load(open(sys.argv[1]))
KNOWN = [(345153, 344978), (342318, 350817), (326654, 326537), (326654, 324202), (326537, 324202)]

def cat(p):
    a, b = p["km"]
    rnd = a is not None and b is not None and a % 1000 == 0 and b % 1000 == 0
    new = a is not None and b is not None and max(a, b) < 1000
    if "фото" in p["strong"]:
        return "A: тот же файл фото"
    if "телефон" in p["strong"] and p["km_close"] and not rnd and not new:
        return "B: телефон + пробег близко, некруглый"
    if "телефон" in p["strong"] and p["km_close"] and rnd:
        return "C: телефон + пробег близко, круглый"
    if p["km_close"] and a == b and not rnd and not new:
        return "D: пробег совпал точно, некруглый"
    return None
    if p["level"] == "вероятно":
        return "вероятно"
    if p["level"] == "возможно":
        if max(a, b) < 1000:
            return "новые <1000 км"
        if a % 1000 == 0 and b % 1000 == 0:
            return "возможно: круглый пробег"
        return "возможно: некруглый пробег"
    return "один признак, пробег не близок"

by = defaultdict(list)
for p in d["pairs"]:
    c = cat(p)
    if c:
        by[c].append(p)
print({c: len(v) for c, v in by.items()})
sample = []
for c, ps in by.items():
    for p in random.sample(ps, min(70, len(ps))):
        sample.append({**p, "cat": c})
for a, b in KNOWN:
    sample.append({"a": a, "b": b, "cat": "контрольные (ваши)", "kind": "", "strong": [], "level": None, "km": None, "price": None})

ids = {x for p in sample for x in (p["a"], p["b"])}
with psycopg.connect(DSN) as con:
    rows = con.execute("select id, image_url, image_urls, external_id, source_id from listings where id = any(%s)", (list(ids),)).fetchall()
def small(u):
    return u.replace("/i/posters/original/", "/i/posters/api/", 1) if u and "lalafo.com/i/posters/original/" in u else u
photos = {}
for lid, cover, gallery, ext, src in rows:
    urls = [cover] + (gallery or [])
    seen = []
    for u in urls:
        u = small(u)
        if u and "preview.png" not in u and u not in seen:
            seen.append(u)
    photos[lid] = seen[:3]

def dhash(url):
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True)
        img = Image.open(io.BytesIO(r.content)).convert("L").resize((9, 8))
        px = list(img.getdata())
        return sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])
    except Exception:
        return None
urls = sorted({u for us in photos.values() for u in us})
with ThreadPoolExecutor(8) as ex:
    H = dict(zip(urls, ex.map(dhash, urls)))
def best(a, b):
    ds = [bin(H[x] ^ H[y]).count("1") for x in photos.get(a, []) for y in photos.get(b, []) if H.get(x) is not None and H.get(y) is not None]
    return min(ds) if ds else None
res = defaultdict(Counter)
for p in sample:
    dist = best(p["a"], p["b"])
    p["photo_dist"] = dist
    res[p["cat"]]["нет фото" if dist is None else ("фото совпали" if dist <= 8 else "фото разные")] += 1
for c in sorted(res):
    print(c, dict(res[c]))
json.dump(sample, open(sys.argv[2], "w"), ensure_ascii=False, default=str, indent=1)
