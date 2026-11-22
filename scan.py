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
SITE_TTL = 15 * 60           # re-read a dispensary's menu if older than 15 minutes when you search
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

# Chain websites, used when the map listing has no website.
CHAIN_SITES = [
    (r"trulieve", "https://www.trulieve.com/deals"), (r"curaleaf", "https://curaleaf.com"), (r"m[uü]v", "https://muvfl.com"),
    (r"sunnyside", "https://www.sunnyside.shop"), (r"green dragon", "https://greendragon.com"), (r"\bayr\b", "https://ayrdispensaries.com"),
    (r"liberty health", "https://www.libertyhealthsciences.com"), (r"fluent", "https://getfluent.com"), (r"\brise\b", "https://risecannabis.com"),
    (r"zen leaf", "https://zenleafdispensaries.com"), (r"cannabist", "https://www.gocannabist.com"), (r"\binsa\b", "https://insa.com"),
    (r"jungle boys", "https://jungleboysflorida.com"), (r"flowery", "https://theflowery.co"),
]
GRAM_RX = [(r"(\d+(?:\.\d+)?)\s*(?:g|gram|grams)\b", None), (r"\b1/8\b|eighth", 3.5), (r"\b1/4\b|quarter", 7.0),
           (r"\b1/2\s*oz|half\s*(?:oz|ounce)", 14.0), (r"\b(?:1\s*)?(?:oz|ounce)\b", 28.0)]


def grams_of(name):
    for rx, val in GRAM_RX:
        m = re.search(rx, name, re.I)
        if m:
            g = val or float(m.group(1))
            return g if 0.1 <= g <= 56 else None
    return None


