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

APP_VERSION = "1.2.0"
REPO = "Dougieee3D/buddeals"
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
STORE_FILE = os.path.join(scan.DATA_DIR, "store.json")

# Runs inside the dispensary's page in the sign-in window. Keeps only point/reward/offer lines and drops
# anything that looks like an email or phone number. Nothing is sent anywhere; it's returned to this app only.
READ_REWARDS_JS = r"""(()=>{const L=document.body.innerText.split(/\n+/).map(s=>s.trim()).filter(s=>s.length>2&&s.length<200&&/(point|pts|reward|\$|%|off|birthday|coupon|offer|free|credit|voucher)/i.test(s)&&!/@|\d{3}[-. )]+\d{3}[-. ]\d{4}/.test(s)).slice(0,60);return JSON.stringify({site:location.hostname.replace(/^www\./,'').split('.')[0],lines:L})})()"""


# Gets past "pick your state", "are you 21 / a patient" and "pick your store" screens like a person would.
# Cookie banners: only ever clicks a reject/decline option. Returns what it did ('' when nothing was in the way).
GATE_JS = r"""((STATE,ZIP,CITY,AGREE)=>{const T=s=>(s||'').replace(/\s+/g,' ').trim();const body=T(document.body.innerText).toLowerCase();
const vis=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};const click=e=>{try{e.click();return true}catch(x){return false}};
const cl=()=>[...document.querySelectorAll('button,a,[role=button],[role=option],input[type=submit]')].filter(vis);let did=[];
for(const b of cl()){if(/^(reject|reject all|decline|deny|necessary only|only necessary|reject non-essential)$/.test(T(b.innerText).toLowerCase())){click(b);break}}
if(STATE&&/(select|choose|pick) (your |a )?state|which state/.test(body)){const st=STATE.toLowerCase();
 const sel=[...document.querySelectorAll('select')].find(x=>[...x.options].some(o=>T(o.text).toLowerCase()===st));
 if(sel){const o=[...sel.options].find(o=>T(o.text).toLowerCase()===st);if(sel.value!==o.value){sel.value=o.value;sel.dispatchEvent(new Event('change',{bubbles:true}));did.push('state')}}
 else{const opt=[...document.querySelectorAll('[role=option],li,button,a,span,div,p')].filter(vis).find(e=>e.children.length<3&&T(e.innerText).toLowerCase()===st);
  if(opt){click(opt);did.push('state')}else{const cb=[...document.querySelectorAll('[role=combobox],[aria-haspopup],select')].filter(vis)[0];if(cb){click(cb);did.push('open-state')}}}}
if(AGREE){for(const c of [...document.querySelectorAll('input[type=checkbox]')].filter(x=>!x.checked)){const l=T((c.labels&&c.labels[0]&&c.labels[0].innerText)||((c.closest('label')||c.parentElement||{}).innerText)||'').toLowerCase();if(/(18|21|age|agree|terms|patient)/.test(l)){click(c);did.push('check')}}}
if(AGREE&&/(21|18|age verification|verify your age|patient|over the age|of legal age|are you (over|at least|a))/.test(body)){for(const b of cl()){const t=T(b.innerText||b.value).toLowerCase();if(t.length<45&&/^(yes|enter|i am 21|i'm 21|i am 18|i'm 18|i am over|i'm over|i am a (medical )?patient|i'm a (medical )?patient|i agree|agree|confirm|yes,? i)/.test(t)){click(b);did.push('age');break}}}
if(/(select|choose|find|pick) (a |your )?(location|store|dispensary)|shop here|select (a )?store|set (as )?(my )?store/.test(body)){
 const re=/^(shop here|select store|select this store|select|choose|choose store|choose this store|set (as )?(my )?store|shop this (store|location)|start shopping|shop now|order here|make this my store)$/;
 const btns=cl().filter(b=>re.test(T(b.innerText).toLowerCase()));let pick=null;
 const cardOf=b=>{let c=b;while(c.parentElement&&btns.filter(x=>c.parentElement.contains(x)).length<2)c=c.parentElement;return c};
 if(CITY){const c0=CITY.toLowerCase();pick=btns.find(b=>T(cardOf(b).innerText).toLowerCase().includes(c0))||null}
 if(!pick){const inp=[...document.querySelectorAll('input')].filter(vis).find(i=>/zip|city|address|location|postal/i.test((i.placeholder||'')+' '+(i.name||'')+' '+(i.getAttribute('aria-label')||'')));
  if(inp&&ZIP&&inp.value!==ZIP){const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;set.call(inp,ZIP);inp.dispatchEvent(new Event('input',{bubbles:true}));
   inp.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,bubbles:true}));const sb=inp.parentElement&&inp.parentElement.querySelector('button');if(sb)click(sb);did.push('store-search')}
  else if(btns.length)pick=btns[0]}
 if(pick){click(pick);did.push('store')}}
return did.join(',')})"""

