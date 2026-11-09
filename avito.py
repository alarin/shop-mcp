"""Avito: search and item details, read-only."""
import json
import re
import time
import urllib.parse

import pics
from cdp import tab_for
from shoplog import dump, log

SORT = {"default": None, "date": "104", "price": "1", "price_desc": "2"}

LIST_JS = r"""
(() => {
  const blocked = /Доступ ограничен|проблема с IP|captcha/i.test(document.title + ' ' + (document.body?.innerText || '').slice(0, 600));
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const items = [...document.querySelectorAll('[data-marker="item"]')].map(el => {
    const a = el.querySelector('a[itemprop="url"], a[data-marker="item-title"]');
    const price = el.querySelector('[itemprop="price"]');
    return {
      id: el.getAttribute('data-item-id'),
      title: t(el.querySelector('[itemprop="name"]')?.textContent || a?.textContent),
      // Redesigned cards (since ~2026-09-28) have no itemprop price: take the first "N ₽" of the price block,
      // the crossed-out old price and the bonus line come after it.
      price: price?.getAttribute('content')
        || (t(el.querySelector('[class*="priceStep"]')?.textContent).match(/\d[\d ]*(?= ?₽)/) || [''])[0].replace(/\D/g, '')
        || t(el.querySelector('[data-marker="item-price"]')?.textContent),
      date: t(el.querySelector('[data-marker="item-date"]')?.textContent),
      city: a ? new URL(a.href).pathname.split('/')[1] : '',
      delivery: [...el.querySelectorAll('p, span')].map(e => t(e.textContent)).find(s => /^Доставка/.test(s)) || '',
      rating: t(el.querySelector('[data-marker="seller-info/score"]')?.textContent),
      url: a ? a.href.split('?')[0] : '',
    };
  });
  const total = t(document.querySelector('[data-marker="page-title/count"]')?.textContent);
  const u = new URL(location.href); u.searchParams.delete('context'); u.searchParams.delete('localPriority');
  return {blocked, total, url: decodeURI(u.toString()), items};
})()
"""

ITEM_JS = r"""
(() => {
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const q = s => t(document.querySelector(s)?.textContent);
  const params = [...document.querySelectorAll('[data-marker="item-view/item-params"] li')].map(li => t(li.textContent));
  return {
    title: q('[data-marker="item-view/title-info"]') || q('h1'),
    price: document.querySelector('[itemprop="price"]')?.getAttribute('content') || q('[data-marker="item-view/item-price"]'),
    address: q('[itemprop="address"]') || q('[data-marker="delivery/location"]'),
    date: q('[data-marker="item-view/item-date"]'),
    seller: q('[data-marker="seller-info/name"]'),
    seller_rating: q('[data-marker="seller-info/score"]'),
    params,
    description: t(document.querySelector('[data-marker="item-view/item-description"]')?.innerText).slice(0, 2500),
  };
})()
"""


SHIP_JS = r"""
(async (paths) => {
  // Sequential with a pause: a burst of parallel fetches gets HTTP 439 "Доступ ограничен".
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const num = s => +s.replace(/\D/g, '');
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const out = [];
  for (const [i, path] of paths.entries()) {
    if (i) await sleep(1200 + Math.random() * 800);
    let r;
    try {
      const resp = await fetch(path, {credentials: 'include'});
      const h = await resp.text();
      const d = new DOMParser().parseFromString(h, 'text/html');
      if (resp.status === 439 || /Доступ ограничен/.test(d.title)) { out.push({blocked: true}); break; }
      // Reserved by another buyer: the Buy button reads "Товар зарезервирован", page state has isReserved:true.
      const reserved = /isReserved[\\"]*:true/.test(h) || /Товар зарезервирован/.test(h);
      // Avito swaps some Cyrillic letters in descriptions for Latin look-alikes ("нe cмог"); map them back.
      const lat = {a: 'а', c: 'с', e: 'е', o: 'о', p: 'р', x: 'х', y: 'у', k: 'к', m: 'м', t: 'т', h: 'н', b: 'в'};
      const desc = t(d.querySelector('[data-marker="item-view/item-description"]')?.textContent).toLowerCase().replace(/[aceopxykmthb]/g, c => lat[c]);
      const broken = /не работа|не запуска|не включа|не грузит|не загружа|на запчаст|как есть|неисправ/i.test(desc);
      const p = [...d.querySelectorAll('p')].find(e => /^Доставка (в|по) /.test(t(e.textContent)));
      if (!p) { out.push({reserved, broken}); continue; }
      const text = t(p.textContent);
      const full = p.querySelector('del');
      const disc = p.querySelector('[data-marker="delivery-item-condition-discount"]');
      const prices = (text.match(/\d[\d ]*(?= ?₽)/g) || []).map(num);
      r = {text, reserved, broken, full: full ? num(full.textContent) : (prices[0] ?? null), wallet: disc ? num(disc.textContent) : null};
    } catch (e) { r = {error: String(e)}; }
    out.push(r);
  }
  return out;
})
"""
SHIP_MAX = 15  # items per search whose delivery price we look up; ~2.5 s each


