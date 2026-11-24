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
SITE_TTL = 2 * 3600          # a normal search re-reads a menu older than 2 hours (Refresh prices re-reads everything)
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
GRAM_RX = [(r"(?<![\d.])(\d*\.\d+|\d+)\s*(?:g|gram|grams)\b", None), (r"\b1/8\b|eighth", 3.5), (r"\b1/4\b|quarter", 7.0),
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
    if cats == ["all"] and x.get("category"):
        cats = cats_of(str(x.get("category")))
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
            "type": str(x.get("strain") or "")[:20], "src": src,
            "hi": num(x.get("hi")) if num(x.get("hi")) and num(x.get("hi")) > price else None}


STATE_NAMES = dict(x.rsplit(" ", 1)[::-1] for x in (
    "Alabama AL,Alaska AK,Arizona AZ,Arkansas AR,California CA,Colorado CO,Connecticut CT,Delaware DE,District of Columbia DC,"
    "Florida FL,Georgia GA,Hawaii HI,Idaho ID,Illinois IL,Indiana IN,Iowa IA,Kansas KS,Kentucky KY,Louisiana LA,Maine ME,"
    "Maryland MD,Massachusetts MA,Michigan MI,Minnesota MN,Mississippi MS,Missouri MO,Montana MT,Nebraska NE,Nevada NV,"
    "New Hampshire NH,New Jersey NJ,New Mexico NM,New York NY,North Carolina NC,North Dakota ND,Ohio OH,Oklahoma OK,Oregon OR,"
    "Pennsylvania PA,Rhode Island RI,South Carolina SC,South Dakota SD,Tennessee TN,Texas TX,Utah UT,Vermont VT,Virginia VA,"
    "Washington WA,West Virginia WV,Wisconsin WI,Wyoming WY").split(","))
# states where dispensaries only sell to medical patients: choose the medical menu there
MED_ONLY = {"Florida", "Pennsylvania", "Texas", "Georgia", "Oklahoma", "Arkansas", "Utah", "Louisiana", "Mississippi", "Alabama",
            "Hawaii", "New Hampshire", "West Virginia", "North Dakota", "South Dakota", "Kentucky", "Iowa", "Tennessee"}


def state_name(st):
    st = (st or "").strip()
    return STATE_NAMES.get(st.upper(), st)


# ---------- product data the menu itself loads (Dutchie, Jane, Shopify/Woo, Next.js…) ----------
_JANE_SIZES = {"half_gram": "0.5g", "gram": "1g", "two_gram": "2g", "eighth_ounce": "3.5g", "eighth": "3.5g", "quarter_ounce": "7g",
               "quarter": "7g", "half_ounce": "14g", "ounce": "28g", "each": ""}
_NAME_KEYS = ("name", "productname", "product_name", "title", "displayname", "display_name")
_PRODUCTISH = re.compile(r"brand|thc|cbd|strain|categor|kind|type|weight|option|variant|sku|potency|cannabinoid|offers|terpene|image|lineage", re.I)


