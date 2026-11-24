// Shared deal-text parser — used by the web app and the GitHub Action scraper.
export const CATS = ['flower', 'preroll', 'vape', 'concentrate', 'edible', 'tincture', 'topical', 'accessory'];
export const CAT_LABEL = { all: 'Everything', flower: 'Flower', preroll: 'Pre-rolls', vape: 'Vapes', concentrate: 'Concentrates', edible: 'Edibles', tincture: 'Tinctures/Oils', topical: 'Topicals', accessory: 'Accessories', other: 'Other' };

const CAT_RX = {
  flower: /\b(flower|eighths?|1\/8s?|quarters?|1\/4s?|half ?oz|ounces?|oz|smalls|shake|popcorn|buds?|whole flower|ground flower)\b/i,
  preroll: /\b(pre-?rolls?|joints?|blunts?)\b/i,
  vape: /\b(vapes?|carts?|cartridges?|pods?|disposables?|pens?|all[- ]in[- ]ones?|aio)\b/i,
  concentrate: /\b(concentrates?|wax|shatter|rosin|resin|badder|budder|batter|diamonds?|crumble|sugar|sauce|rso|hash|kief|dabs?|extracts?)\b/i,
  edible: /\b(edibles?|gumm(y|ies)|chocolates?|cand(y|ies)|beverages?|drinks?|mints?|lozenges?|chews?)\b/i,
  tincture: /\b(tinctures?|capsules?|tablets?|syringes?|sublingual|oral oils?)\b/i,
  topical: /\b(topicals?|lotions?|balms?|creams?|salves?|patch(es)?|transdermal)\b/i,
  accessory: /\b(accessor(y|ies)|glass|pipes?|bongs?|batter(y|ies)|papers?|grinders?|lighters?|merch|apparel|rigs?)\b/i,
};
const STOREWIDE = /\b(storewide|store-wide|sitewide|entire (store|menu|purchase|order)|everything|all products|whole order|your (order|purchase|cart)|all items)\b/i;
const DAYS = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'];
const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];

export function catsOf(text) {
  const cats = CATS.filter(c => CAT_RX[c].test(text));
  return cats.length && !STOREWIDE.test(text) ? cats : ['all'];
}

