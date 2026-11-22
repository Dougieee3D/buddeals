"""Finds dispensaries (OpenStreetMap) and reads deal text from their public websites. No UI code here."""
import concurrent.futures as cf
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "BudDeals")
CACHE_FILE = os.path.join(DATA_DIR, "cache.json")
APP_UA = "BudDeals/1.0 (personal dispensary deal finder; Windows desktop app)"
PAGE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36 BudDeals/1.0"
SITE_TTL = 3 * 3600          # re-read a dispensary's site after 3 hours
AREA_TTL = 7 * 24 * 3600     # dispensary list for an area is good for a week
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
NAME_RX = ("dispensar|cannabis|marijuana|weed|Trulieve|Curaleaf|Sunnyside|Liberty Health|Fluent|M[UÜ]V|Surterra|Vidacann|"
           "Insa|RISE|Zen Leaf|AYR|GrowHealthy|Jungle Boys|Sanctuary|Flowery|Green Dragon|Cookies|Planet 13|Thrive|"
           "Cannabist|Beyond Hello|Verilife|Story Cannabis|Gold Flora")
DEAL_RX = re.compile(r"(\d{1,2}\s*%\s*off|\bbogo\b|b1g1|buy\s*(\d+|one)\s*,?\s*get|\$\s?\d+\s*off|spend\s*(over\s*)?\$\s?\d+|"
                     r"(?<![/\d.])\d{1,2}\s*(for|/)\s*\$\s?\d+)", re.I)
LINK_RX = re.compile(r"deal|special|promo|discount|sale|offer|daily", re.I)
CATS = {
    "flower": r"flower|eighths?|1/8s?|quarters?|ounces?|oz|smalls|shake|buds?",
    "preroll": r"pre-?rolls?|joints?|blunts?",
    "vape": r"vapes?|carts?|cartridges?|pods?|disposables?|pens?",
    "concentrate": r"concentrates?|wax|shatter|rosin|resin|badder|budder|batter|diamonds?|crumble|sugar|sauce|rso|hash|kief|dabs?",
    "edible": r"edibles?|gumm(?:y|ies)|chocolates?|cand(?:y|ies)|beverages?|drinks?|mints?|chews?",
    "tincture": r"tinctures?|capsules?|tablets?|syringes?|sublingual",
    "topical": r"topicals?|lotions?|balms?|creams?|salves?|patch(?:es)?",
    "accessory": r"accessor(?:y|ies)|glass|pipes?|bongs?|batter(?:y|ies)|papers?|grinders?|lighters?",
}

_lock = threading.Lock()


def _load():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"areas": {}, "sites": {}}


def _save(cache):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)