def _money(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.fullmatch(r"\s*\$?\s*(\d{1,4}(?:\.\d{1,2})?)\s*", v)
        return float(m.group(1)) if m else None
    return None


def _pct(v):
    if isinstance(v, dict):
        rng = v.get("range") or v.get("Range")
        if isinstance(rng, list) and rng:
            v = max([x for x in rng if isinstance(x, (int, float))] or [0])
        else:
            v = v.get("value") or v.get("max") or v.get("formatted")
    if isinstance(v, list) and v:
        v = max([x for x in v if isinstance(x, (int, float))] or [0])
    if isinstance(v, str):
        m = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%", v) or re.fullmatch(r"\s*(\d{1,2}(?:\.\d+)?)\s*", v)
        v = float(m.group(1)) if m else None
    return float(v) if isinstance(v, (int, float)) and 0 < v < 100 else None


def _json_item(o, page, med):
    """One product node from a menu's JSON -> list of product dicts (one per size when sizes are listed)."""
    low = {k.lower(): k for k in o}
    name = next((o[low[k]] for k in _NAME_KEYS if k in low and isinstance(o[low[k]], str)), None)
    if not name or not 1 < len(name.strip()) < 140 or not any(_PRODUCTISH.search(k) for k in o):
        return []
    brand = o.get(low.get("brand")) if "brand" in low else o.get(low.get("brandname") or low.get("brand_name") or "", "")
    if isinstance(brand, dict):
        brand = brand.get("name") or brand.get("Name") or ""
    cat = next((o[low[k]] for k in ("category", "type", "kind", "producttype", "product_type", "root_subtype", "subcategory") if k in low and isinstance(o[low[k]], str)), "")
    strain = next((o[low[k]] for k in ("straintype", "strain_type", "lineage", "category") if k in low and isinstance(o[low[k]], str) and o[low[k]].lower() in ("indica", "sativa", "hybrid", "cbd")), "")
    thc = next((_pct(o[k]) for k in o if re.fullmatch(r"(percent_)?thc(content|_?percent(age)?|a?)?", k, re.I) and _pct(o[k])), None)
    cbd = next((_pct(o[k]) for k in o if re.fullmatch(r"(percent_)?cbd(content|_?percent(age)?|a?)?", k, re.I) and _pct(o[k])), None)
    terps = []
    for k in o:
        if re.search(r"terpene", k, re.I) and isinstance(o[k], list):
            terps = [str(t.get("name") or t.get("Name") or "") if isinstance(t, dict) else str(t) for t in o[k]][:6]
    url = next((o[low[k]] for k in ("url", "href", "permalink", "link", "product_url") if k in low and isinstance(o[low[k]], str)), "")
    url = urllib.parse.urljoin(page, url) if url else ""
    base = {"name": name.strip(), "brand": str(brand or "")[:60], "category": cat, "strain": strain, "thc": thc, "cbd": cbd,
            "terps": [t for t in terps if t], "url": url}
    out = []
    # Jane: price_gram / discounted_price_gram … one per size
    jane = {k[6:]: _money(v) for k, v in o.items() if k.startswith("price_") and _money(v)}
    if jane:
        for size, price in jane.items():
            disc = _money(o.get("discounted_price_" + size) or o.get("special_price_" + size))
            label = _JANE_SIZES.get(size, size.replace("_", " "))
            nm = base["name"] + (f" {label}" if label and label.lower() not in base["name"].lower() else "")
            out.append({**base, "name": nm, "price": disc if disc and disc < price else price, "was": price if disc and disc < price else None, "size": label})
        return out
    # Dutchie-style aligned arrays: Options ["1g","3.5g"], Prices / recPrices / medicalPrices, *SpecialPrices
    arrays = {k: v for k, v in o.items() if isinstance(v, list) and v and all(_money(x) is not None for x in v) and re.search(r"price", k, re.I)}
    opts = next((o[k] for k in o if re.fullmatch(r"options|weights|sizes", k, re.I) and isinstance(o[k], list) and o[k] and all(isinstance(x, str) for x in o[k])), None)
    if arrays:
        def pick(special):
            ks = [k for k in arrays if bool(re.search(r"special|sale|discount", k, re.I)) == special]
            pref = [k for k in ks if re.search(r"^med", k, re.I)] if med else [k for k in ks if re.search(r"^rec", k, re.I)]
            ks = pref or [k for k in ks if not re.search(r"^(med|rec)", k, re.I)] or ([] if special else ks)
            return arrays[ks[0]] if ks else None
        reg, spec = pick(False), pick(True)
        if reg:
            for i, v in enumerate(reg):
                price, sp = _money(v), _money(spec[i]) if spec and i < len(spec) else None
                label = opts[i] if opts and i < len(opts) else ""
                nm = base["name"] + (f" {label}" if label and label.lower() not in base["name"].lower() and label.upper() != "N/A" else "")
                if price:
                    out.append({**base, "name": nm, "price": sp if sp and sp < price else price, "was": price if sp and sp < price else None, "size": label})
            return out
    # variants / offers: [{price, option/title}]
    for k in ("variants", "Variants", "offers", "options"):
        vs = o.get(k)
        if isinstance(vs, dict):
            vs = [vs]
        if isinstance(vs, list) and vs and isinstance(vs[0], dict):
            for v in vs[:12]:
                price = _money(v.get("price") or v.get("Price") or v.get("lowPrice"))
                was = _money(v.get("compare_at_price") or v.get("compareAtPrice") or v.get("regular_price") or v.get("originalPrice"))
                label = str(v.get("option") or v.get("title") or v.get("name") or v.get("weight") or "")
                if label.lower() in ("default title", base["name"].lower()):
                    label = ""
                nm = base["name"] + (f" {label}" if label and label.lower() not in base["name"].lower() else "")
                if price:
                    out.append({**base, "name": nm, "price": price, "was": was if was and was > price else None, "size": label})
            if out:
                return out
    # single price fields
    prices = {k: _money(v) for k, v in o.items() if re.search(r"price", k, re.I) and _money(v)}
    if prices:
        regs = {k: v for k, v in prices.items() if not re.search(r"special|sale|discount|compare|original|regular|was|per|unit", k, re.I)}
        sale = [v for k, v in prices.items() if re.search(r"special|sale|discount", k, re.I)]
        was = [v for k, v in prices.items() if re.search(r"compare|original|regular|was", k, re.I)]
        pref = {k: v for k, v in regs.items() if (k.lower().startswith("med") if med else k.lower().startswith("rec"))}
        price = min((pref or regs or prices).values())
        if sale and min(sale) < price:
            price, was = min(sale), [price]
        w = max(was) if was else None
        out.append({**base, "price": price, "was": w if w and w > price else None,
                    "size": str(o.get(low.get("weight", ""), "") or o.get(low.get("size", ""), "") or "")})
    return out


def json_products(texts, page, med=False):
    """Products from the JSON a menu page loaded. Only lists of 3+ products (a real menu), or schema.org Product entries."""
    found, seen = [], set()
    def walk(node, depth=0):
        if depth > 12:
            return
        if isinstance(node, list):
            batch = []
            for x in node[:2000]:
                if isinstance(x, dict):
                    batch += _json_item(x, page, med)
            if len(batch) >= 3:
                found.extend(batch)
                return
            for x in node[:2000]:
                walk(x, depth + 1)
        elif isinstance(node, dict):
            if str(node.get("@type", "")).lower() == "product":
                found.extend(_json_item(node, page, med))
                return
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
    for t in texts or []:
        try:
            walk(json.loads(t))
        except Exception:
            continue
    out = []
    for x in found:
        key = (x["name"].lower(), x["price"])
        if key not in seen and 0.5 <= x["price"] <= 2000:
            seen.add(key)
            out.append(x)
    return out


def page_products(r, page, ctx=None):
    """Products from one page read: the menu's own data when it has it (exact prices, sizes, THC), else the visible cards."""
    prods = [p for p in (_product(x, page) for x in r.get("items") or []) if p]
    jp = json_products(r.get("net"), page, (ctx or {}).get("state") in MED_ONLY)
    if jp and len(jp) >= 0.6 * len(prods):
        urls = {re.sub(r"\W+", "", p["name"].lower())[:24]: p["url"] for p in prods if p.get("url")}
        out = []
        for x in jp:
            x["url"] = x.get("url") or urls.get(re.sub(r"\W+", "", x["name"].lower())[:24], "")
            p = _product(x, page)
            if p:
                out.append(p)
        if out:
            return out
    return prods


def read_site(website, reader=None, ctx=None, known_pages=None):
    """reader(url, ctx) -> {url, lines, items, links} from a real (hidden) browser; falls back to a plain download.
    known_pages: pages that had products last time — read those directly instead of discovering again."""
    base = website if re.match(r"https?:", website, re.I) else "https://" + website
    start = list(dict.fromkeys(known_pages or [])) or [base]
    pages, seen, deals, products, ok, useful, dbg = start[:], set(), [], [], False, [], []
    limit = max(len(start), 18 if reader else 4)  # home page + every category page (edibles, topicals, accessories…)
    t_end = time.time() + 120
    i = 0
    while i < len(pages) and i < limit and time.time() < t_end:
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
                for e in (r.get("embeds") or [])[:2]:  # the menu lives in an embedded ordering system: read it directly
                    if e not in seen and e not in pages:
                        pages.insert(i, e)
                prods = page_products(r, final, ctx)
                ok = ok or bool(r.get("url"))
                if r.get("dbg") and len(dbg) < 4:
                    dbg.append({"url": final, **r["dbg"]})
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
                pages += links[:16] if reader else (links[:3] or [urllib.parse.urljoin(base, "/deals"), urllib.parse.urljoin(base, "/specials")])
        except Exception:
            pass
    if reader and not products and not (known_pages and ok):
        tried = set(seen)
        for path in ("/menu", "/shop", "/order-online"):
            if time.time() > t_end:
                break
            u = urllib.parse.urljoin(base, path)
            if u in tried:
                continue
            try:
                r = reader(u, ctx) or {}
            except Exception:
                r = {}
            if r.get("dbg"):
                dbg.append({"url": r.get("url") or u, **r["dbg"]})
            prods = page_products(r, r.get("url") or u, ctx)
            for e in (r.get("embeds") or [])[:2]:
                rr = reader(e, ctx) or {}
                prods += page_products(rr, rr.get("url") or e, ctx)
                deals += [{"text": t, "src": rr.get("url") or e} for t in rr.get("lines") or []]
                if rr.get("dbg"):
                    dbg.append({"url": e, **rr["dbg"]})
            if prods:
                products += prods
                useful.append(u)
                deals += [{"text": t, "src": r.get("url") or u} for t in r.get("lines") or []]
                break
    if known_pages and ok and not products and not deals:
        return read_site(website, reader, ctx)  # the site changed; discover its pages again
    uniq = list({d["text"].lower(): d for d in deals}.values())[:30]
    prods = list({(p["name"].lower(), p["price"]): p for p in products}.values())[:600]
    out = {"t": time.time(), "deals": uniq, "products": prods, "pages": useful[:9]}
    if not prods:
        out["dbg"] = dbg[:5]  # what the pages looked like, to fix this site
    return ok, out


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


def _leafly_page(state, city, page, reader):
    url = f"https://www.leafly.com/dispensaries/{slug(state)}/{slug(city)}" + (f"?page={page}" if page > 1 else "")
    j = next_data(url, reader)
    data = ((((j or {}).get("props") or {}).get("pageProps") or {}).get("storeLocatorResults") or {}).get("data") or {}
    return [x for v in data.values() if isinstance(v, list) for x in v if isinstance(x, dict) and x.get("slug")]


def leafly_stores(lat, lon, miles, area, reader=None):
    """Every storefront dispensary within `miles`, from Leafly's city listing (any state).
    The listing is sorted by 'Recommended', not distance, so read pages in batches and only stop once a whole
    batch adds nothing within range."""
    state, city = area.get("state"), area.get("city")
    if not state or not city:
        return []
    out, page = {}, 1
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        while page <= 16:
            batch = list(range(page, page + 4))
            results = list(pool.map(lambda pg: _leafly_page(state, city, pg, reader), batch))
            page += 4
            added = seen_any = 0
            for stores in results:
                seen_any += len(stores)
                for x in stores:
                    a = x.get("address") or {}
                    if a.get("lat") is None or miles_between(lat, lon, a["lat"], a["lon"]) > miles:
                        continue
                    marks = x.get("mapMarkers") or []
                    if marks and all((m.get("locationType") or "storefront") != "storefront" for m in marks):
                        continue  # delivery-only service, nowhere to walk in
                    if x["slug"] not in out:
                        added += 1
                    out[x["slug"]] = {
                        "id": "leafly/" + x["slug"], "name": x.get("name") or "Dispensary", "lat": a["lat"], "lon": a["lon"],
                        "address": " ".join(v for v in [a.get("address1"), a.get("city"), a.get("state")] if v), "city": a.get("city") or "", "state": a.get("state") or "",
                        "phone": x.get("phone") or "", "hours": _open_text(x), "leafly": x["slug"], "hasMenu": bool(x.get("hasMenuItems")),
                        "website": "", "tag": x.get("mainTag") or "",
                    }
            if not seen_any or (added == 0 and page > 5):
                break
    return list(out.values())


def osm_cannabis(lat, lon, miles):
    """Licensed cannabis shops on OpenStreetMap within range (a second source for stores Leafly doesn't list)."""
    r = round(miles * 1609.34)
    q = f'[out:json][timeout:40];(nwr["shop"="cannabis"](around:{r},{lat},{lon});nwr["healthcare"="dispensary"]["name"](around:{r},{lat},{lon}););out center tags;'
    body = urllib.parse.urlencode({"data": q}).encode()
    for ep in OVERPASS:
        try:
            els = json.loads(_get(ep, data=body, timeout=45, ua=APP_UA, limit=20_000_000,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})[2]).get("elements", [])
            out = []
            for e in els:
                d = _to_disp(e)
                if d["lat"] is None or not (e.get("tags") or {}).get("name"):
                    continue
                if re.search(r"\b(cbd|hemp|kratom|smoke|vape shop|tobacco|delta)\b", d["name"], re.I):
                    continue  # not a licensed dispensary
                d["id"] = "osm/" + d["id"]
                out.append(d)
            return out
        except Exception:
            continue
    return []


