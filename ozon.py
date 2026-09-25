"""Ozon: search and cart via the site's own JSON API, called from the logged-in tab.

No navigation per request: fetch() runs inside an ozon.ru page, so cookies and
anti-bot tokens are the browser's own. Stops at the cart, never checks out.
"""
import json
import re
import time
import urllib.parse

from cdp import tab_for
from shoplog import dump, log

SORTS = ("score", "new", "price", "price_desc", "rating", "discount")
API = "/api/entrypoint-api.bx/page/json/v2?url="
_last_call = 0.0
DATE_RE = re.compile(r"Сегодня|Завтра|Послезавтра|\d+\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)|\d+\s*(?:час|дн)", re.I)


def _tab():
    tab = tab_for("ozon.ru")
    if "ozon.ru" not in (tab.js("location.hostname") or ""):
        tab.goto("https://www.ozon.ru/", "ozon", wait_js="document.readyState === 'complete'")
    return tab


def _fetch(tab, path, method="GET", body=None, retried=False):
    global _last_call
    gap = 1.5 - (time.time() - _last_call)
    if gap > 0:
        time.sleep(gap)
    _last_call = time.time()
    opts = {"method": method, "credentials": "include"}
    if body is not None:
        opts["headers"] = {"Content-Type": "application/json"}
        opts["body"] = json.dumps(body)
    r = tab.js(f"fetch({json.dumps(path)}, {json.dumps(opts)}).then(async r => ({{s: r.status, b: await r.text()}}))")
    try:
        return json.loads(r["b"])
    except ValueError:
        pass
    saved = dump("ozon", r["s"], r["b"])
    if retried:
        log.error("ozon %s %s: %s again after reload, body %s", method, path[:150], r["s"], saved)
        raise RuntimeError(f"Ozon ответил {r['s']} не-JSON и после перезагрузки вкладки "
                           f"(вероятно, проверка на бота). Ответ целиком: макмини {saved}")
    # A tab left idle for hours loses its anti-bot cookie; fetch() cannot pass the JS check,
    # a real page load can. Seen 2026-09-25 after ~20 h idle.
    log.warning("ozon %s %s: %s non-JSON, body %s; reloading tab and retrying", method, path[:150], r["s"], saved)
    tab.goto("https://www.ozon.ru/", "ozon", wait_js="document.readyState === 'complete'")
    time.sleep(3)
    return _fetch(tab, path, method, body, retried=True)


def _page(tab, url):
    return _fetch(tab, API + urllib.parse.quote(url, safe=""))


def _widgets(page, prefix):
    return [json.loads(v) for k, v in page.get("widgetStates", {}).items() if k.startswith(prefix)]


def _texts(o):
    """All text/title strings under a node, in order."""
    out = []
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("text", "title") and isinstance(v, str):
                out.append(v)
            elif k not in ("trackingInfo", "testInfo", "action", "common"):
                out += _texts(v)
    elif isinstance(o, list):
        for v in o:
            out += _texts(v)
    return out


def _tile(it):
    title = price = old = rating = reviews = ""
    for m in it.get("mainState", []):
        if m.get("id") == "name" or m.get("type") == "textDS" and not title:
            title = m.get("textDS", {}).get("text", title)
        elif m["type"] == "priceV2":
            for p in m["priceV2"]["price"]:
                if p.get("textStyle") == "PRICE":
                    price = p["text"]
                elif p.get("textStyle") == "ORIGINAL_PRICE":
                    old = p["text"]
        elif m["type"] == "labelListV2":
            txt = [x["text"]["text"] for x in m["labelListV2"]["items"] if x.get("type") == "text"]
            if any("отзыв" in x for x in txt):
                rating, reviews = txt[0], re.sub(r"\D", "", txt[-1])
    delivery = next((x for x in _texts(it.get("multiButton", {})) if DATE_RE.search(x)), "")
    link = it.get("action", {}).get("link", "").split("?")[0]
    return {"sku": it.get("sku"), "title": title, "price": re.sub(r"\D", "", price),
            "old": re.sub(r"\D", "", old), "rating": rating, "reviews": reviews,
            "delivery": delivery, "url": link}