def _get(url, data=None, timeout=15, ua=PAGE_UA, limit=3_000_000, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers.get("content-type", ""), r.geturl(), r.read(limit)


def _json(url):
    return json.loads(_get(url, ua=APP_UA, timeout=15)[2])


def geocode(q):
    q = q.strip()
    if re.fullmatch(r"\d{5}", q):
        try:
            j = _json("https://api.zippopotam.us/us/" + q)
            p = j["places"][0]
            return {"lat": float(p["latitude"]), "lon": float(p["longitude"]), "label": f"{p['place name']}, {p['state abbreviation']} {q}"}
        except Exception:
            pass
    j = _json("https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" + urllib.parse.quote(q))
    if not j:
        return None
    return {"lat": float(j[0]["lat"]), "lon": float(j[0]["lon"]), "label": ",".join(j[0]["display_name"].split(",")[:3])}


def reverse(lat, lon):
    try:
        a = _json(f"https://nominatim.openstreetmap.org/reverse?format=json&zoom=10&lat={lat}&lon={lon}").get("address", {})
        return ", ".join(x for x in [a.get("city") or a.get("town") or a.get("village") or a.get("county"), a.get("state")] if x) or "your location"
    except Exception:
        return "your location"


def overpass_query(lat, lon, miles):
    r = round(miles * 1609.34)
    a = f"(around:{r},{lat},{lon})"
    # Step 1: every shop/clinic in range (cheap tag lookup). Step 2: keep only cannabis ones (filter on that small set).
    return (f'[out:json][timeout:120];(nwr["shop"]{a};nwr["healthcare"]{a};)->.all;'
            f'(nwr.all["shop"="cannabis"];nwr.all["name"~"{NAME_RX}",i];);out center tags;')


def _to_disp(e):
    t = e.get("tags", {})
    c = e.get("center", {})
    return {
        "id": f"{e['type']}/{e['id']}", "name": t.get("name") or t.get("brand") or "Dispensary",
        "lat": e.get("lat", c.get("lat")), "lon": e.get("lon", c.get("lon")),
        "address": " ".join(x for x in [t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:city"), t.get("addr:state")] if x),
        "website": t.get("website") or t.get("contact:website") or t.get("url") or "",
        "phone": t.get("phone") or t.get("contact:phone") or "", "hours": t.get("opening_hours") or "",
    }


def dispensaries(lat, lon, miles, cache, force=False):
    key = f"{lat:.2f},{lon:.2f},{int(miles)}"
    hit = cache["areas"].get(key)
    if hit and not force and time.time() - hit["t"] < AREA_TTL:
        return hit["d"], None
    body = urllib.parse.urlencode({"data": overpass_query(lat, lon, miles)}).encode()
    err = None
    for ep in OVERPASS:
        try:
            els = json.loads(_get(ep, data=body, timeout=150, ua=APP_UA, limit=50_000_000,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})[2]).get("elements", [])
            d = [x for x in map(_to_disp, els) if x["lat"] is not None]
            cache["areas"][key] = {"t": time.time(), "d": d}
            return d, None
        except Exception as e:
            err = e
    if hit:
        return hit["d"], "Map server is busy — showing the last dispensary list."
    return [], f"Couldn't reach the map servers ({err}). Try again in a minute."


def _walk(o, out):
    if len(out) > 80:
        return
    if isinstance(o, list):
        for x in o:
            _walk(x, out)
        return
    if not isinstance(o, dict):
        return
    types = o.get("@type") or []
    types = types if isinstance(types, list) else [types]
    if any("Product" in str(t) for t in types) and o.get("name"):
        off = o.get("offers") or {}
        off = off[0] if isinstance(off, list) and off else off
        try:
            price = float(off.get("price") or off.get("lowPrice") or 0) if isinstance(off, dict) else 0
        except (TypeError, ValueError):
            price = 0
        if price > 0:
            name = str(o["name"])[:120]
            out.append({"name": name, "price": price, "cats": cats_of(name + " " + str(o.get("category", "")))})
    for v in o.values():
        if isinstance(v, (dict, list)):
            _walk(v, out)


def cats_of(text):
    found = [c for c, rx in CATS.items() if re.search(rf"\b({rx})\b", text, re.I)]
    return found or ["all"]


def extract(page, base):
    products, links, deals = [], [], []
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>([\s\S]*?)</script>", page, re.I):
        try:
            _walk(json.loads(m.group(1)), products)
        except Exception:
            pass
    host = urllib.parse.urlparse(base).hostname
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>([\s\S]{0,200}?)</a>", page, re.I):
        label = re.sub(r"<[^>]+>", " ", m.group(2))
        if LINK_RX.search(m.group(1) + " " + label):
            u = urllib.parse.urljoin(base, m.group(1))
            if urllib.parse.urlparse(u).hostname == host and u not in links:
                links.append(u)
    text = re.sub(r"<(script|style|svg|head)[\s\S]*?</\1>", " ", page, flags=re.I)
    text = re.sub(r"</(p|div|li|h\d|tr|section|article|button|a)>|<br\s*/?>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    for line in re.split(r"\n|(?<=[.!])\s+(?=[A-Z])", text):
        s = re.sub(r"\s+", " ", line).strip()
        if 6 <= len(s) <= 240 and DEAL_RX.search(s):
            deals.append(s)
    return {"deals": deals, "links": links[:3], "products": products}


def read_site(website):
    base = website if re.match(r"https?:", website, re.I) else "https://" + website
    pages, seen, deals, products, ok = [base], set(), [], [], False
    i = 0
    while i < len(pages) and i < 5:
        url = pages[i]
        i += 1
        if url in seen:
            continue
        seen.add(url)
        try:
            ctype, final, raw = _get(url)
            if "html" not in ctype.lower():
                continue
            ok = True
            r = extract(raw.decode("utf-8", "replace"), final)
            deals += [{"text": s, "src": final} for s in r["deals"]]
            products += r["products"]
            if len(seen) == 1:
                pages += r["links"] or [urllib.parse.urljoin(base, "/deals"), urllib.parse.urljoin(base, "/specials")]
        except Exception:
            pass
    uniq = list({d["text"].lower(): d for d in deals}.values())[:20]
    return ok, {"t": time.time(), "deals": uniq, "products": products[:60]}


def scan(lat, lon, miles, force=False, progress=None):
    with _lock:
        cache = _load()
    disps, note = dispensaries(lat, lon, miles, cache, force)
    todo = [d for d in disps if d["website"] and (force or time.time() - cache["sites"].get(d["website"], {}).get("t", 0) > SITE_TTL)]
    done = 0
    if progress and todo:
        progress(0, len(todo))
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(read_site, d["website"]): d for d in todo}
        for f in cf.as_completed(futs):
            ok, data = f.result()
            site = futs[f]["website"]
            if ok or not cache["sites"].get(site, {}).get("deals"):
                cache["sites"][site] = data
            else:
                cache["sites"][site]["stale"] = True
            done += 1
            if progress and (done % 3 == 0 or done == len(todo)):
                progress(done, len(todo))
    out = []
    for d in disps:
        s = cache["sites"].get(d["website"], {}) if d["website"] else {}
        out.append({**d, "deals": s.get("deals", []), "products": s.get("products", []), "stale": s.get("stale", False),
                    "checked": datetime.fromtimestamp(s["t"], timezone.utc).isoformat() if s.get("t") else None})
    with _lock:
        _save(cache)
    return {"dispensaries": out, "updated": datetime.now(timezone.utc).isoformat(), "error": note}