function endDate(l, now) {
  let m = l.match(/(?:thru|through|until|till|ends?|expires?|valid (?:through|until))\s*:?\s*(?:[a-z]+\.?,?\s)?(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?/);
  let mo, da, yr;
  if (m) { mo = +m[1] - 1; da = +m[2]; yr = m[3] ? (+m[3] < 100 ? 2000 + +m[3] : +m[3]) : null; }
  else if ((m = l.match(/(?:thru|through|until|till|ends?|expires?)\s*:?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(\d{1,2})/))) { mo = MONTHS.indexOf(m[1]); da = +m[2]; yr = null; }
  else return null;
  if (yr == null) { yr = now.getFullYear(); if (mo < now.getMonth() - 6) yr++; }
  return new Date(yr, mo, da, 23, 59, 59);
}

export function parseDeal(text, now = new Date()) {
  const t = String(text).replace(/\s+/g, ' ').trim();
  const l = t.toLowerCase();
  const d = { text: t, cats: catsOf(t), days: null, kind: null, pct: 0, threshold: 0, dollarsOff: 0, unitPrice: null, qty: null, upTo: false, newOnly: false, groups: [], expires: null, expired: false, score: 0, label: '' };

  const days = DAYS.map((n, i) => (new RegExp(`\\b${n}s?\\b`).test(l) ? i : -1)).filter(i => i >= 0);
  if (/\bweekends?\b/.test(l)) days.push(0, 6);
  if (days.length && !/\b(daily|every ?day|all week)\b/.test(l)) d.days = [...new Set(days)];
  d.newOnly = /\b(first[- ]time|new (patients?|customers?|members?)|first (visit|order|purchase))\b/.test(l);
  for (const [g, rx] of [['Veterans', /\b(veterans?|military)\b/], ['Seniors', /\bseniors?\b/], ['Industry', /\bindustry\b/], ['Students', /\bstudents?\b/], ['Birthday', /\bbirthday\b/], ['Members', /\b(members?|loyalty|rewards)\b/]]) if (rx.test(l)) d.groups.push(g);
  const end = endDate(l, now);
  if (end) { d.expires = `${end.getFullYear()}-${String(end.getMonth() + 1).padStart(2, '0')}-${String(end.getDate()).padStart(2, '0')}`; d.expired = end < now; }

  let m;
  const pctOk = p => p >= 5 && p < 90;
  if ((m = l.match(/spend\s*(?:over\s*|at least\s*)?\$\s?(\d+)(?:\s*(?:\+|or more))?[^.%$]{0,40}?(\d{1,2})\s*%/)) && pctOk(+m[2])) {
    Object.assign(d, { kind: 'tier', threshold: +m[1], pct: +m[2] / 100 });
  } else if ((m = l.match(/(\d{1,2})\s*%\s*off[^.]{0,40}?(?:orders?|purchases?|carts?|spend|spending)\s*(?:of\s*|over\s*|\$?\s*)?\$\s?(\d+)/)) && pctOk(+m[1])) {
    Object.assign(d, { kind: 'tier', threshold: +m[2], pct: +m[1] / 100 });
  } else if ((m = l.match(/spend\s*(?:over\s*)?\$\s?(\d+)[^.%]{0,40}?\$\s?(\d+)\s*off/))) {
    Object.assign(d, { kind: 'tier$', threshold: +m[1], dollarsOff: +m[2] });
  } else if ((m = l.match(/\$\s?(\d+)\s*off[^.]{0,40}?(?:orders?|purchases?|carts?)\s*(?:of\s*|over\s*)?\$\s?(\d+)/))) {
    Object.assign(d, { kind: 'tier$', threshold: +m[2], dollarsOff: +m[1] });
  } else if (/\b(bogo|b1g1|buy\s*(one|1)\s*,?\s*get\s*(one|1))\b/.test(l)) {
    const half = /(50\s*%|half)/.test(l);
    Object.assign(d, { kind: 'bogo', pct: half ? 0.25 : 0.5, half });
  } else if ((m = l.match(/buy\s*(\d+)\s*,?\s*get\s*(\d+)\s*(free|half|(\d{1,2})\s*%\s*off)?/))) {
    const x = +m[1], y = +m[2], frac = m[4] ? +m[4] / 100 : m[3] === 'half' ? 0.5 : 1;
    Object.assign(d, { kind: 'bxgy', qty: [x, y, frac], pct: (y * frac) / (x + y) });
  } else if ((m = l.match(/(up to\s*)?(\d{1,2})\s*%\s*off/)) && pctOk(+m[2])) {
    Object.assign(d, { kind: 'pct', pct: +m[2] / 100, upTo: !!m[1] });
  } else if ((m = l.match(/(?<![\/\d.])(\d{1,2})\s*(?:for|\/)\s*\$\s?(\d+(?:\.\d+)?)/)) && +m[1] > 1) {
    Object.assign(d, { kind: 'bundle', qty: +m[1], unitPrice: +m[2] / +m[1], total: +m[2] });
  } else if ((m = l.match(/\$\s?(\d+)\s*off/))) {
    Object.assign(d, { kind: 'dollars', dollarsOff: +m[1] });
  } else if ((m = l.match(/\$\s?(\d+(?:\.\d\d)?)\s*(eighths?|1\/8s?|quarters?|halves|half ounces?|ounces?|oz\b|grams?|carts?|cartridges?|disposables?|pre-?rolls?|gumm(?:y|ies)|vapes?)/))) {
    Object.assign(d, { kind: 'price', unitPrice: +m[1], unit: m[2] });
  } else if ((m = l.match(/(eighths?|1\/8s?|quarters?|1\/4s?|half ounces?|ounces?|oz|grams?|carts?|cartridges?|disposables?|pre-?rolls?|vapes?)\s*(?:for|@|at|only)\s*\$\s?(\d+(?:\.\d\d)?)/))) {
    Object.assign(d, { kind: 'price', unitPrice: +m[2], unit: m[1] });
  }

  switch (d.kind) {
    case 'tier': d.score = d.pct; d.label = `Spend $${d.threshold} → ${Math.round(d.pct * 100)}% off`; break;
    case 'tier$': d.score = d.dollarsOff / d.threshold; d.label = `Spend $${d.threshold} → $${d.dollarsOff} off`; break;
    case 'bogo': d.score = d.pct; d.label = d.half ? 'BOGO 50%' : 'BOGO FREE'; break;
    case 'bxgy': d.score = d.pct; d.label = `Buy ${d.qty[0]} Get ${d.qty[1]} ${d.qty[2] === 1 ? 'Free' : Math.round(d.qty[2] * 100) + '% off'}`; break;
    case 'pct': d.score = d.upTo ? d.pct * 0.6 : d.pct; d.label = `${d.upTo ? 'UP TO ' : ''}${Math.round(d.pct * 100)}% OFF`; break;
    case 'bundle': d.score = 0.2; d.label = `${d.qty} for $${d.total}`; break;
    case 'dollars': d.score = Math.min(0.3, d.dollarsOff / 100); d.label = `$${d.dollarsOff} OFF`; break;
    case 'price': d.score = 0.15; d.label = `$${d.unitPrice} ${d.unit}`; break;
    default: d.label = 'Deal';
  }
  return d;
}

// Quick filter for scraped lines — only lines with a concrete number-based deal.
export function isDealText(s) {
  return s.length >= 6 && s.length <= 240 && /(\d{1,2}\s*%\s*off|\bbogo\b|b1g1|buy\s*(\d+|one)\s*,?\s*get|\$\s?\d+\s*off|spend\s*(over\s*)?\$\s?\d+|(?<![\/\d.])\d{1,2}\s*(for|\/)\s*\$\s?\d+)/i.test(s);
}

export const appliesToday = (d, now = new Date()) => !d.days || d.days.includes(now.getDay());
export const matchesCat = (d, cat) => cat === 'all' || d.cats.includes('all') || d.cats.includes(cat);

// How much retail product `pay` dollars buys with this deal (assumes deal covers the whole cart, threshold is pre-discount).
export function retailFor(d, pay) {
  switch (d.kind) {
    case 'tier': { const r = pay / (1 - d.pct); return r >= d.threshold ? r : null; }
    case 'tier$': return pay + d.dollarsOff >= d.threshold ? pay + d.dollarsOff : null;
    case 'pct': case 'bogo': case 'bxgy': return pay / (1 - (d.upTo ? d.pct * 0.6 : d.pct));
    case 'dollars': return pay + d.dollarsOff;
    default: return null;
  }
}

// Pull points / reward dollars / personal offers out of a rewards page's visible text.
export function parseRewards(text) {
  const lines = String(text).split(/\n+/).map(s => s.replace(/\s+/g, ' ').trim()).filter(Boolean);
  let points = null, dollars = null;
  const offers = [];
  for (const s of lines) {
    const l = s.toLowerCase();
    let m;
    if (points == null && (m = l.match(/([\d,]{1,9})\s*(?:pts|points)\b/) || l.match(/points?\s*(?:balance|available)?\s*:?\s*([\d,]{1,9})\b/))) points = +m[1].replace(/,/g, '');
    if (dollars == null && (m = l.match(/\$\s?(\d+(?:\.\d\d)?)\s*(?:in\s*)?(?:rewards?|reward dollars|credit|available|to spend|off your next)/))) dollars = +m[1];
    if (isDealText(s) || /\b(birthday|reward|coupon|voucher|offer)\b/.test(l) && /(\d+\s*%|\$\s?\d+|free)/.test(l)) offers.push(s);
  }
  return { points, dollars, offers: [...new Set(offers)].slice(0, 12) };
}

// ---------- cart pricing: stack a store's deals the way its register would ----------
// items: [{price, was, qty, cat}]  deals: parsed store deals (already filtered to today / eligible)
// Item deals (category % off, BOGO, buy X get Y, N for $T) go on items not already on sale, unless the deal says it stacks.
// Then the best order deal (spend $X get Y% / $Z off) on the eligible subtotal. "Cannot be combined" deals are tried both ways.
const NOCOMBO = /(not|cannot|can'?t|can not)\s+be\s+combined|no stacking|not stackable|excludes?\s+(sale|discounted)|exclusions apply/i;
const STACKS = /stack|on top of|additional|extra \d|in addition/i;
export function priceCart(items, deals) {
  const units = [];
  items.forEach((x, i) => { for (let n = 0; n < (x.qty || 1); n++) units.push({ i, price: +x.price || 0, reg: +(x.was || x.price) || 0, sale: !!x.was, cat: x.cat || 'other' }); });
  const sub = units.reduce((s, u) => s + u.price, 0), reg = units.reduce((s, u) => s + u.reg, 0);
  const itemDeals = deals.filter(d => ['pct', 'bogo', 'bxgy', 'bundle'].includes(d.kind) && !(d.kind === 'pct' && d.upTo && d.cats.includes('all')));
  const orderDeals = deals.filter(d => d.kind === 'tier' || d.kind === 'tier$');
  const fits = (d, u) => matchesCat(d, u.cat) && (!u.sale || STACKS.test(d.text));
  // savings one item deal gives on the units it covers (each unit used once)
  function itemPass(pool) {
    const used = new Set(), lines = [];
    const options = itemDeals.map(d => {
      const us = pool.filter(u => fits(d, u)).sort((a, b) => b.price - a.price);
      let save = 0;
      if (d.kind === 'pct') save = us.reduce((s, u) => s + u.price * d.pct, 0);
      else if (d.kind === 'bogo') { for (let k = 1; k < us.length; k += 2) save += us[k].price * (d.half ? 0.5 : 1); }
      else if (d.kind === 'bxgy') { const [x, y, f] = d.qty; for (let k = 0; k + x + y <= us.length; k += x + y) for (let j = k + x; j < k + x + y; j++) save += us[j].price * f; }
      else if (d.kind === 'bundle') { for (let k = 0; k + d.qty <= us.length; k += d.qty) { const g = us.slice(k, k + d.qty).reduce((s, u) => s + u.price, 0); if (g > d.total) save += g - d.total; } }
      return { d, us, save };
    }).filter(o => o.save > 0.005).sort((a, b) => b.save - a.save);
    let save = 0;
    for (const o of options) {  // best deal first; a unit only gets one item deal
      if (o.us.some(u => used.has(u))) continue;
      o.us.forEach(u => used.add(u)); save += o.save; lines.push({ label: o.d.label || o.d.text, save: o.save, text: o.d.text });
    }
    return { save, lines };
  }
  function orderPass(pool, skipNoCombo) {
    let best = null, hint = null;
    for (const d of orderDeals) {
      if (skipNoCombo && NOCOMBO.test(d.text)) continue;
      const elig = pool.filter(u => matchesCat(d, u.cat));
      const spend = elig.reduce((s, u) => s + (u.now ?? u.price), 0);
      if (spend + 1e-9 >= d.threshold) {
        const save = d.kind === 'tier' ? spend * d.pct : Math.min(d.dollarsOff, spend);
        if (!best || save > best.save) best = { label: d.label || d.text, save, text: d.text };
      } else if (!hint || d.threshold - spend < hint.need) hint = { need: d.threshold - spend, label: d.label || d.text, cats: d.cats };
    }
    return { best, hint };
  }
  // A: item deals, then order deal on what's left to pay (skipping order deals that refuse to combine when items got a deal)
  const A = itemPass(units);
  const scale = sub ? (sub - A.save) / sub : 1;  // order deals count what you pay after item deals
  const oA = orderPass(units.map(u => ({ ...u, now: u.price * scale })), A.save > 0);
  const saveA = A.save + (oA.best ? oA.best.save : 0);
  // B: only the order deal, on full menu prices (for "cannot be combined" deals)
  const oB = orderPass(units, false);
  const saveB = oB.best ? oB.best.save : 0;
  const useB = saveB > saveA + 0.005;
  const lines = useB ? [oB.best] : [...A.lines, ...(oA.best ? [oA.best] : [])];
  const save = useB ? saveB : saveA;
  return { sub, reg, save, total: Math.max(0, sub - save), lines, hint: (useB ? oB : oA).hint, alone: useB };
}