def host_of(url):
    try:
        u = url if re.match(r"https?:", url, re.I) else "https://" + url
        return (urllib.parse.urlparse(u).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


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


WIN_LOCATION_PS = (
    "Add-Type -AssemblyName System.Device;"
    "$w=New-Object System.Device.Location.GeoCoordinateWatcher;$w.Start();$t=0;"
    "while(($w.Status -ne 'Ready') -and ($w.Permission -ne 'Denied') -and $t -lt 100){Start-Sleep -Milliseconds 100;$t++};"
    "$c=$w.Position.Location;if($c.IsUnknown){'none'}else{\"$($c.Latitude),$($c.Longitude)\"}"
)


def locate_me():
    """Windows location first (exact, if Location is on in Windows settings), then IP-based (city-level)."""
    if os.name == "nt":
        try:
            import subprocess
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", WIN_LOCATION_PS], capture_output=True,
                                 text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
            if "," in out:
                lat, lon = (float(x) for x in out.split(",")[:2])
                return {"lat": lat, "lon": lon, "label": reverse(lat, lon)}
        except Exception:
            pass
    for url in ("https://ipwho.is/", "https://ipapi.co/json/"):
        try:
            j = _json(url)
            lat, lon = float(j["latitude"]), float(j["longitude"])
            label = ", ".join(x for x in [j.get("city"), j.get("region_code") or j.get("region")] if x) or "your area"
            return {"lat": lat, "lon": lon, "label": label, "approx": True}
        except Exception:
            pass
    return None


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
        "website": t.get("website") or t.get("contact:website") or t.get("url")
        or next((u for rx, u in CHAIN_SITES if re.search(rx, t.get("name") or t.get("brand") or "", re.I)), ""),
        "phone": t.get("phone") or t.get("contact:phone") or "", "hours": t.get("opening_hours") or "",
        "city": t.get("addr:city") or "",
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


def area_of(lat, lon):
    """State name + ZIP for answering a site's 'pick your state / store' screens."""
    try:
        a = _json(f"https://nominatim.openstreetmap.org/reverse?format=json&zoom=18&addressdetails=1&lat={lat}&lon={lon}").get("address", {})
        return {"state": a.get("state", ""), "zip": (a.get("postcode") or "")[:5], "city": a.get("city") or a.get("town") or a.get("village") or ""}
    except Exception:
        return {"state": "", "zip": "", "city": ""}


def _product(x, src):
    try:
        price = float(x.get("price") or 0)
    except (TypeError, ValueError):
        return None
    name = str(x.get("name") or "")[:140]
    if not name or not 0.5 <= price <= 2000:
        return None
    was = x.get("was")
    m = re.search(r"(\d{1,2})\s*%", x.get("tag") or "")
    if not was and m and 5 <= int(m.group(1)) < 90:
        was = round(price / (1 - int(m.group(1)) / 100), 2)
    cats = cats_of(name)
    if cats == ["all"]:
        cats = cats_of(urllib.parse.unquote(urllib.parse.urlparse(src).path).replace("-", " ").replace("/", " "))
    try:
        thc = float(x.get("thc") or 0) or None
    except (TypeError, ValueError):
        thc = None
    try:
        mg = float(x.get("mg") or 0) or None
    except (TypeError, ValueError):
        mg = None
    grams = grams_of(name) or grams_of(str(x.get("size") or ""))
    mg_thc = mg or (grams * 10 * thc if grams and thc and thc < 100 else None)  # g * 1000 mg * thc%/100
    return {"name": name, "price": price, "was": was if was and was > price else None, "cats": cats, "grams": grams,
            "thc": thc, "mgThc": round(mg_thc) if mg_thc else None, "brand": str(x.get("brand") or "")[:60],
            "type": str(x.get("strain") or "")[:20], "src": src}


def read_site(website, reader=None, ctx=None, known_pages=None):
    """reader(url, ctx) -> {url, lines, items, links} from a real (hidden) browser; falls back to a plain download.
    known_pages: pages that had products last time — read those directly instead of discovering again."""
    base = website if re.match(r"https?:", website, re.I) else "https://" + website
    start = list(dict.fromkeys(known_pages or [])) or [base]
    pages, seen, deals, products, ok, useful = start[:], set(), [], [], False, []
    limit = max(len(start), 9 if reader else 4)
    i = 0
    while i < len(pages) and i < limit:
        url = pages[i]
        i += 1
        if url in seen:
            continue
        seen.add(url)
        try:
            if reader:
                r = reader(url, ctx) or {}
                final = r.get("url") or url
                lines, links = r.get("lines") or [], r.get("links") or []
                prods = [p for p in (_product(x, final) for x in r.get("items") or []) if p]
                ok = ok or bool(r.get("url"))
            else:
                ctype, final, raw = _get(url)
                if "html" not in ctype.lower():
                    continue
                ok = True
                r = extract(raw.decode("utf-8", "replace"), final)
                lines, links = r["deals"], r["links"]
                prods = [p for p in (_product({**x, "size": ""}, final) for x in r["products"]) if p]
            deals += [{"text": t, "src": final} for t in lines]
            products += prods
            if prods or lines:
                useful.append(url)
            if len(seen) == 1 and not known_pages:
                pages += links[:8] if reader else (links[:3] or [urllib.parse.urljoin(base, "/deals"), urllib.parse.urljoin(base, "/specials")])
        except Exception:
            pass
    if known_pages and ok and not products and not deals:
        return read_site(website, reader, ctx)  # the site changed; discover its pages again
    uniq = list({d["text"].lower(): d for d in deals}.values())[:30]
    prods = list({(p["name"].lower(), p["price"]): p for p in products}.values())[:600]
    return ok, {"t": time.time(), "deals": uniq, "products": prods, "pages": useful[:9]}


def _history(cache, host, products):
    """Keeps each product's lowest price per day (60 days) and adds: low30, typical, days seen, lowest-now, inflated sale."""
    hist = cache.setdefault("hist", {})
    today = time.strftime("%Y-%m-%d")
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 60 * 86400))
    month = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
    for p in products:
        k = f"{host}|{p['name'].lower()}"
        h = {d: v for d, v in hist.get(k, {}).items() if d >= cutoff}
        h[today] = min(h.get(today, 1e9), p["price"])
        hist[k] = h
        last30 = [v for d, v in h.items() if d >= month]
        vals = sorted(h.values())
        p["low30"] = min(last30)
        p["typical"] = vals[len(vals) // 2]
        p["daysSeen"] = len(h)
        p["isLow"] = len(h) >= 3 and p["price"] <= p["low30"] and p["price"] < vals[-1]
        # "was $X" but we've never seen it anywhere near $X, and today's price is just the usual price
        p["inflated"] = bool(p.get("was") and len(h) >= 5 and p["was"] > max(vals) * 1.15 and p["price"] >= p["typical"])


def _out(disps, cache):
    out = []
    for d in disps:
        h = host_of(d["website"]) if d["website"] else ""
        s = cache["sites"].get(h, {}) if h else {}
        out.append({**{k: v for k, v in d.items() if not k.startswith("_")}, "host": h, "deals": s.get("deals", []),
                    "products": s.get("products", []), "stale": s.get("stale", False),
                    "checked": datetime.fromtimestamp(s["t"], timezone.utc).isoformat() if s.get("t") else None})
    return out


def _area_disps(lat, lon, miles, cache, force):
    disps, note = dispensaries(lat, lon, miles, cache, force)
    for d in disps:
        if not d["website"]:
            d["website"] = next((u for rx, u in CHAIN_SITES if re.search(rx, d["name"], re.I)), "")
        d["_dist"] = (d["lat"] - lat) ** 2 + (d["lon"] - lon) ** 2
    if not disps and not note:
        note = "No dispensaries found on the map within this radius. Try a bigger radius."
    return disps, note


def cached(lat, lon, miles):
    """Instant: whatever was read before for this area (only goes online if the area's store list isn't known yet)."""
    with _lock:
        cache = _load()
    disps, note = _area_disps(lat, lon, miles, cache, False)
    with _lock:
        c2 = _load()
        c2["areas"].update(cache["areas"])
        _save(c2)
    return {"dispensaries": _out(disps, cache), "updated": None, "error": note}


def scan(lat, lon, miles, force=False, progress=None, reader=None, agree=False, on_site=None, only_host=None, workers=8):
    with _lock:
        cache = _load()
    disps, note = _area_disps(lat, lon, miles, cache, False)
    area = area_of(lat, lon) if reader else {}
    sites, ctxs = {}, {}
    for d in sorted(disps, key=lambda x: x["_dist"]):
        h = host_of(d["website"]) if d["website"] else ""
        if h and h not in sites:
            sites[h] = d["website"]
            ctxs[h] = {**area, "city": d.get("city") or area.get("city", ""), "agree": agree}
    if only_host:
        todo = [(h, w) for h, w in sites.items() if h == only_host]
    else:
        todo = [(h, w) for h, w in sites.items() if force or time.time() - cache["sites"].get(h, {}).get("t", 0) > SITE_TTL]
    done = 0
    if progress:
        progress(0, len(todo))
    with cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo) or 1)) if reader else 8) as pool:
        futs = {pool.submit(read_site, w, reader, ctxs.get(h), cache["sites"].get(h, {}).get("pages")): h for h, w in todo}
        for f in cf.as_completed(futs):
            h = futs[f]
            try:
                ok, data = f.result()
            except Exception:
                ok, data = False, {"t": time.time(), "deals": [], "products": []}
            with _lock:
                old = cache["sites"].get(h, {})
                if ok or not (old.get("deals") or old.get("products")):
                    _history(cache, h, data["products"])
                    cache["sites"][h] = data
                else:
                    old["stale"] = True
            done += 1
            if on_site:
                on_site(_out([d for d in disps if host_of(d["website"] or "") == h], cache))
            if progress:
                progress(done, len(todo))
    with _lock:
        c2 = _load()  # merge in case another scan saved meanwhile
        c2["areas"].update(cache["areas"])
        c2["sites"].update({h: cache["sites"][h] for h, _ in todo if h in cache["sites"]})
        c2.setdefault("hist", {}).update(cache.get("hist", {}))
        _save(c2)
    return {"dispensaries": _out(disps, cache), "updated": datetime.now(timezone.utc).isoformat(), "error": note, "sitesRead": len(sites)}
