# BudDeals
Windows desktop app: Python 3.12 + pywebview (WebView2) window around `ui/index.html`; PyInstaller onedir + Inno Setup; same release/auto-update pattern as LoLStat.
- `app.py` window + JS API (store, scan, sign-in window, read rewards, auto-update). Bump `APP_VERSION` for every release; the commit message becomes the release notes.
- `scan.py` OpenStreetMap (Overpass, two-step shop→cannabis filter) + reads dispensary sites (hidden WebView2 windows via `BrowserReader` in app.py) for deal lines + product prices/sale prices; sites cached per host. `GATE_JS` answers state/age/terms/store-picker screens (age+terms only when the user allowed it: `S.agree`), `LOAD_MORE_JS` expands menus, category pages are crawled (up to 9 pages/site). Cache: `%APPDATA%\BudDeals\cache.json`.
- `ui/index.html` UI (calls `window.pywebview.api`), `ui/js/dealparse.mjs` deal parser/ranking.
- `.github/workflows/build.yml` builds `BudDeals-Setup.exe` on push to main → GitHub Release (tag v<version>).
- `installer.iss` per-user install to %LOCALAPPDATA%\Programs\BudDeals.
- Checks: `python -m py_compile app.py scan.py`; parser: `node -e 'import("./ui/js/dealparse.mjs").then(p=>console.log(p.parseDeal("Spend $200 get 50% off")))'`
- User data (store.json, wallet, sign-in cookies) stays on the PC — never upload it.
