"""Avito watch: new single-board computers under a total price, reserved ones skipped.

Run by launchd on the Mac mini (local.shop-watch, 10:00 and 20:00): `uv run --script watch.py`.
Sends new listings (price + cheapest delivery <= MAX_TOTAL) to Telegram (see tg.py) and remembers
them in logs/watch_seen.json, so each listing is reported once. --print prints instead of sending.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets>=13"]
# ///
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import avito  # noqa: E402
from cdp import tab_for  # noqa: E402
from shoplog import log  # noqa: E402
import html  # noqa: E402
import tg  # noqa: E402

MAX_TOTAL = 6000
QUERIES = ["raspberry pi 4", "raspberry pi 5", "orange pi"]
SEEN = Path(__file__).parent / "logs" / "watch_seen.json"

BOARD = re.compile(r"(raspberry|распберри|малин|rpi|\bpi)\s*(4|5|400|500)(b|\b)"
                   r"|orange\s*pi\s*(3|4|5|6|800|zero\s*(2w|3))", re.I)
# Accessories and boards with too little RAM for a desktop (OpenBuilds Control, LinuxCNC).
SKIP = re.compile(r"\bдля\b|корпус|кейс|case|блок питания|бп\b|кулер|радиатор|вентилятор|hat\b|ups|модуль|аксессуар|"
                  r"камер|дисплей|экран|монитор|кабель|адаптер|переходник|плата расширения|ssd|nvme|"
                  r"512\s*mb|1([.,]5)?\s*(gb|гб)\b|без платы", re.I)

# How well a board suits LinuxCNC: (pattern, score, why). First match wins, so Orange Pi goes before "pi 5".
CNC = [
    (r"zero\s*(2w|3)", 1, "слабый вариант: нет Ethernet для платы Mesa, официального образа нет"),
    (r"orange\s*pi\s*5", 3, "мощный (RK3588S), но официального образа LinuxCNC нет, RT-ядро собирать самому"),
    (r"orange\s*pi\s*(3|4)", 2, "официального образа нет, RT-ядро собирать самому; для OpenBuilds Control по USB хватит"),
    (r"\bpi\s*5|pi5", 5, "лучший выбор: есть официальный образ LinuxCNC с RT-ядром, самый быстрый"),
    (r"\bpi\s*4|pi4", 4, "хороший выбор: официальный образ LinuxCNC с RT-ядром, проверен сообществом"),
]


def cnc(title):
    for pat, score, why in CNC:
        if re.search(pat, title, re.I):
            return score, why
    return 0, "модель не распознана"


def wanted(title):
    return bool(BOARD.search(title)) and not SKIP.search(title)


def scan(tab, query):
    url = avito.search_url(query, "all", 2000, MAX_TOTAL, "date", 1, True)
    tab.goto(url, "avito", wait_js="document.querySelector('[data-marker=\"item\"], [data-marker=\"page-title/count\"]')")
    r = tab.js(avito.LIST_JS)
    if r["blocked"]:
        raise RuntimeError(f"Авито показал блокировку или капчу: {r['url']}")
    return [x for x in r["items"] if x["url"] and wanted(x["title"])]


def main():
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    tab = tab_for("avito.ru")
    found, new = {}, []
    try:
        for q in QUERIES:
            for x in scan(tab, q):
                found.setdefault(x["id"], x)
        fresh = [x for i, x in found.items() if i not in seen]
        ship = avito._shipping(tab, [urllib.parse.urlparse(x["url"]).path for x in fresh])
    finally:
        tab.close()
    for x, s in zip(fresh, ship):
        if (s or {}).get("blocked"):
            break
        if (s or {}).get("reserved"):
            continue  # not marked seen: may come back if the deal falls through
        seen.add(x["id"])
        _, cheapest = avito._ship_cell(s)
        price = int(x["price"]) if str(x["price"]).isdigit() else None
        if price is None or cheapest is None or price + cheapest > MAX_TOTAL:
            continue
        score, why = cnc(x["title"])
        if s.get("broken"):
            score, why = -1, "⚠️ по описанию не работает или продаётся как есть. " + why
        new.append((score, price + cheapest,
                    f"{'⭐' * max(score, 0) or '·'} <b>{price + cheapest} ₽</b> ({price} + доставка {cheapest}) · "
                    f"<a href=\"{x['url']}\">{html.escape(x['title'])}</a> · {x['city']}\n{why}"))
    SEEN.parent.mkdir(exist_ok=True)
    SEEN.write_text(json.dumps(sorted(seen)))
    log.info("watch: %d matching, %d not seen before, %d new under %d ₽", len(found), len(fresh), len(new), MAX_TOTAL)
    # Best for LinuxCNC first, then cheapest.
    return [t for _, _, t in sorted(new, key=lambda n: (-n[0], n[1]))]


def notify(text):
    if "--print" in sys.argv:
        print(text)
    else:
        tg.send(text)


if __name__ == "__main__":
    if "--print" not in sys.argv and not tg.CONF.exists():
        log.warning("watch: no telegram.json, skipped")  # else listings would be marked seen unseen
        sys.exit("telegram.json missing, see tg.py")
    try:
        new = main()
    except Exception as e:
        log.exception("watch failed")
        notify(f"⚠️ Мониторинг Авито упал: {html.escape(str(e))[:500]}")
        raise
    if new:
        notify(f"Новые платы на Авито до {MAX_TOTAL} ₽ с доставкой, без резерва. "
               "Сверху лучшие для LinuxCNC (⭐ — насколько подходит):\n\n" + "\n\n".join(new))