def _merge_stores(primary, extra):
    """Add stores from `extra` that aren't already in `primary` (same place = within ~0.15 mi or same name nearby)."""
    out = list(primary)
    for d in extra:
        n = norm_name = re.sub(r"[^a-z0-9]", "", d["name"].lower())[:8]
        dup = any(miles_between(d["lat"], d["lon"], p["lat"], p["lon"]) < 0.15 or
                  (miles_between(d["lat"], d["lon"], p["lat"], p["lon"]) < 1.0 and re.sub(r"[^a-z0-9]", "", p["name"].lower())[:8] == n)
                  for p in out)
        if not dup:
            out.append(d)
    return out


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
     "follow": lambda st: rf"/locations/{slug(st['city'])}/menu/menu/(flower|pre-rolls|vapes|edibles|concentrates|tinctures|oral|topicals|accessories|cbd|wellness|other)-\d+$", "more": 3},
    {"key": "trulieve", "rx": r"trulieve", "site": "https://www.trulieve.com",
     "pages": lambda st: [f"https://www.trulieve.com/category/{c}" for c in ("flower", "pre-rolls", "vapes", "concentrates", "edibles", "oral", "topicals", "accessories")],
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
        products += page_products(r, r["url"], ctx)
        if follow:
            pages += [l for l in (r.get("links") or []) + (r.get("allLinks") or []) if re.search(follow, l) and l not in seen][:10]
    uniq = list({d["text"].lower(): d for d in deals}.values())[:30]
    prods = list({(p["name"].lower(), p["price"]): p for p in products}.values())[:800]
    return ok, {"t": time.time(), "deals": uniq, "products": prods}


def leafly_website(store_slug, cache, reader=None):
    """The store's own website, from its Leafly page (cached 30 days)."""
    web = cache.setdefault("web", {})
    hit = web.get(store_slug)
    if hit and time.time() - hit["t"] < 30 * 86400:
        return hit["url"]
    j = next_data(f"https://www.leafly.com/dispensary-info/{store_slug}", reader)
    d = (((j or {}).get("props") or {}).get("pageProps") or {}).get("dispensary") or {}
    url = ""
    for k in ("website", "websiteUrl", "url", "menuUrl", "orderOnlineUrl", "onlineOrderingUrl"):
        v = d.get(k)
        if isinstance(v, str) and v.startswith("http") and "leafly." not in v:
            url = v
            break
    if not url:  # look anywhere in the store's data for an outside link that looks like its site
        for v in re.findall(r'"(https?://[^"\s]+)"', json.dumps(d))[:200]:
            if not re.search(r"leafly|imgix|cloudfront|google|facebook|instagram|twitter|x\.com|yelp|apple|amazonaws|\.(png|jpe?g|svg|webp)", v, re.I):
                url = v
                break
    if j is not None:
        web[store_slug] = {"t": time.time(), "url": url}
    return url


SKIP_SITES = r"leafly|weedmaps|yelp|facebook|instagram|twitter|x\.com|tiktok|google|bing|duckduckgo|mapquest|tripadvisor|indeed|glassdoor|linkedin|wikipedia|youtube|reddit|yellowpages|bbb\.org|foursquare|apple\.com|dutchie\.com/dispensary|iheartjane\.com/stores|allbud|cannabis\.net|where2getit|massroots|budbo|kaycha|patch\.com|news|\.gov"


def search_website(name, city, state):
    """Find a store's own website the way a person would: search the web for it and take the first real site."""
    q = urllib.parse.quote(f"{name} {city} {state} dispensary")
    found = []
    try:
        page = _get(f"https://html.duckduckgo.com/html/?q={q}", timeout=12)[2].decode("utf-8", "replace")
        for m in re.finditer(r'uddg=([^&"]+)', page):
            found.append(urllib.parse.unquote(m.group(1)))
    except Exception:
        pass
    if not found:
        try:
            page = _get(f"https://www.bing.com/search?q={q}", timeout=12)[2].decode("utf-8", "replace")
            found = re.findall(r'<li class="b_algo"[^>]*>.*?<a href="(https?://[^"]+)"', page, re.S)
        except Exception:
            pass
    words = [w for w in re.findall(r"[a-z]{3,}", name.lower()) if w not in ("dispensary", "cannabis", "the", "and", "medical", "adult", "use", "rec", "med", "company", "co")]
    for u in found[:8]:
        if re.search(SKIP_SITES, u, re.I):
            continue
        host = host_of(u)
        if words and not any(w[:5] in host for w in words) and len(found) > 1:
            continue  # first result isn't the store's own site
        return u
    return ""


def _search_links(query):
    """Result links from a web search (DuckDuckGo, then Bing)."""
    q = urllib.parse.quote(query)
    links = []
    try:
        page = _get(f"https://html.duckduckgo.com/html/?q={q}", timeout=12)[2].decode("utf-8", "replace")
        links = [urllib.parse.unquote(m.group(1)) for m in re.finditer(r'uddg=([^&"]+)', page)]
    except Exception:
        pass
    if not links:
        try:
            page = _get(f"https://www.bing.com/search?q={q}", timeout=12)[2].decode("utf-8", "replace")
            links = re.findall(r'<a[^>]+href="(https?://[^"]+)"', page)
        except Exception:
            pass
    return links


DUTCHIE_CATS = ("flower", "pre-rolls", "vaporizers", "concentrates", "edibles", "tinctures", "topicals", "accessories", "cbd")


def ordering_menu(store, area, cache):
    """Where the store actually sells online — its Dutchie, Jane or Weedmaps ordering store (cached 7 days).
    Returns a list of menu pages to read, best first."""
    plat = cache.setdefault("plat", {})
    key = store.get("leafly") or store["id"]
    hit = plat.get(key)
    if hit and time.time() - hit["t"] < 7 * 86400:
        return hit["pages"]
    name = re.sub(r"\s*[-–(].*$", "", store["name"]).strip()
    city = store.get("city") or area.get("city", "")
    words = [w for w in re.findall(r"[a-z]{3,}", name.lower()) if w not in ("dispensary", "cannabis", "the", "and", "company")]
    def fits(slug):
        return not words or any(w[:5] in slug.lower() for w in words)
    pages = []
    for q, rx in ((f"{name} {city} dutchie", r"https?://(?:www\.)?dutchie\.com/(stores|dispensary|embedded-menu)/([a-z0-9-]+)"),
                  (f"{name} {city} iheartjane", r"https?://(?:www\.)?iheartjane\.com/(?:embed/)?stores/(\d+)/([a-z0-9-]+)"),
                  (f"{name} {city} weedmaps menu", r"https?://(?:www\.)?weedmaps\.com/dispensaries/([a-z0-9-]+)")):
        for link in _search_links(q)[:12]:
            m = re.search(rx, link, re.I)
            if not m:
                continue
            if "dutchie" in rx and fits(m.group(2)):
                base = f"https://dutchie.com/{'stores' if m.group(1) != 'dispensary' else 'dispensary'}/{m.group(2)}"
                pages = [base + "/specials"] + [f"{base}/products/{c}" for c in DUTCHIE_CATS]
            elif "iheartjane" in rx and fits(m.group(2)):
                pages = [f"https://www.iheartjane.com/stores/{m.group(1)}/{m.group(2)}/menu"]
            elif "weedmaps" in rx and fits(m.group(1)):
                pages = [f"https://weedmaps.com/dispensaries/{m.group(1)}"]
            if pages:
                break
        if pages:
            break
    plat[key] = {"t": time.time(), "pages": pages}
    return pages


def jointcommerce_menu(store, reader, ctx):
    """The store's live menu on JointCommerce (public pages: /dispensaries/<store>/menu?page=N, sale items via ?on_sale=1).
    Covers chains whose own sites hide the menu behind store pickers (Curaleaf, Fluent…)."""
    s = re.sub(r"[^a-z0-9]+", "-", store["name"].lower().replace("ü", "u")).strip("-")
    base = f"https://www.jointcommerce.com/dispensaries/{s}/menu"
    c2 = {**ctx, "more": 0}
    prods, deals, seen, pages, t_end = [], [], set(), [], time.time() + 90
    for n in range(0, 16):
        if time.time() > t_end:
            break
        url = base + "?on_sale=1" if n == 0 else base + ("" if n == 1 else f"?page={n}")
        r = reader(url, c2) or {}
        got = page_products(r, r.get("url") or url, ctx)
        deals += [{"text": t, "src": url} for t in (r.get("lines") or [])]
        new = [p for p in got if (p["name"].lower(), p["price"]) not in seen]
        for p in new:
            seen.add((p["name"].lower(), p["price"]))
        prods += new
        if new:
            pages.append(url)
        elif n >= 1:
            break  # past the last page (or the store isn't on JointCommerce)
    return bool(prods), {"t": time.time(), "deals": deals[:40], "products": prods, "pages": pages,
                         "from": f"{store['name'].split(' - ')[0]} live menu (JointCommerce)"}


def read_menu_pages(pages, reader, ctx, label):
    rc = {"key": "menu", "pages": lambda st: pages, "follow": None, "more": 8}
    ok, data = read_recipe(rc, {"city": ctx.get("city", "")}, reader, ctx)
    data["from"] = label
    return ok, data


def find_website(store, area, cache, reader=None):
    """Store's own site: chain list → its Leafly page → web search. Cached 30 days."""
    web = cache.setdefault("web", {})
    key = store.get("leafly") or store["id"]
    hit = web.get("s:" + key)
    if hit and time.time() - hit["t"] < 30 * 86400:
        return hit["url"]
    url = next((u for rx, u in CHAIN_SITES if re.search(rx, store["name"], re.I)), "")
    if not url and store.get("leafly"):
        url = leafly_website(store["leafly"], cache, reader)
    if not url:
        url = search_website(re.sub(r"\s*[-–(].*$", "", store["name"]), store.get("city") or area.get("city", ""), area.get("state", ""))
    web["s:" + key] = {"t": time.time(), "url": url}
    return url


def _source(store):
    rc = recipe_for(store["name"])
    if rc:
        return rc["key"], rc
    if store.get("website"):
        h = host_of(store["website"])
        if h:
            return "site:" + h, None
    return ("leafly:" + store["leafly"], None) if store.get("leafly") else ("", None)


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
    key = f"L2:{lat:.2f},{lon:.2f},{int(miles)}"
    hit = cache["areas"].get(key)
    if hit and not force and time.time() - hit["t"] < 24 * 3600:
        return hit["d"], hit.get("area", {}), None
    area = area_of(lat, lon)
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(leafly_stores, lat, lon, miles, area, reader)
        f2 = pool.submit(osm_cannabis, lat, lon, miles)
        leafly, osm = f1.result(), f2.result()
    disps = _merge_stores(leafly, osm)
    note = None if disps else None
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
    key = f"L2:{lat:.2f},{lon:.2f},{int(miles)}"
    hit = cache["areas"].get(key)
    return {"dispensaries": _out(hit["d"], cache) if hit else [], "updated": None, "error": None}


def scan(lat, lon, miles, force=False, progress=None, reader=None, agree=False, on_site=None, only_host=None, workers=8, use_leafly=False, cancel=None):
    stopped = lambda: bool(cancel and cancel.is_set())
    with _lock:
        cache = _load()
    if progress:
        progress(0, 0, "stores")
    disps, area, note = _area_disps(lat, lon, miles, cache, False, reader)
    for d in disps:
        d["_dist"] = miles_between(lat, lon, d["lat"], d["lon"])
    if stopped():
        return {"dispensaries": [], "updated": None, "cancelled": True}
    if on_site and disps:
        on_site(_out(disps, cache))  # stores show up right away, before websites are looked up
    need = [d for d in disps if not d.get("website") and not recipe_for(d["name"])]
    if need and progress:
        progress(0, len(need), "websites")
    if need:  # find each store's own website so we read the real site
        with cf.ThreadPoolExecutor(max_workers=8) as pool:
            for d, url in zip(need, pool.map(lambda d: find_website(d, area, cache, None), need)):
                d["website"] = url
    jobs = {}  # source key -> (recipe, nearest store of that source)
    for d in sorted(disps, key=lambda x: x["_dist"]):
        k, rc = _source(d)
        if k and k not in jobs:
            jobs[k] = (rc, d)
    if only_host:
        todo = [k for k in jobs if k == only_host]
    else:
        def stale(k):
            e = cache["sites"].get(k, {})
            age = time.time() - e.get("t", 0)
            return age > SITE_TTL or (not e.get("products") and age > 10 * 60)  # empty results get retried soon
        todo = [k for k in jobs if force or stale(k)
                or (not use_leafly and "Leafly" in cache["sites"].get(k, {}).get("from", ""))]
    base_ctx = {**area, "agree": agree}

    def work(k):
        if stopped():
            return False, {"t": time.time(), "deals": [], "products": []}
        rc, st = jobs[k]
        # "pick your state" screens: the store's own state (a Seekonk MA store when searching from RI), else the searched area's
        ctx = {**base_ctx, "state": state_name(st.get("state") or base_ctx.get("state", ""))}
        if rc:
            ok, data = read_recipe(rc, st, reader, ctx)
            data["from"] = f"{re.sub(r'\s*[-–].*$', '', st['name'])} website ({st.get('city', '')} store)"
            if not data["products"] and reader:
                okj, dj = jointcommerce_menu(st, reader, ctx)
                if dj["products"]:
                    dj["deals"] = data["deals"] + dj["deals"]
                    return okj, dj
            if use_leafly and not data["products"] and st.get("leafly") and st.get("hasMenu"):
                ok2, d2 = leafly_menu(st["leafly"], reader)
                if d2["products"]:
                    d2["deals"] = data["deals"] + d2["deals"]
                    d2["from"] = "Leafly menu"
                    return ok2, d2
            return ok, data
        if k.startswith("leafly:"):
            pages = ordering_menu(st, area, cache) if reader else []
            if pages:
                ok, data = read_menu_pages(pages, reader, {**ctx, "city": st.get("city") or ""}, f"{st['name'].split(' - ')[0]} online ordering menu")
                if data["products"]:
                    return ok, data
            if reader:
                okj, dj = jointcommerce_menu(st, reader, ctx)
                if dj["products"]:
                    return okj, dj
            if not use_leafly:
                return True, {"t": time.time(), "deals": [], "products": [], "from": "couldn't find this store's website or online menu"}
            if not st.get("hasMenu"):
                return True, {"t": time.time(), "deals": [], "products": [], "from": "no online menu"}
            ok, data = leafly_menu(st["leafly"], reader)
            data["from"] = "Leafly menu"
            return ok, data
        ok, data = read_site(st.get("website"), reader, {**ctx, "city": st.get("city") or ctx.get("city", ""), "more": 4})
        data["from"] = "store website"
        if not data["products"] and reader:  # the website doesn't show the menu itself: read the store's online ordering menu
            pages = ordering_menu(st, area, cache)
            if pages:
                ok2, d2 = read_menu_pages(pages, reader, {**ctx, "city": st.get("city") or ""}, f"{st['name'].split(' - ')[0]} online ordering menu")
                if d2["products"]:
                    d2["deals"] = data["deals"] + d2["deals"]
                    return ok2, d2
        if not data["products"] and reader:
            okj, dj = jointcommerce_menu(st, reader, ctx)
            if dj["products"]:
                dj["deals"] = data["deals"] + dj["deals"]
                return okj, dj
        if use_leafly and not data["products"] and st.get("leafly") and st.get("hasMenu"):
            ok2, d2 = leafly_menu(st["leafly"], reader)
            if d2["products"]:
                d2["deals"] = data["deals"] + d2["deals"]
                d2["from"] = "Leafly menu (store website had no readable prices)"
                return ok2, d2
        return ok, data

    done = 0
    if progress:
        progress(0, len(todo))
    pool = cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo) or 1)))
    futs = {pool.submit(work, k): k for k in todo}
    finished = []
    try:
        def finished_futures():  # like as_completed, but notices Stop within half a second
            left = set(futs)
            while left and not stopped():
                fin, left = cf.wait(left, timeout=0.5, return_when=cf.FIRST_COMPLETED)
                yield from fin
        for f in finished_futures():
            if stopped():
                break
            k = futs[f]
            finished.append(k)
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
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if stopped():  # keep the stores already read, then hand control back right away
        with _lock:
            c2 = _load()
            c2.setdefault("web", {}).update(cache.get("web", {}))
            c2.setdefault("plat", {}).update(cache.get("plat", {}))
            c2["areas"].update(cache["areas"])
            c2["sites"].update({k: cache["sites"][k] for k in finished if k in cache["sites"]})
            c2.setdefault("hist", {}).update(cache.get("hist", {}))
            _save(c2)
        return {"dispensaries": [], "updated": None, "cancelled": True}
    with _lock:
        c2 = _load()
        c2.setdefault("web", {}).update(cache.get("web", {}))
        c2.setdefault("plat", {}).update(cache.get("plat", {}))
        c2["areas"].update(cache["areas"])
        c2["sites"].update({k: cache["sites"][k] for k in finished if k in cache["sites"]})
        c2.setdefault("hist", {}).update(cache.get("hist", {}))
        _save(c2)
    return {"dispensaries": _out(disps, cache), "updated": datetime.now(timezone.utc).isoformat(), "error": note, "sitesRead": len(jobs)}
