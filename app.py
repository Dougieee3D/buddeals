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

import desktop
import scan

APP_VERSION = "2.8.0"
REPO = "Dougieee3D/buddeals"
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
STORE_FILE = os.path.join(scan.DATA_DIR, "store.json")

# Runs inside the dispensary's page in the sign-in window. Keeps only point/reward/offer lines and drops
# anything that looks like an email or phone number. Nothing is sent anywhere; it's returned to this app only.
READY_JS = r"""(()=>{const t=(document.body&&document.body.innerText)||'';if(/(select|choose|pick) (your |a )?(state|location|store)|are you (over|at least|a)|age verification|verify your age/i.test(t))return -1;return [...document.querySelectorAll('body *')].filter(e=>e.children.length===0&&/^\$\s?\d/.test((e.textContent||'').trim())).length})()"""

READ_REWARDS_JS = r"""(()=>{const L=document.body.innerText.split(/\n+/).map(s=>s.trim()).filter(s=>s.length>2&&s.length<200&&/(point|pts|reward|\$|%|off|birthday|coupon|offer|free|credit|voucher)/i.test(s)&&!/@|\d{3}[-. )]+\d{3}[-. ]\d{4}/.test(s)).slice(0,60);return JSON.stringify({site:location.hostname.replace(/^www\./,'').split('.')[0],lines:L})})()"""