LOAD_MORE_JS = r"""(()=>{const b=[...document.querySelectorAll('button,a,[role=button]')].find(e=>{const r=e.getBoundingClientRect();return r.width>0&&/^(load|show|view|see) more( products| items)?$/i.test((e.innerText||'').trim())});if(b){b.scrollIntoView();b.click();return true}window.scrollTo(0,document.body.scrollHeight);return false})()"""

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
 if(!name)continue;items.push({name,price,was:wasP&&wasP>price?wasP:null,tag:T((t.match(/\d{1,2}\s*%\s*off/i)||[''])[0]),size:T((t.match(/\b\d+(\.\d+)?\s?(g|oz)\b|\b1\/8\b|\b1\/4\b/i)||[''])[0])});if(items.length>=400)break}
const host=location.hostname;const links=[...document.querySelectorAll('a[href]')].map(a=>({h:a.href.split('#')[0],t:T(a.innerText).toLowerCase()})).filter(x=>{try{return new URL(x.h).hostname===host&&x.h!==location.href}catch(e){return false}});
const pick=re=>links.filter(x=>re.test(x.h+' '+x.t)).map(x=>x.h);
const bad=/login|sign-?in|account|cart|checkout|blog|learn|news|career|about|contact|faq|privacy|terms|locat|dispensaries\/|wishlist|register/i;
return JSON.stringify({url:location.href,lines,items,links:[...new Set([...pick(/deal|special|promo|sale|discount/),...pick(/categor|flower|vape|vapor|concentrate|edible|pre-?roll|cartridge|shop|menu|products/)])].filter(h=>!bad.test(h)).slice(0,8)})})()"""


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

    def __call__(self, url, ctx=None):
        ctx = ctx or {}
        w, ev = self.pool.get()
        try:
            ev.clear()
            w.load_url(url)
            ev.wait(25)
            time.sleep(3)
            args = json.dumps([ctx.get("state", ""), ctx.get("zip", ""), ctx.get("city", ""), bool(ctx.get("agree"))])[1:-1]
            last = None
            for _ in range(6):  # gates can come one after another (state -> age -> store)
                did = w.evaluate_js(f"{GATE_JS}({args})")
                if not did or did == last:
                    break
                last = did
                time.sleep(3)
            cur = w.get_current_url() or ""
            if cur and cur.rstrip("/") != url.rstrip("/") and "deals" not in url:
                ev.clear()
                w.load_url(url)  # the store picker may have sent us elsewhere; come back to the page we wanted
                ev.wait(25)
                time.sleep(3)
            for _ in range(8):
                if not w.evaluate_js(LOAD_MORE_JS):
                    time.sleep(0.8)
                    if not w.evaluate_js(LOAD_MORE_JS):
                        break
                time.sleep(2)
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

    def scan(self, lat, lon, miles, force=False, agree=False):
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
            return scan.scan(float(lat), float(lon), float(miles), bool(force), progress, reader, bool(agree))
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
