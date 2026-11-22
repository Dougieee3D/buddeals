# BudDeals
Windows desktop app: Python 3.12 + pywebview (WebView2) window around `ui/index.html`; PyInstaller onedir + Inno Setup; same release/auto-update pattern as LoLStat.
- `app.py` window + JS API (store, scan, sign-in window, read rewards, auto-update). Bump `APP_VERSION` for every release; the commit message becomes the release notes.
- `scan.py` v2: stores from Leafly's public listing (`leafly_stores`, page `__NEXT_DATA__` → storeLocatorResults; OSM only as fallback). Menus: per-chain `RECIPES` (MÜV per-store `/locations/<city>/menu/menu/...`, Trulieve `/category/{flower,vapes,concentrates,edibles}`, Sunnyside/Green Dragon generic crawl) read once per chain via the nearest store; otherwise the store's Leafly menu (`leafly_menu`). Cache key per source (`muv`, `trulieve`, `leafly:<slug>`). Cache: `%APPDATA%\BudDeals\cache.json`.
- `app.py` `BrowserReader` (hidden WebView2 windows): `GATE_JS` answers state/age/terms/store screens (age+terms only if `S.agree`), `EXTRACT_JS` finds the product grid and reads Currently/Formerly/struck prices, `LOAD_MORE_JS` (ctx `more`), `next_data()` for Next.js page data.
- `desktop.py` tray icon (pystray), Windows notifications, start-with-Windows (HKCU Run, `--tray` starts hidden), QR codes (qrcode). Closing the window hides to tray when `S.tray`.
- `ui/index.html` UI (calls `window.pywebview.api`): cache-first `api.cached()` then live `api.scan()` with `window.onSiteResult` per store; Settings tab state lives in `S` (store.json). `ui/js/dealparse.mjs` deal parser/ranking.
- UI tabs: Deals (grades A–D, compare, watch, used), Budget (best cart, stacking, QR), List (cheapest store / 2-store split + route), Spending (monthly + FL allotment log), Wallet, Stores (best days), Map, Add deal, Settings.
- Price history per product in cache.json `hist` (60 days) → `isLow`, `inflated`, `typical`.
- `.github/workflows/build.yml` builds `BudDeals-Setup.exe` on push to main → GitHub Release (tag v<version>).
- `installer.iss` per-user install to %LOCALAPPDATA%\Programs\BudDeals.
- Checks: `python -m py_compile app.py scan.py desktop.py`; parser: `node -e 'import("./ui/js/dealparse.mjs").then(p=>console.log(p.parseDeal("Spend $200 get 50% off")))'`
- User data (store.json, wallet, sign-in cookies) stays on the PC — never upload it.