# Gets past "pick your state", "are you 21 / a patient" and "pick your store" screens like a person would.
# Cookie banners: only ever clicks a reject/decline option. Returns what it did ('' when nothing was in the way).
GATE_JS = r"""((STATE,ZIP,CITY,AGREE)=>{const T=s=>(s||'').replace(/\s+/g,' ').trim();const body=T(document.body.innerText).toLowerCase();
const vis=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};const click=e=>{try{e.click();return true}catch(x){return false}};
const cl=()=>[...document.querySelectorAll('button,a,[role=button],[role=option],input[type=submit]')].filter(vis);let did=[];
{const cb=cl();const dec=cb.find(b=>/^(reject|reject all|decline|deny|necessary only|only necessary|reject non-essential|decline all)$/.test(T(b.innerText).toLowerCase()));if(dec){click(dec)}else if(AGREE&&/cookie/.test(body)){const ok=cb.find(b=>/^(accept|accept all|accept cookies|ok|okay|got it|i understand|allow|allow all|close|dismiss|continue)$/.test(T(b.innerText||b.getAttribute('aria-label')).toLowerCase()));if(ok){click(ok);did.push('cookies')}}}
if(STATE&&/(select|choose|pick) (your |a )?state|which state/.test(body)){const st=STATE.toLowerCase();
 const sel=[...document.querySelectorAll('select')].find(x=>[...x.options].some(o=>T(o.text).toLowerCase()===st));
 if(sel){const o=[...sel.options].find(o=>T(o.text).toLowerCase()===st);if(sel.value!==o.value){sel.value=o.value;sel.dispatchEvent(new Event('change',{bubbles:true}));did.push('state')}}
 else{const opt=[...document.querySelectorAll('[role=option],li,button,a,span,div,p')].filter(vis).find(e=>e.children.length<3&&T(e.innerText).toLowerCase()===st);
  if(opt){click(opt);did.push('state')}else{const cb=[...document.querySelectorAll('[role=combobox],[aria-haspopup],select')].filter(vis)[0];if(cb){click(cb);did.push('open-state')}}}}
if(AGREE){for(const c of [...document.querySelectorAll('input[type=checkbox]')].filter(x=>!x.checked)){const l=T((c.labels&&c.labels[0]&&c.labels[0].innerText)||((c.closest('label')||c.parentElement||{}).innerText)||'').toLowerCase();if(/(18|21|age|agree|terms|patient)/.test(l)){click(c);did.push('check')}}}
if(AGREE&&/(21|18|age verification|verify your age|patient|over the age|of legal age|are you (over|at least|a)|adult[- ]use|recreational|medical)/.test(body)){const MED=/florida/i.test(STATE);const choose=cl().find(b=>{const t=T(b.innerText||b.value).toLowerCase();return t.length<40&&(MED?/^(medical|med|shop medical|medical menu|i'?m a (medical )?patient|i am a (medical )?patient|patient)$/:/^(adult[- ]use|recreational|rec|shop (adult[- ]use|recreational)|adult[- ]use menu|recreational menu|21\+|adult use 21\+)$/).test(t)});if(choose){click(choose);did.push('menu-type')}else{for(const b of cl()){const t=T(b.innerText||b.value).toLowerCase();if(t.length<45&&/^(yes|enter|enter site|i am 21|i'm 21|i am 18|i'm 18|i am over|i'm over|i am of (legal )?age|i'm of (legal )?age|i am a (medical )?patient|i'm a (medical )?patient|i have a medical card|i agree|agree|confirm|continue|yes,? i|verify|i'?m 21\+|21\+|over 21)/.test(t)){click(b);did.push('age');break}}}}
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
const TERPS=['myrcene','limonene','caryophyllene','pinene','linalool','terpinolene','humulene','ocimene','bisabolol','nerolidol','guaiol','terpineol','valencene','farnesene','eucalyptol','camphene'];
const XF=t=>({terp:((t.match(/TERP(?:S|ENES)?:?\s*(\d{1,2}(\.\d+)?)\s*%/i)||t.match(/(\d{1,2}(\.\d+)?)\s*%\s*terp/i)||[])[1]||''),cbd:((t.match(/CBD:?\s*(\d{1,2}(\.\d+)?)\s*%/i)||[])[1]||''),terps:TERPS.filter(x=>new RegExp('\\b'+x,'i').test(t))});
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
 const name=t.split(/\n+/).map(x=>T(x.replace(/\$\s?\d[\d,.]*/g,' '))).filter(s=>s.length>3&&s.length<140&&!/\$|add to (cart|bag)|^\d+\s*%|^(sale|new|qty|quantity|in stock|sold out)/i.test(s)).sort((a,b)=>b.length-a.length)[0]||'';
 if(!name)continue;const la=card.closest('a[href]')||card.querySelector('a[href]');items.push({...XF(t),url:la?la.href:'',left:((t.match(/only\s+(\d+)\s+left/i)||[])[1]||''),name,price,was:wasP&&wasP>price?wasP:null,tag:T((t.match(/\d{1,2}\s*%\s*off/i)||[''])[0]),size:T((t.match(/(?<![\d.])(\d*\.\d+|\d+)\s?(g|oz)\b|\b1\/8\b|\b1\/4\b/i)||[''])[0]),thc:((t.match(/(\d{1,2}(\.\d+)?)\s*%\s*(total\s*)?THC/i)||[])[1]||''),mg:((t.match(/(\d{2,4})\s*mg\b/i)||[])[1]||''),strain:((t.match(/\b(indica|sativa|hybrid)\b/i)||[])[1]||''),brand:(t.split(/\n+/).map(T).find(x=>x.length>1&&x.length<30&&x!==name&&!/\$|%|thc|terp|^\d|indica|sativa|hybrid|sale|new|\|/i.test(x))||'')});if(items.length>=400)break}

const META=/^(sale|new|indica|sativa|hybrid|cbd|thc|terp|only \d+ left|view options|add to (cart|bag)|currently|formerly|was|now|regular|as low as:?|each|limited|in stock|sold out|staff pick|best seller|popular|featured|\d+(\.\d+)?\s?(g|mg|oz|ml)|\d+\s*%\s*off|\$[\d.,]+\s*off|save .*|\|)$/i;
let grid=null,gn=0;for(const el of document.querySelectorAll('body *')){const k=el.children;if(k.length<4||k.length>600)continue;let n=0;for(const c of k){const t=c.innerText||'';if(t.length>12&&t.length<900&&/\$\s?\d/.test(t))n++}if(n>=4&&n/k.length>=0.5&&n>gn){grid=el;gn=n}}
const items2=[];
if(grid)for(const c of grid.children){const L=(c.innerText||'').split(/\n+/).map(T).filter(Boolean);if(L.length<2||!/\$\s?\d/.test(L.join(' ')))continue;
 let now=null,was=null,low=null,label='';const money=x=>parseFloat(x.replace(/[$,\s]/g,''));
 for(const l of L){const lab=l.match(/^(currently|now|sale price|formerly|was|regular price|reg\.?|original|as low as:?)\s*(\$\s?[\d.,]+)?$/i);
  if(lab){label=lab[1].toLowerCase();if(lab[2]){const v=money(lab[2]);if(/as low/.test(label))low=v;else if(/formerly|was|reg|original/.test(label))was=v;else now=v;label=''}continue}
  if(/^(\$\s?[\d.,]+\s*)+$/.test(l)){const vs=[...l.matchAll(/\$\s?([\d.,]+)/g)].map(m=>money(m[1]));for(const v of vs){if(/formerly|was|reg|original/.test(label))was=v;else if(/as low/.test(label))low=v;else if(now==null)now=v;else if(was==null&&v>now)was=v}label='';continue}
  const al=l.match(/as low as:?\s*\$\s?([\d.,]+)/i);if(al)low=money(al[0].replace(/as low as:?/i,''))}
 for(const e of c.querySelectorAll('*')){if(e.children.length||!/^\$\s?[\d.,]+$/.test(T(e.textContent)))continue;if(/^(S|DEL|STRIKE)$/.test(e.tagName)||getComputedStyle(e).textDecorationLine.includes('line-through')||(e.parentElement&&getComputedStyle(e.parentElement).textDecorationLine.includes('line-through'))){const v=parseFloat(T(e.textContent).replace(/[^\d.]/g,''));if(v>0){was=v;if(now===v)now=null}}}
 const allP=[...(c.innerText||'').matchAll(/\$\s?([\d.,]+)(?!\s*off)/gi)].map(m=>money(m[1])).filter(v=>v>0&&v!==was);if(now==null&&allP.length)now=Math.min(...allP);
 const price=now??low;if(!(price>0))continue;
 const brandLine=L.find(l=>/\bby\s+\S/i.test(l)&&l.length<60);const brand=brandLine?brandLine.replace(/^.*?\bby\s+/i,''):'';
 const cand=L.filter(l=>l.length>2&&l.length<100&&l!==brandLine&&!META.test(l)&&!/\$|%|:\s|^\d/.test(l));
 const name=cand.sort((a,b)=>b.length-a.length)[0]||'';if(!name)continue;
const pa=[...c.querySelectorAll('a[href]')].map(a=>a.href).find(h=>{try{const u=new URL(h);return u.hostname===location.hostname&&u.pathname.length>1&&h.split('#')[0]!==location.href&&!/cart|login|account|category\/?$|wishlist|\?page=/i.test(h)}catch(e){return false}})||'';
 const t=L.join('\n');items2.push({...XF(t),url:pa,left:((t.match(/only\s+(\d+)\s+left/i)||[])[1]||''),name,price,was:was&&was>price?was:null,tag:T((t.match(/\d{1,2}\s*%\s*off/i)||[''])[0]),size:T((t.match(/(?<![\d.])(\d*\.\d+|\d+)\s?(g|oz)\b|\b1\/8\b|\b1\/4\b/i)||[''])[0]),thc:((t.match(/THC:?\s*(\d{1,2}(\.\d+)?)\s*%/i)||t.match(/(\d{1,2}(\.\d+)?)\s*%\s*(total\s*)?THC/i)||[])[1]||''),mg:((t.match(/(\d{2,4})\s*mg\b/i)||[])[1]||''),strain:((t.match(/\b(indica|sativa|hybrid)\b/i)||[])[1]||''),brand:brand||(cand.length>1?cand.filter(x=>x!==name).sort((a,b)=>a.length-b.length)[0]:'')})}
const useItems=items2.length>=items.length?items2:items;
const host=location.hostname;const links=[...document.querySelectorAll('a[href]')].map(a=>({h:a.href.split('#')[0],t:T(a.innerText).toLowerCase()})).filter(x=>{try{return new URL(x.h).hostname===host&&x.h!==location.href}catch(e){return false}});
const pick=re=>links.filter(x=>re.test(x.h+' '+x.t)).map(x=>x.h);
const bad=/login|sign-?in|account|cart|checkout|blog|learn|news|career|about|contact|faq|privacy|terms|locat|dispensaries\/|wishlist|register/i;
const embeds=[...document.querySelectorAll('iframe[src]')].map(f=>f.src).filter(u=>/dutchie|iheartjane|\bjane\b|sweed|weedmaps|tymber|blaze|meadow|dispenseapp|treez|flowhub|joint|alpine|grassdoor|leafbuyer|olla/i.test(u)&&!/leafly/i.test(u)).slice(0,3);
const dbg=useItems.length?null:{title:document.title,text:T(document.body.innerText).slice(0,400),frames:[...document.querySelectorAll('iframe')].map(f=>f.src||'(no src)').slice(0,6),scripts:[...document.scripts].map(x=>x.src).filter(u=>/dutchie|jane|sweed|tymber|weedmaps|blaze|meadow|treez|flowhub|joint|alpine|olla|leafly/i.test(u)).slice(0,6),buttons:[...document.querySelectorAll('button,a[role=button]')].map(b=>T(b.innerText)).filter(Boolean).slice(0,15)};
return JSON.stringify({url:location.href,dbg,embeds,lines,items:useItems,allLinks:[...new Set(links.map(x=>x.h))].slice(0,300),links:[...new Set([...pick(/deal|special|promo|sale|discount/),...pick(/categor|flower|vape|vapor|concentrate|edible|pre-?roll|cartridge|shop|menu|products/)])].filter(h=>!bad.test(h)).slice(0,8)})})()"""


