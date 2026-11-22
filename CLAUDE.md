# BudDeals
Static GitHub Pages app, no build step, no npm deps.
- `index.html` UI (ES module) · `js/dealparse.mjs` shared parser · `scraper/scrape.mjs` Node 22 scraper → `data/deals.json`
- `.github/workflows/scan.yml` scans every 6h (+ on regions.json commits) and deploys Pages (Source: GitHub Actions)
- Test parser: `node -e 'import("./js/dealparse.mjs").then(p=>console.log(p.parseDeal("Spend $200 get 50% off")))'`
- Run scraper locally: `node scraper/scrape.mjs`
- User data (wallet/favs/manual deals) is localStorage only — never upload it.
