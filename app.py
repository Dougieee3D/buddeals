"""BudDeals desktop app: a pywebview window around ui/index.html, with scanning, sign-in and auto-update in Python."""
import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import webview

import scan

APP_VERSION = "1.1.0"
REPO = "Dougieee3D/buddeals"
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
STORE_FILE = os.path.join(scan.DATA_DIR, "store.json")

# Runs inside the dispensary's page in the sign-in window. Keeps only point/reward/offer lines and drops
# anything that looks like an email or phone number. Nothing is sent anywhere; it's returned to this app only.
READ_REWARDS_JS = r"""(()=>{const L=document.body.innerText.split(/\n+/).map(s=>s.trim()).filter(s=>s.length>2&&s.length<200&&/(point|pts|reward|\$|%|off|birthday|coupon|offer|free|credit|voucher)/i.test(s)&&!/@|\d{3}[-. )]+\d{3}[-. ]\d{4}/.test(s)).slice(0,60);return JSON.stringify({site:location.hostname.replace(/^www\./,'').split('.')[0],lines:L})})()"""


# Clicks "Yes / I'm 21 / I'm a patient" age gates so the menu shows.
AGE_JS = r"""(()=>{if(!/(21|age verification|verify your age|patient|over the age|of legal age)/i.test(document.body.innerText))return 0;let n=0;for(const b of document.querySelectorAll('button,a,[role=button],input[type=submit]')){const t=(b.innerText||b.value||'').replace(/\s+/g,' ').trim().toLowerCase();if(t.length<45&&/^(yes|enter|i am 21|i'm 21|i am over|i'm over|i am a (medical )?patient|i'm a (medical )?patient|i agree|agree|confirm)/.test(t)){try{b.click();n++}catch(e){}}if(n>2)break}return n})()"""

# Reads deal lines, product cards (price + crossed-out price) and menu/deal links from the rendered page.
EXTRACT_JS = r"""(()=>{const T=s=>(s||'').replace(/\s+/g,' ').trim();
const DEAL=/(\d{1,2}\s*%\s*off|\bbogo\b|b1g1|buy\s*(\d+|one)\s*,?\s*get|\$\s?\d+\s*off|spend\s*(over\s*)?\$\s?\d+|(?<![\/\d.])\d{1,2}\s*(for|\/)\s*\$\s?\d+)/i;
const lines=[...new Set((document.body.innerText||'').split(/\n+/).map(T).filter(s=>s.length>=6&&s.length<=240&&DEAL.test(s)&&!/^\d{1,2}\s*%\s*off\s*\$?[\d.]*$/i.test(s)))].slice(0,40);
const P=/^\$\s?\d{1,4}(\.\d{1,2})?$/;
const isPrice=e=>e.children.length===0&&P.test(T(e.textContent));
const struck=e=>{for(let x=e,i=0;x&&i<3;x=x.parentElement,i++){if(/^(S|DEL|STRIKE)$/.test(x.tagName)||getComputedStyle(x).textDecorationLine.includes('line-through'))return true}return false};
const seen=new Set(),items=[];
for(const leaf of [...document.querySelectorAll('body *')].filter(isPrice).slice(0,1500)){
 let card=leaf;for(let i=0;i<6&&card.parentElement;i++){card=card.parentElement;const t=T(card.innerText);if(t.length>25&&/[a-z]{3}/i.test(t.replace(/\$[\d.]+/g,'')))break}
 if(seen.has(card))continue;seen.add(card);const t=card.innerText||'';if(t.length>600)continue;
 const vals=[...card.querySelectorAll('*')].filter(isPrice).map(e=>({v:parseFloat(T(e.textContent).replace(/[$\s]/g,'')),s:struck(e)})).filter(x=>x.v>0);
 if(!vals.length)continue;const now=vals.filter(x=>!x.s).map(x=>x.v),was=vals.filter(x=>x.s).map(x=>x.v);
 const price=now.length?Math.min(...now):Math.min(...vals.map(x=>x.v));const wasP=was.length?Math.max(...was):null;
 const name=t.split(/\n+/).map(T).filter(s=>s.length>3&&s.length<140&&!/\$|add to (cart|bag)|^\d+\s*%|^(sale|new|qty|quantity|in stock|sold out)/i.test(s)).sort((a,b)=>b.length-a.length)[0]||'';
 if(!name)continue;items.push({name,price,was:wasP&&wasP>price?wasP:null,tag:T((t.match(/\d{1,2}\s*%\s*off/i)||[''])[0])});if(items.length>=150)break}
const host=location.hostname;const links=[...document.querySelectorAll('a[href]')].map(a=>({h:a.href.split('#')[0],t:T(a.innerText).toLowerCase()})).filter(x=>{try{return new URL(x.h).hostname===host&&x.h!==location.href}catch(e){return false}});
const pick=re=>links.filter(x=>re.test(x.h+' '+x.t)).map(x=>x.h);
return JSON.stringify({url:location.href,lines,items,links:[...new Set([...pick(/deal|special|promo|sale|discount/),...pick(/shop|menu|order|products/)])].slice(0,4)})})()"""