class BrowserReader:
    """A few hidden browser windows that load dispensary pages like Edge would, so JavaScript menus and sale prices show up."""

    def __init__(self, n=8):
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
            self._settle(w)
            args = json.dumps([ctx.get("state", ""), ctx.get("zip", ""), ctx.get("city", ""), bool(ctx.get("agree"))])[1:-1]
            last = None
            for _ in range(6):  # gates can come one after another (state -> age -> store)
                did = w.evaluate_js(f"{GATE_JS}({args})")
                if not did or did == last:
                    break
                last = did
                time.sleep(1.5)
                self._settle(w)
            cur = w.get_current_url() or ""
            if cur and cur.rstrip("/") != url.rstrip("/") and "deals" not in url:
                ev.clear()
                w.load_url(url)  # the store picker may have sent us elsewhere; come back to the page we wanted
                ev.wait(25)
                self._settle(w)
            for _ in range(int(ctx.get("more", 6))):
                if not w.evaluate_js(LOAD_MORE_JS):
                    time.sleep(0.8)
                    if not w.evaluate_js(LOAD_MORE_JS):
                        break
                time.sleep(1.2)
            return json.loads(w.evaluate_js(EXTRACT_JS) or "{}")
        except Exception:
            return {}
        finally:
            self.pool.put((w, ev))

    def next_data(self, url):
        w, ev = self.pool.get()
        try:
            ev.clear()
            w.load_url(url)
            ev.wait(25)
            txt = w.evaluate_js("(document.getElementById('__NEXT_DATA__')||{}).textContent||''")
            return json.loads(txt) if txt else None
        except Exception:
            return None
        finally:
            self.pool.put((w, ev))

    @staticmethod
    def _settle(w, limit=7.0):
        """Wait until prices appear and stop changing (or a gate shows) instead of a fixed pause."""
        end, last = time.time() + limit, None
        time.sleep(0.6)
        while time.time() < end:
            try:
                n = w.evaluate_js(READY_JS)
            except Exception:
                n = None
            if n == -1 or (n and n == last):
                return
            last = n
            time.sleep(0.6)

    def close(self):
        for w in self.wins:
            try:
                w.destroy()
            except Exception:
                pass


