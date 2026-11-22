// Runs in GitHub Actions (Node 22, no dependencies). Finds dispensaries per region via OpenStreetMap,
// reads their public websites for deal text + JSON-LD product prices, and writes data/deals.json.
import fs from 'node:fs/promises';
import { isDealText, catsOf } from '../js/dealparse.mjs';

const UA = 'BudDealsBot/1.0 (personal deal finder; GitHub Pages)';
const MAX_SITES = 500, CONCURRENCY = 6, TIMEOUT = 15000;
const OVERPASS = ['https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter'];
const NAME_RX = 'dispensar|cannabis|marijuana|weed|Trulieve|Curaleaf|Sunnyside|Liberty Health|Fluent|M[UÜ]V|Surterra|Vidacann|Insa|RISE|Zen Leaf|AYR|GrowHealthy|Jungle Boys|Sanctuary|Flowery|Green Dragon|Cookies|Planet 13|Thrive|Cannabist|Beyond Hello|Verilife|Story Cannabis|Gold Flora';

const regions = JSON.parse(await fs.readFile('regions.json', 'utf8'));
let old = {};
try { for (const d of JSON.parse(await fs.readFile('data/deals.json', 'utf8')).dispensaries) old[d.id] = d; } catch {}

export function overpassQuery(lat, lon, miles) {
  const r = Math.round(miles * 1609.34);
  return `[out:json][timeout:60];(nwr["shop"="cannabis"](around:${r},${lat},${lon});nwr["name"~"${NAME_RX}",i]["shop"](around:${r},${lat},${lon});nwr["name"~"${NAME_RX}",i]["healthcare"](around:${r},${lat},${lon});nwr["name"~"${NAME_RX}",i]["amenity"](around:${r},${lat},${lon}););out center tags;`;
}

async function get(url, opts = {}) {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), TIMEOUT);
  try { return await fetch(url, { ...opts, signal: ctl.signal, headers: { 'user-agent': UA, ...(opts.headers || {}) }, redirect: 'follow' }); }
  finally { clearTimeout(t); }
}

async function overpass(lat, lon, miles) {
  for (const ep of OVERPASS) {
    try {
      const res = await get(ep, { method: 'POST', body: 'data=' + encodeURIComponent(overpassQuery(lat, lon, miles)), headers: { 'content-type': 'application/x-www-form-urlencoded' } });
      if (res.ok) return (await res.json()).elements;
    } catch (e) { console.warn('overpass fail', ep, e.message); }
  }
  return [];
}

const toDisp = e => {
  const t = e.tags || {};
  return {
    id: `${e.type}/${e.id}`, name: t.name || t.brand || 'Dispensary', lat: e.lat ?? e.center?.lat, lon: e.lon ?? e.center?.lon,
    address: [t['addr:housenumber'], t['addr:street'], t['addr:city'], t['addr:state']].filter(Boolean).join(' '),
    website: t.website || t['contact:website'] || t.url || '', phone: t.phone || t['contact:phone'] || '', hours: t.opening_hours || '',
  };
};

const decode = s => s.replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&#36;|&dollar;/g, '$').replace(/&#37;|&percnt;/g, '%').replace(/&quot;/g, '"').replace(/&#0?39;|&apos;|&rsquo;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#(\d+);/g, (_, n) => String.fromCharCode(+n));

function extract(html, base) {
  const products = [];
  for (const m of html.matchAll(/<script[^>]*application\/ld\+json[^>]*>([\s\S]*?)<\/script>/gi)) {
    try { walk(JSON.parse(m[1]), products); } catch {}
  }
  const links = [];
  for (const m of html.matchAll(/<a\b[^>]*href=["']([^"'#]+)["'][^>]*>([\s\S]{0,200}?)<\/a>/gi)) {
    const label = m[2].replace(/<[^>]+>/g, ' ');
    if (/deal|special|promo|discount|sale|offer|daily/i.test(m[1] + ' ' + label)) {
      try { const u = new URL(m[1], base); if (u.hostname === new URL(base).hostname) links.push(u.href); } catch {}
    }
  }
  const text = decode(html.replace(/<(script|style|svg|head)[\s\S]*?<\/\1>/gi, ' ')
    .replace(/<\/(p|div|li|h\d|tr|section|article|button)>|<br\s*\/?>/gi, '\n').replace(/<[^>]+>/g, ' '));
  const deals = [];
  for (const line of text.split(/\n|(?<=[.!])\s+(?=[A-Z])/)) {
    const s = line.replace(/\s+/g, ' ').trim();
    if (isDealText(s)) deals.push(s);
  }
  return { deals, links: [...new Set(links)].slice(0, 3), products };
}

function walk(o, out) {
  if (!o || typeof o !== 'object' || out.length > 80) return;
  if (Array.isArray(o)) return o.forEach(x => walk(x, out));
  const type = [].concat(o['@type'] || []).join(',');
  if (/Product/.test(type) && o.name) {
    const off = [].concat(o.offers || [])[0] || {};
    const price = +(off.price ?? off.lowPrice);
    if (price > 0) out.push({ name: String(o.name).slice(0, 120), price, cats: catsOf(o.name + ' ' + (o.category || '')) });
  }
  for (const v of Object.values(o)) if (v && typeof v === 'object') walk(v, out);
}

async function scan(d) {
  if (!d.website) return { ...d, deals: [], products: [], checked: null };
  const base = /^https?:/i.test(d.website) ? d.website : 'https://' + d.website;
  const seen = new Set(), deals = [], products = [];
  const pages = [base];
  let ok = false;
  for (let i = 0; i < pages.length && i < 5; i++) {
    const url = pages[i]; if (seen.has(url)) continue; seen.add(url);
    try {
      const res = await get(url);
      if (!res.ok || !/html/i.test(res.headers.get('content-type') || '')) continue;
      ok = true;
      const r = extract((await res.text()).slice(0, 3_000_000), res.url);
      for (const s of r.deals) deals.push({ text: s, src: res.url });
      products.push(...r.products);
      if (i === 0) {
        pages.push(...r.links);
        if (!r.links.length) pages.push(new URL('/deals', base).href, new URL('/specials', base).href);
      }
    } catch {}
  }
  const uniq = [...new Map(deals.map(x => [x.text.toLowerCase(), x])).values()].slice(0, 20);
  if (!ok && old[d.id]?.deals?.length) return { ...old[d.id], ...d, stale: true };
  return { ...d, deals: uniq, products: products.slice(0, 60), checked: new Date().toISOString() };
}

const all = new Map();
for (const r of regions.regions) {
  const els = await overpass(r.lat, r.lon, r.radiusMiles);
  console.log(`${r.name}: ${els.length} places`);
  for (const e of els) { const d = toDisp(e); if (d.lat) all.set(d.id, d); }
  await new Promise(s => setTimeout(s, 2000));
}
for (const s of regions.extraSites || []) all.set('site:' + s.website, { id: 'site:' + s.website, address: '', phone: '', hours: '', ...s });

const list = [...all.values()].slice(0, MAX_SITES);
const out = [];
let idx = 0;
await Promise.all(Array.from({ length: CONCURRENCY }, async () => {
  while (idx < list.length) { const d = list[idx++]; out.push(await scan(d)); }
}));
out.sort((a, b) => a.name.localeCompare(b.name));
await fs.mkdir('data', { recursive: true });
await fs.writeFile('data/deals.json', JSON.stringify({ updated: new Date().toISOString(), regions: regions.regions, dispensaries: out }));
console.log(`Saved ${out.length} dispensaries, ${out.reduce((n, d) => n + d.deals.length, 0)} deals.`);
