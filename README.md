# 🌿 BudDeals
Free dispensary deal finder: any ZIP/city/GPS, 5–150 mi radius, ranked by best deal per category, budget planner, private rewards wallet, map.

## Put it online (free, ~5 min)
1. Create a **public** GitHub repo (free Pages needs public), e.g. `buddeals`, and upload everything in this folder (keep `.github/`).
2. Repo **Settings → Pages → Source: GitHub Actions**.
3. **Actions** tab → "Scan deals & deploy" → **Run workflow**. Your site: `https://YOURNAME.github.io/buddeals/`
4. It re-scans every 6 hours automatically. The footer has a "Scan for new deals now" link.

## Add areas to scan
Edit `regions.json` on GitHub (name, lat, lon, radiusMiles). Committing it triggers a scan. Add any dispensary site that's missing from OpenStreetMap under `extraSites`:
`{ "name": "Store", "website": "https://...", "lat": 28.6, "lon": -80.8 }`

## How it works
- `index.html` – the whole app. Dispensary locations come live from OpenStreetMap; deals come from `data/deals.json`.
- `scraper/scrape.mjs` – run by GitHub Actions. Reads each dispensary's public site + its deals/specials pages, keeps lines with real deals (% off, BOGO, spend $X get Y, 3 for $100…), plus product prices when the site publishes them.
- `js/dealparse.mjs` – shared deal parser (used by both).
- Wallet, favorites and deals you add yourself live only in your browser (localStorage). Use Export to back them up.