class BrowserReader:
    """A few hidden browser windows that load dispensary pages like Edge would, so JavaScript menus and sale prices show up."""

    def __init__(self, n=3):
        self.pool = queue.Queue()
        self.wins = []
        for i in range(n):
            ev = threading.Event()
            w = webview.create_window(f"BudDeals scanner {i + 1}", html="<html></html>", hidden=True, width=1280, height=900)
            w.events.loaded += ev.set
            self.wins.append(w)
            self.pool.put((w, ev))

    def __call__(self, url):
        w, ev = self.pool.get()
        try:
            ev.clear()
            w.load_url(url)
            ev.wait(25)
            time.sleep(3)
            if w.evaluate_js(AGE_JS):
                time.sleep(3)
            for _ in range(5):
                w.evaluate_js("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.8)
            return json.loads(w.evaluate_js(EXTRACT_JS) or "{}")
        except Exception:
            return {}
        finally:
            self.pool.put((w, ev))

    def close(self):
        for w in self.wins:
            try:
                w.destroy()
            except Exception:
                pass


def _vt(v):
    return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())


class Api:
    def __init__(self):
        self._lock = threading.Lock()
        self._login = None
        self._release = None

    # ----- settings, favorites, wallet (saved only on this PC) -----
    def load_store(self):
        try:
            with open(STORE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save(self, key, value):
        with self._lock:
            d = self.load_store()
            d[key] = value
            os.makedirs(scan.DATA_DIR, exist_ok=True)
            tmp = STORE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, STORE_FILE)
        return True

    # ----- location + scanning -----
    def geocode(self, q):
        try:
            return scan.geocode(q)
        except Exception:
            return None

    def reverse(self, lat, lon):
        return scan.reverse(lat, lon)

    def locate_me(self):
        return scan.locate_me()

    def scan(self, lat, lon, miles, force=False):
        def progress(done, total):
            try:
                main.evaluate_js(f"window.onScanProgress && window.onScanProgress({done},{total})")
            except Exception:
                pass
        reader = None
        try:
            reader = BrowserReader()
        except Exception:
            reader = None
        try:
            return scan.scan(float(lat), float(lon), float(miles), bool(force), progress, reader)
        except Exception as e:
            return {"dispensaries": [], "updated": None, "error": f"Scan failed: {e}"}
        finally:
            if reader:
                reader.close()

    # ----- rewards sign-in (you type your login into the dispensary's own page; the app never sees it) -----
    def open_login(self, url):
        url = url.strip()
        if not url:
            return False
        if not url.lower().startswith("http"):
            url = "https://" + url
        if self._login:
            try:
                self._login.load_url(url)
                self._login.show()
                return True
            except Exception:
                self._login = None
        self._login = webview.create_window("Sign in – BudDeals", url, width=1100, height=820)
        self._login.events.closed += lambda: setattr(self, "_login", None)
        return True

    def read_rewards(self):
        if not self._login:
            return {"error": "Click “Open & sign in” first, then go to your rewards/points page."}
        try:
            return json.loads(self._login.evaluate_js(READ_REWARDS_JS))
        except Exception as e:
            return {"error": f"Couldn’t read that page ({e})."}

    def clear_logins(self):
        try:
            main.clear_cookies()
            return True
        except Exception:
            return False

    # ----- updates (same idea as LoLStat: check GitHub Releases on open) -----
    def version(self):
        return APP_VERSION

    def check_update(self):
        try:
            req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest", headers={"User-Agent": scan.APP_UA})
            rel = json.loads(urllib.request.urlopen(req, timeout=15).read())
            assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets") or []}
            latest = rel.get("tag_name", "")
            if "BudDeals-Setup.exe" in assets and _vt(latest) > _vt(APP_VERSION):
                self._release = {"version": latest.lstrip("v"), "setup": assets["BudDeals-Setup.exe"], "sums": assets.get("SHA256SUMS.txt"),
                                 "notes": (rel.get("body") or "").split("\n---")[0].strip()}
                return {k: self._release[k] for k in ("version", "notes")}
        except Exception:
            pass
        return None

    def install_update(self):
        r = self._release
        if not r:
            return {"error": "No update found."}
        try:
            path = os.path.join(tempfile.gettempdir(), "BudDeals-Setup.exe")
            data = urllib.request.urlopen(urllib.request.Request(r["setup"], headers={"User-Agent": scan.APP_UA}), timeout=120).read()
            if r.get("sums"):
                sums = urllib.request.urlopen(urllib.request.Request(r["sums"], headers={"User-Agent": scan.APP_UA}), timeout=30).read().decode()
                want = sums.split()[0].lower()
                if hashlib.sha256(data).hexdigest() != want:
                    return {"error": "The download didn’t match its checksum, so it wasn’t installed."}
            with open(path, "wb") as f:
                f.write(data)
            subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"], close_fds=True)
            threading.Timer(1.0, lambda: [w.destroy() for w in list(webview.windows)]).start()
            return {"ok": True}
        except Exception as e:
            return {"error": f"Update failed: {e}"}


api = Api()
main = webview.create_window("BudDeals", url=os.path.join(BASE, "ui", "index.html"), js_api=api,
                             width=1200, height=860, min_size=(420, 600), background_color="#0f1712")

if __name__ == "__main__":
    os.makedirs(scan.DATA_DIR, exist_ok=True)
    webview.start(private_mode=False, storage_path=os.path.join(scan.DATA_DIR, "webview"))
