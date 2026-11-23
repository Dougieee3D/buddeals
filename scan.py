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
    def num(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    terps = [str(t).lower() for t in (x.get("terps") or []) if t][:6]
    url = str(x.get("url") or "")
    left = x.get("left")
    return {"name": name, "price": price, "was": was if was and was > price else None, "cats": cats, "grams": grams,
            "url": url if url.startswith("http") else "", "terp": num(x.get("terp")), "cbd": num(x.get("cbd")), "terps": terps, "left": int(left) if str(left or "").isdigit() else None,
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


# ---------------------------------------------------------------------------------------------------------
# v2: stores come from Leafly's public dispensary listing (complete, with exact locations); menus come from
# each chain's own site via a per-chain recipe, or from the store's Leafly menu when there's no recipe.
# ---------------------------------------------------------------------------------------------------------
import math
import unicodedata


def slug(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def miles_between(a, b, c, d):
    r = math.radians
    h = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(h))


def _next_data(html_text):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
    return json.loads(m.group(1)) if m else None


def next_data(url, reader=None):
    """A Next.js page's built-in data. Plain download first (fast); a real browser if the site blocks that."""
    try:
        _, _, raw = _get(url, timeout=20)
        j = _next_data(raw.decode("utf-8", "replace"))
        if j:
            return j
    except Exception:
        pass
    if reader is not None and hasattr(reader, "next_data"):
        try:
            return reader.next_data(url)
        except Exception:
            return None
    return None


def _open_text(s):
    o = s.get("openStatus") or {}
    ch = o.get("statusChangeAt") or {}
    if o.get("isOpen24Hours"):
        return "Open 24 hours"
    t = ch.get("time") or ""
    try:
        h, m = (int(x) for x in t.split(":"))
        t = f"{(h % 12) or 12}{':%02d' % m if m else ''}{'am' if h < 12 else 'pm'}"
    except Exception:
        pass
    if o.get("isOpen") is True:
        return f"Open until {t}" if t else "Open now"
    if o.get("isOpen") is False:
        return f"Closed · opens {t}" if t else "Closed now"
    return ""


def leafly_stores(lat, lon, miles, area, reader=None):
    state, city = area.get("state"), area.get("city")
    if not state or not city:
        return []
    out = {}
    for page in range(1, 8):
        url = f"https://www.leafly.com/dispensaries/{slug(state)}/{slug(city)}" + (f"?page={page}" if page > 1 else "")
        j = next_data(url, reader)
        data = ((((j or {}).get("props") or {}).get("pageProps") or {}).get("storeLocatorResults") or {}).get("data") or {}
        stores = [x for v in data.values() if isinstance(v, list) for x in v if isinstance(x, dict) and x.get("slug")]
        if not stores:
            break
        far = 0
        for x in stores:
            a = x.get("address") or {}
            if a.get("lat") is None:
                continue
            if miles_between(lat, lon, a["lat"], a["lon"]) > miles:
                far += 1
                continue
            out[x["slug"]] = {
                "id": "leafly/" + x["slug"], "name": x.get("name") or "Dispensary", "lat": a["lat"], "lon": a["lon"],
                "address": " ".join(v for v in [a.get("address1"), a.get("city"), a.get("state")] if v), "city": a.get("city") or "",
                "phone": x.get("phone") or "", "hours": _open_text(x), "leafly": x["slug"], "hasMenu": bool(x.get("hasMenuItems")),
                "website": "", "tag": x.get("mainTag") or "",
            }
        if far >= len(stores) * 0.8:
            break
    return list(out.values())


LEAFLY_CATS = {"flower": "flower", "pre-roll": "preroll", "preroll": "preroll", "cartridge": "vape", "vape": "vape", "vaporizer": "vape",
               "concentrate": "concentrate", "edible": "edible", "tincture": "tincture", "oral": "tincture", "topical": "topical",
               "accessor": "accessory", "gear": "accessory"}


def _leafly_item(x, src, store_slug=""):
    v = (x.get("variants") or [None])[0] or {}
    price = x.get("price") or v.get("price")
    deal = x.get("deal") or v.get("deal") or {}
    now = deal.get("discountedPrice") if isinstance(deal, dict) else None
    cat = (x.get("productCategory") or "").lower()
    cats = [c for k, c in LEAFLY_CATS.items() if k in cat][:1] or cats_of(x.get("name") or "")
    thc = None
    m = re.search(r"([\d.]+)\s*%", str(x.get("thcContentLabel") or ""))
    if m:
        thc = float(m.group(1))
    cbd = None
    m = re.search(r"([\d.]+)\s*%", str(x.get("cbdContentLabel") or ""))
    if m:
        cbd = float(m.group(1))
    st = x.get("strain") if isinstance(x.get("strain"), dict) else {}
    strain_type = str(st.get("category") or st.get("phenotype") or "")
    tp = st.get("terps") or st.get("terpenes") or {}
    if isinstance(tp, dict):
        terps = [k for k, _ in sorted(tp.items(), key=lambda kv: -(kv[1].get("score", 0) if isinstance(kv[1], dict) else 0))][:3]
    elif isinstance(tp, list):
        terps = [str(t.get("name") if isinstance(t, dict) else t) for t in tp][:3]
    else:
        terps = []
    p = _product({"name": x.get("name"), "price": now or price, "was": price if now and now < (price or 0) else None,
                  "brand": x.get("brandName") or "", "strain": strain_type, "thc": thc, "cbd": cbd, "terps": terps,
                  "size": x.get("normalizedQuantityLabel") or x.get("displayQuantity") or "",
                  "left": x.get("stockQuantity") if isinstance(x.get("stockQuantity"), int) and x.get("stockQuantity") < 20 else None,
                  "url": f"https://www.leafly.com/dispensary-info/{store_slug}/p/{x.get('menuItemId')}/{slug(x.get('name') or '')}?variant={x.get('id')}"
                  if store_slug and x.get("menuItemId") else ""}, src)
    if p:
        p["cats"] = cats
    return p


def leafly_menu(store_slug, reader=None, pages=3):
    deals, products = [], []
    for page in range(1, pages + 1):
        url = f"https://www.leafly.com/dispensary-info/{store_slug}/menu" + (f"?page={page}" if page > 1 else "")
        j = next_data(url, reader)
        pp = (((j or {}).get("props") or {}).get("pageProps") or {})
        md = pp.get("menuData") or {}
        items = md.get("menuItems") or []
        if page == 1:
            for d in pp.get("deals") or []:
                t = " — ".join(v for v in [d.get("title"), (d.get("description") or "")[:160]] if v)
                if t:
                    deals.append({"text": t, "src": f"https://www.leafly.com/dispensary-info/{store_slug}/deals"})
        products += [p for p in (_leafly_item(x, url, store_slug) for x in items) if p]
        if not items or len(products) >= (md.get("totalItems") or 0):
            break
    return bool(j), {"t": time.time(), "deals": deals, "products": products}


# Per-chain recipes: where each chain's menu for a store lives. `pages(store)` gives start pages; `follow` is a
# regex for menu links worth following from them; `more` = how many "Load more" clicks per page.
RECIPES = [
    {"key": "muv", "rx": r"\bm[uü]v\b", "site": "https://muvfl.com",
     "pages": lambda st: [f"https://muvfl.com/locations/{slug(st['city'])}/menu/menu/discounts?promo=products",
                          f"https://muvfl.com/locations/{slug(st['city'])}/menu/menu"],
     "follow": lambda st: rf"/locations/{slug(st['city'])}/menu/menu/(flower|pre-rolls|vapes|edibles|concentrates)-\d+$", "more": 3},
    {"key": "trulieve", "rx": r"trulieve", "site": "https://www.trulieve.com",
     "pages": lambda st: [f"https://www.trulieve.com/category/{c}" for c in ("flower", "vapes", "concentrates", "edibles")],
     "follow": None, "more": 3},
    {"key": "sunnyside", "rx": r"sunnyside", "site": "https://www.sunnyside.shop", "pages": lambda st: ["https://www.sunnyside.shop"], "generic": True, "more": 3},
    {"key": "greendragon", "rx": r"green dragon", "site": "https://greendragon.com", "pages": lambda st: ["https://greendragon.com"], "generic": True, "more": 3},
]


def recipe_for(name):
    return next((r for r in RECIPES if re.search(r["rx"], name, re.I)), None)


def read_recipe(rc, store, reader, ctx):
    """Reads a chain's menu pages for one store with the hidden browser."""
    if reader is None:
        return False, {"t": time.time(), "deals": [], "products": []}
    ctx = {**ctx, "city": store.get("city") or ctx.get("city", ""), "more": rc.get("more", 3)}
    if rc.get("generic"):
        return read_site(rc["pages"](store)[0], reader, ctx)
    pages, seen, deals, products, ok = list(rc["pages"](store)), set(), [], [], False
    follow = rc["follow"](store) if rc.get("follow") else None
    t_end = time.time() + 60
    while pages and len(seen) < 10 and time.time() < t_end:
        url = pages.pop(0)
        if url in seen:
            continue
        seen.add(url)
        r = reader(url, ctx) or {}
        if not r.get("url"):
            continue
        ok = True
        deals += [{"text": t, "src": r["url"]} for t in r.get("lines") or []]
        products += [p for p in (_product(x, r["url"]) for x in r.get("items") or []) if p]
        if follow:
            pages += [l for l in (r.get("links") or []) + (r.get("allLinks") or []) if re.search(follow, l) and l not in seen][:10]
    uniq = list({d["text"].lower(): d for d in deals}.values())[:30]
    prods = list({(p["name"].lower(), p["price"]): p for p in products}.values())[:800]
    return ok, {"t": time.time(), "deals": uniq, "products": prods}


def _source(store):
    rc = recipe_for(store["name"])
    if rc:
        return rc["key"], rc
    return ("leafly:" + store["leafly"], None) if store.get("leafly") else (host_of(store.get("website") or ""), None)


def _out(disps, cache):
    out = []
    for d in disps:
        key, rc = _source(d)
        s = cache["sites"].get(key, {}) if key else {}
        site = (rc or {}).get("site") or d.get("website") or (f"https://www.leafly.com/dispensary-info/{d['leafly']}" if d.get("leafly") else "")
        out.append({**{k: v for k, v in d.items() if not k.startswith("_")}, "website": site, "host": key, "deals": s.get("deals", []),
                    "products": s.get("products", []), "stale": s.get("stale", False), "from": s.get("from", ""),
                    "checked": datetime.fromtimestamp(s["t"], timezone.utc).isoformat() if s.get("t") else None})
    return out


def _area_disps(lat, lon, miles, cache, force, reader=None):
    key = f"L{lat:.2f},{lon:.2f},{int(miles)}"
    hit = cache["areas"].get(key)
    if hit and not force and time.time() - hit["t"] < 24 * 3600:
        return hit["d"], hit.get("area", {}), None
    area = area_of(lat, lon)
    disps = leafly_stores(lat, lon, miles, area, reader)
    note = None
    if not disps:  # Leafly unreachable or no city match: fall back to the map, cannabis shops only
        osm, note = dispensaries(lat, lon, miles, cache, False)
        disps = [d for d in osm if re.search(r"cannabis|dispensar|marijuana|" + "|".join(r["rx"] for r in RECIPES), d["name"], re.I)]
    if disps:
        cache["areas"][key] = {"t": time.time(), "d": disps, "area": area}
    elif hit:
        return hit["d"], hit.get("area", {}), "Couldn't refresh the store list — showing the last one."
    if not disps and not note:
        note = "No dispensaries found within this radius. Try a bigger radius."
    return disps, area, note


def cached(lat, lon, miles):
    """Instant: whatever was read before for this area (no browsing)."""
    with _lock:
        cache = _load()
    key = f"L{lat:.2f},{lon:.2f},{int(miles)}"
    hit = cache["areas"].get(key)
    return {"dispensaries": _out(hit["d"], cache) if hit else [], "updated": None, "error": None}


def scan(lat, lon, miles, force=False, progress=None, reader=None, agree=False, on_site=None, only_host=None, workers=8):
    with _lock:
        cache = _load()
    disps, area, note = _area_disps(lat, lon, miles, cache, False, reader)
    for d in disps:
        d["_dist"] = miles_between(lat, lon, d["lat"], d["lon"])
    jobs = {}  # source key -> (recipe, nearest store of that source)
    for d in sorted(disps, key=lambda x: x["_dist"]):
        k, rc = _source(d)
        if k and k not in jobs:
            jobs[k] = (rc, d)
    if only_host:
        todo = [k for k in jobs if k == only_host]
    else:
        todo = [k for k in jobs if force or time.time() - cache["sites"].get(k, {}).get("t", 0) > SITE_TTL]
    ctx = {**area, "agree": agree}

    def work(k):
        rc, st = jobs[k]
        if rc:
            ok, data = read_recipe(rc, st, reader, ctx)
            data["from"] = f"{re.sub(r'\s*[-–].*$', '', st['name'])} website ({st.get('city', '')} store)"
            if not data["products"] and st.get("leafly") and st.get("hasMenu"):
                ok2, d2 = leafly_menu(st["leafly"], reader)
                if d2["products"]:
                    d2["deals"] = data["deals"] + d2["deals"]
                    d2["from"] = "Leafly menu"
                    return ok2, d2
            return ok, data
        if k.startswith("leafly:"):
            if not st.get("hasMenu"):
                return True, {"t": time.time(), "deals": [], "products": [], "from": "no online menu"}
            ok, data = leafly_menu(st["leafly"], reader)
            data["from"] = "Leafly menu"
            return ok, data
        ok, data = read_site(st.get("website"), reader, ctx)
        data["from"] = "store website"
        return ok, data

    done = 0
    if on_site and disps:
        on_site(_out(disps, cache))  # stores show up right away; menus fill in as each one is read
    if progress:
        progress(0, len(todo))
    with cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo) or 1))) as pool:
        futs = {pool.submit(work, k): k for k in todo}
        for f in cf.as_completed(futs):
            k = futs[f]
            try:
                ok, data = f.result()
            except Exception:
                ok, data = False, {"t": time.time(), "deals": [], "products": []}
            with _lock:
                old = cache["sites"].get(k, {})
                if ok or not (old.get("deals") or old.get("products")):
                    _history(cache, k, data["products"])
                    cache["sites"][k] = data
                else:
                    old["stale"] = True
            done += 1
            if on_site:
                on_site(_out([d for d in disps if _source(d)[0] == k], cache))
            if progress:
                progress(done, len(todo))
    with _lock:
        c2 = _load()
        c2["areas"].update(cache["areas"])
        c2["sites"].update({k: cache["sites"][k] for k in todo if k in cache["sites"]})
        c2.setdefault("hist", {}).update(cache.get("hist", {}))
        _save(c2)
    return {"dispensaries": _out(disps, cache), "updated": datetime.now(timezone.utc).isoformat(), "error": note, "sitesRead": len(jobs)}