def search(query, price_min=None, price_max=None, sort="score", limit=30):
    if sort not in SORTS:
        raise ValueError(f"sort: одно из {SORTS}")
    qs = {"text": query, "sorting": sort}
    if price_min or price_max:
        qs["currency_price"] = f"{float(price_min or 0):.3f};{float(price_max or 10**8):.3f}"
    url = "/search/?" + urllib.parse.urlencode(qs)
    tab = _tab()
    items, seen = [], set()
    try:
        for _ in range(8):  # ~8 tiles per page
            page = _page(tab, url)
            for grid in _widgets(page, "tileGrid"):
                for it in grid.get("items", []):
                    x = _tile(it)
                    if x["sku"] and x["sku"] not in seen:
                        seen.add(x["sku"])
                        items.append(x)
            nxt = [w.get("nextPage") for w in _widgets(page, "infiniteVirtualPaginator") + _widgets(page, "paginator")]
            nxt = next((n for n in nxt if n), None) or page.get("nextPage")
            if len(items) >= limit or not nxt:
                break
            url = nxt
    finally:
        tab.close()
    rows = [f"{i+1}\t{x['sku']}\t{x['price']}\t{x['old']}\t{x['title'][:110]}\t{x['rating']}\t{x['reviews']}\t{x['delivery']}"
            for i, x in enumerate(items[:limit])]
    head = ("Ozon · https://www.ozon.ru/search/?" + urllib.parse.urlencode(qs) +
            "\nтовар: https://www.ozon.ru/product/<sku>\n#\tsku\tцена ₽\tбез скидки\tназвание\tрейтинг\tотзывов\tпривезут")
    return head + "\n" + ("\n".join(rows) if rows else "(пусто)")


def _sku(sku_or_url):
    s = str(sku_or_url).strip()
    if s.isdigit():
        return int(s)
    m = re.search(r"(\d{5,})/?(?:\?|$)", s) or re.search(r"-(\d{5,})", s)
    if not m:
        raise ValueError(f"не нашёл sku в {s!r}")
    return int(m.group(1))


def add_to_cart(sku_or_url, quantity=1):
    sku = _sku(sku_or_url)
    tab = _tab()
    try:
        r = _fetch(tab, "/api/composer-api.bx/_action/addToCart", "POST", [{"id": sku, "quantity": int(quantity)}])
    finally:
        tab.close()
    if not r.get("success"):
        return f"Ozon не добавил {sku}: {json.dumps(r, ensure_ascii=False)[:300]}"
    got = {i["id"]: i["qty"] for i in r.get("cart", {}).get("cartItems", [])}
    return f"добавлено: sku {sku}, в корзине этого товара {got.get(sku, '?')} шт."


def _cart_sku(it):
    try:
        post = it["controls"]["deleteButton"]["common"]["action"]["params"]["postBody"]
        return json.loads(json.loads(post)["params"])["items"][0]
    except (KeyError, ValueError, IndexError):
        return ""


def _cart_text(page):
    if _widgets(page, "emptyCart"):
        return "корзина Ozon пуста"
    rows = []
    for split in _widgets(page, "cartSplit"):
        group = _texts(split.get("header", {}))
        for it in split.get("cartItems", []):
            p = it.get("product", {})
            title = next((t for t in _texts(p.get("titleColumn", [])) if len(t) > 15), "")
            price = next((t for t in _texts(p.get("priceColumn", [])) if "₽" in t), "")
            qty = it.get("controls", {}).get("quantity", {}).get("current", "")
            sku = _cart_sku(it)
            rows.append(f"{sku}\t{qty}\t{re.sub(r'\s', ' ', price)}\t{title[:110]}\t{group[0] if group else ''}")
    total = next(iter(_widgets(page, "total")), {}).get("summary", {})
    foot = total.get("footer", {})
    head = total.get("header", {}).get("info", "")
    return (f"корзина Ozon: {head}, итого {foot.get('price', '?')}\nsku\tшт\tцена\tназвание\tгруппа\n" + "\n".join(rows))


def cart():
    tab = _tab()
    try:
        return _cart_text(_page(tab, "/cart"))
    finally:
        tab.close()


def remove_from_cart(sku_or_url):
    sku = str(_sku(sku_or_url))
    body = {"name": "deleteItems", "params": json.dumps({"items": [sku]})}
    tab = _tab()
    try:
        return "удалено. " + _cart_text(_fetch(tab, API + "%2Fcart", "POST", body))
    finally:
        tab.close()