def _ulog(msg):
    try:
        os.makedirs(scan.DATA_DIR, exist_ok=True)
        with open(os.path.join(scan.DATA_DIR, "update.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def _vt(v):
    return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())


class Api:
    def __init__(self):
        self._lock = threading.Lock()
        self._login = None
        self._release = None
        self._tray_on = True
        self._quitting = False
        self._told_tray = False

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

    def cached(self, lat, lon, miles):
        try:
            return scan.cached(float(lat), float(lon), float(miles))
        except Exception as e:
            return {"dispensaries": [], "updated": None, "error": f"{e}"}

    def scan(self, lat, lon, miles, force=False, agree=False, only_host=None, workers=8, leafly=False):
        def progress(done, total):
            try:
                main.evaluate_js(f"window.onScanProgress && window.onScanProgress({done},{total})")
            except Exception:
                pass
        def on_site(disps):
            try:
                main.evaluate_js(f"window.onSiteResult && window.onSiteResult({json.dumps(disps)})")
            except Exception:
                pass
        reader = None
        try:
            reader = BrowserReader(1 if only_host else max(1, min(int(workers or 8), 12)))
        except Exception:
            reader = None
        try:
            return scan.scan(float(lat), float(lon), float(miles), bool(force), progress, reader, bool(agree), on_site, only_host,
                             int(workers or 8), bool(leafly))
        except Exception as e:
            return {"dispensaries": [], "updated": None, "error": f"Scan failed: {e}"}
        finally:
            if reader:
                reader.close()

    # ----- tray, notifications, start with Windows, QR -----
    def notify(self, title, message):
        return tray.notify(title, message) if tray else False

    def set_tray(self, on):
        self._tray_on = bool(on)
        return True

    def get_startup(self):
        return desktop.get_startup()

    def set_startup(self, on):
        return desktop.set_startup(bool(on))

    def qr(self, text):
        try:
            return desktop.qr_data_url(text)
        except Exception:
            return None

    def quit_app(self):
        self._quitting = True
        if tray:
            tray.stop()
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:
                pass
        return True

    def clear_cache(self):
        try:
            os.remove(scan.CACHE_FILE)
        except Exception:
            pass
        return True

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
                    _ulog("checksum mismatch — not installing")
                    return {"error": "The download didn’t match its checksum, so it wasn’t installed."}
            with open(path, "wb") as f:
                f.write(data)
            inst_log = os.path.join(scan.DATA_DIR, "installer.log")
            args = [path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS",
                    f"/LOG={inst_log}"]
            flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP: the installer outlives the app
            try:
                proc = subprocess.Popen(args, creationflags=flags | 0x01000000, close_fds=True, cwd=tempfile.gettempdir())
            except OSError:
                proc = subprocess.Popen(args, creationflags=flags, close_fds=True, cwd=tempfile.gettempdir())
            _ulog(f"started installer for {r['version']} (pid {proc.pid}); BudDeals is closing so it can be replaced")

            def _bye():
                time.sleep(0.8)
                self.quit_app()
                time.sleep(1.0)
                os._exit(0)  # nothing (tray, hidden windows) may keep the old version running
            threading.Thread(target=_bye, daemon=True).start()
            return {"ok": True}
        except Exception as e:
            _ulog(f"update failed: {e!r}")
            return {"error": f"Update failed: {e}"}


api = Api()
START_HIDDEN = "--tray" in sys.argv
main = webview.create_window("BudDeals", url=os.path.join(BASE, "ui", "index.html"), js_api=api, hidden=START_HIDDEN,
                             width=1400, height=900, min_size=(420, 600), background_color="#f3f5f2", maximized=True)


def _open():
    try:
        main.show()
        main.restore()
    except Exception:
        pass


def _check():
    try:
        main.evaluate_js("window.checkNow && window.checkNow()")
    except Exception:
        pass


tray = desktop.Tray(_open, _check, api.quit_app) if desktop.pystray else None


def _closing():
    """Closing the window keeps BudDeals in the tray (if that's on) so alerts keep working."""
    if tray and api._tray_on and not api._quitting:
        main.hide()
        if not api._told_tray:
            api._told_tray = True
            tray.notify("BudDeals is still running", "Watching deals in the tray. Right-click the 🌿 icon to quit.")
        return False
    if tray:
        tray.stop()
    return True


main.events.closing += _closing

if __name__ == "__main__":
    os.makedirs(scan.DATA_DIR, exist_ok=True)
    _ulog(f"BudDeals {APP_VERSION} started ({sys.executable})")
    if os.name == "nt" and getattr(sys, "frozen", False):
        # Only one BudDeals at a time: close any older copy (e.g. one left in the tray) so it can't keep stale code running.
        try:
            r = subprocess.run(["taskkill", "/F", "/FI", "IMAGENAME eq BudDeals.exe", "/FI", f"PID ne {os.getpid()}"],
                               capture_output=True, text=True, timeout=10, creationflags=0x08000000)
            if "SUCCESS" in (r.stdout or ""):
                _ulog("closed an older BudDeals that was still running")
        except Exception:
            pass
    # New version? Clear the window's cached copy of the old screens (settings, cart and sign-ins are kept).
    vfile = os.path.join(scan.DATA_DIR, "last_version.txt")
    try:
        last = open(vfile, encoding="utf-8").read().strip()
    except Exception:
        last = ""
    if last != APP_VERSION:
        import shutil
        prof = os.path.join(scan.DATA_DIR, "webview", "EBWebView", "Default")
        for sub in ("Cache", "Code Cache", "GPUCache", os.path.join("Service Worker", "CacheStorage"), os.path.join("Service Worker", "ScriptCache")):
            shutil.rmtree(os.path.join(prof, sub), ignore_errors=True)
        try:
            with open(vfile, "w", encoding="utf-8") as f:
                f.write(APP_VERSION)
        except Exception:
            pass
        _ulog(f"new version ({last or 'first run'} -> {APP_VERSION}): cleared cached screens")
    webview.start(lambda: tray and tray.start(), private_mode=False, storage_path=os.path.join(scan.DATA_DIR, "webview"))