def _shipping(tab, paths):
    """Delivery offer for each item path, read from the item HTML without navigating.

    Shorter than paths if Avito started its security check: the rest were not asked.
    """
    return tab.js(f"{SHIP_JS}({json.dumps(paths)})", timeout=15 + 4 * len(paths)) if paths else []


def _ship_cell(s):
    """('3127 кошельком / 7919', 3127) — the cell text and the cheapest delivery price."""
    if s and s.get("blocked"):
        return "проверка Авито", None
    if not s or s.get("error") or s.get("full") is None:
        return "", None
    cheapest = min(x for x in (s["full"], s["wallet"]) if x is not None)
    cell = f"{s['wallet']} кошельком / {s['full']}" if s["wallet"] is not None else str(s["full"])
    return cell, cheapest


def search_url(query, region, price_min, price_max, sort, page, delivery):
    qs = {"q": query}
    if price_min:
        qs["pmin"] = int(price_min)
    if price_max:
        qs["pmax"] = int(price_max)
    if SORT.get(sort):
        qs["s"] = SORT[sort]
    if page > 1:
        qs["p"] = page
    if delivery:
        qs["d"] = 1
    return f"https://www.avito.ru/{region}?" + urllib.parse.urlencode(qs)


def search(query, region="sankt-peterburg", price_min=None, price_max=None,
           sort="default", page=1, limit=30, delivery=False, shipping=SHIP_MAX):
    url = search_url(query, region, price_min, price_max, sort, page, delivery)
    tab = tab_for("avito.ru")
    try:
        tab.goto(url, "avito", wait_js="document.querySelector('[data-marker=\"item\"], [data-marker=\"page-title/count\"]')")
        r = tab.js(LIST_JS)
        if r["blocked"]:
            saved = dump("avito", "block", tab.js("document.documentElement.outerHTML"))
            log.warning("avito search %s: block or captcha, page %s", r["url"], saved)
        items = r["items"][:limit]
        ship = [None] * len(items)
        note = ""
        if shipping and not r["blocked"]:
            idx = [i for i, x in enumerate(items) if x["delivery"] and x["url"]][:shipping]
            got = _shipping(tab, [urllib.parse.urlparse(items[i]["url"]).path for i in idx])
            for i, s in zip(idx, got):
                ship[i] = s
            if got and (got[-1] or {}).get("blocked"):
                log.warning("avito shipping: 439 after %d of %d items, search %s", len(got) - 1, len(idx), r["url"])
                note = "\n⚠️ Авито включил проверку безопасности — цену доставки узнали не для всех; подождать 10–30 минут."
            elif len([x for x in items if x["delivery"]]) > len(idx):
                note = f"\nцена доставки — только у первых {len(idx)} объявлений с доставкой (shipping={shipping})"
    finally:
        tab.close()
    if r["blocked"]:
        return f"Авито показал блокировку или капчу: {r['url']}. Нужно пройти её через Screen Sharing на макмини."
    # Reserved items can't be bought; only those whose page we fetched for shipping are checked.
    reserved = [x for x, s in zip(items, ship) if s and s.get("reserved")]
    rows = []
    for i, (x, s) in enumerate((x, s) for x, s in zip(items, ship) if not (s and s.get("reserved"))):
        cell, cheapest = _ship_cell(s)
        price = int(x["price"]) if str(x["price"]).isdigit() else None
        total = price + cheapest if price is not None and cheapest is not None else ""
        rows.append(f"{i+1}\t{x['price']}\t{cell}\t{total}\t{x['title']}\t{x['city']}\t{x['delivery']}\t{x['rating']}\t{x['date']}\t{x['url'].replace('https://www.avito.ru', '')}")
    head = (f"{r['total'] or len(r['items'])} найдено · {r['url']}\n"
            "ссылки относительно https://www.avito.ru; доставка ₽ — в город из профиля Авито; итого = цена + самая дешёвая доставка\n"
            "#\tцена\tдоставка ₽\tитого\tназвание\tгород\tдоставка\tрейтинг продавца\tкогда\tссылка")
    if reserved:
        note += f"\nскрыто зарезервированных: {len(reserved)} ({', '.join(x['url'].rsplit('_', 1)[-1] for x in reserved)})"
    return head + "\n" + ("\n".join(rows) if rows else "(пусто)") + note


def _reveal_address(tab):
    """Address is hidden behind the "Узнать подробности" map button; click it and diff page text."""
    before = set(tab.js("document.body.innerText").splitlines())
    if not tab.click('[data-marker="item-map-button"]'):
        return "", ""
    new = []
    for _ in range(8):
        time.sleep(0.6)
        new = [l.strip() for l in tab.js("document.body.innerText").splitlines()
               if l.strip() and l not in before and "Яндекс" not in l]
        if any(not re.search(r"до \d+ мин", l) for l in new):
            break
    metro = [re.sub(r"(до \d+ мин)\.?", r" (\1)", l) for l in new if re.search(r"до \d+ мин", l)]
    addr = [l for l in new if not re.search(r"до \d+ мин", l)]
    return (addr[-1] if addr else ""), ", ".join(metro)


def item(url):
    tab = tab_for("avito.ru")
    try:
        tab.goto(url, "avito", wait_js="document.querySelector('h1')")
        r = tab.js(ITEM_JS)
        if not r["title"]:
            saved = dump("avito", "item", tab.js("document.documentElement.outerHTML"))
            log.warning("avito item %s: no title, page %s", url, saved)
        r["ship"] = (tab.js(f"{SHIP_JS}([location.pathname])") or [None])[0]
        if not r["address"]:
            r["address"], r["metro"] = _reveal_address(tab)
    finally:
        tab.close()
    price = str(r["price"]).replace("₽", "").strip()
    lines = [f"{r['title']} — {price} ₽"]
    if (r.get("ship") or {}).get("reserved"):
        lines.append("⛔ ЗАРЕЗЕРВИРОВАН другим покупателем — сейчас купить нельзя")
    lines += [f"{k}: {v}" for k, v in (("адрес", r["address"]), ("метро", r.get("metro")), ("дата", r["date"].lstrip("· ")),
              ("продавец", f"{r['seller']} {r['seller_rating']}".strip())) if v]
    s = r.get("ship")
    if s and s.get("text"):
        _, cheapest = _ship_cell(s)
        base = int(price.replace(" ", "").replace("\u00a0", "")) if price.replace(" ", "").replace("\u00a0", "").isdigit() else None
        lines.append(f"доставка: {s['text']}" + (f" → итого от {base + cheapest} ₽" if base is not None and cheapest is not None else ""))
    if r["params"]:
        lines.append("параметры: " + "; ".join(r["params"]))
    lines.append("\n" + r["description"])
    return "\n".join(lines)


IMAGES_JS = r"""
(() => {
  const blocked = /Доступ ограничен|проблема с IP|captcha/i.test(
    document.title + ' ' + (document.body?.innerText || '').slice(0, 600));
  const loaders = window.__staticRouterHydrationData?.loaderData || {};
  const item = Object.values(loaders).map(x => x?.buyerItem?.item).find(Boolean);
  const best = sizes => sizes['1280x960'] || sizes['640x480'] ||
    Object.entries(sizes).filter(([k, v]) => /^\d+x\d+$/.test(k) && typeof v === 'string')
      .sort(([a], [b]) => parseInt(b) - parseInt(a))[0]?.[1];
  let urls = (item?.imageUrls || []).map(best).filter(Boolean);
  // Older layouts: stay inside the main gallery, never collect recommendations or avatars.
  if (!urls.length) urls = [...document.querySelectorAll('[data-marker="item-view/main-gallery"] img')]
    .map(e => e.currentSrc || e.src).filter(Boolean);
  return {blocked, title: item?.title || document.querySelector('h1')?.textContent || '',
          url: location.href.split('?')[0], urls: [...new Set(urls)]};
})()
"""


def images(url, limit=6):
    """Download the listing gallery as MCP images, in the seller's order."""
    if not 1 <= limit <= 20:
        raise ValueError("limit должен быть от 1 до 20")
    url = urllib.parse.urljoin("https://www.avito.ru/", url)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ("avito.ru", "www.avito.ru", "m.avito.ru"):
        raise ValueError("Нужна ссылка на объявление https://www.avito.ru/…")
    tab = tab_for("avito.ru")
    try:
        tab.goto(url, "avito", wait_js="document.querySelector('h1')")
        r = tab.js(IMAGES_JS)
    finally:
        tab.close()
    if r["blocked"]:
        return [f"Авито показал блокировку или капчу: {url}. Нужно пройти её через Screen Sharing на макмини."]
    gallery = [(f"#{i}", u) for i, u in enumerate(r["urls"], 1)]
    head = f"{r['title']}\n{r['url']}\nфотографий: {len(gallery)}; показываю {min(limit, len(gallery))}"
    if not gallery:
        return [head + "\nФотографии не найдены: объявление недоступно, без фото или изменился формат галереи."]
    return pics.fetch(head, gallery, limit)
