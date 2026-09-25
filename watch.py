"""Avito watch: new single-board computers under a total price, reserved ones skipped.

Run by launchd on the Mac mini: `uv run --script watch.py`. Prints new listings
(price + cheapest delivery <= MAX_TOTAL) and remembers them in logs/watch_seen.json,
so each listing is reported once.
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

MAX_TOTAL = 6000
QUERIES = ["raspberry pi 4", "raspberry pi 5", "orange pi"]
SEEN = Path(__file__).parent / "logs" / "watch_seen.json"

BOARD = re.compile(r"(raspberry|распберри|малин|rpi|\bpi)\s*(4|5|400|500)\b|orange\s*pi", re.I)
# Accessories and boards with too little RAM for a desktop (OpenBuilds Control, LinuxCNC).
SKIP = re.compile(r"корпус|кейс|case|блок питания|бп\b|кулер|радиатор|вентилятор|hat\b|камер|дисплей|экран|монитор|"
                  r"кабель|адаптер|переходник|плата расширения|ssd|nvme|pico|zero(?!\s*(2w|3))|\bone\b|\bpc\b|lite|"
                  r"512\s*mb|1\s*(gb|гб)\b|4g-iot|без платы", re.I)


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
        new.append(f"{price + cheapest} ₽ ({price} + доставка {cheapest}) · {x['title']} · {x['city']}\n{x['url']}")
    SEEN.parent.mkdir(exist_ok=True)
    SEEN.write_text(json.dumps(sorted(seen)))
    log.info("watch: %d matching, %d not seen before, %d new under %d ₽", len(found), len(fresh), len(new), MAX_TOTAL)
    print("\n\n".join(new))


if __name__ == "__main__":
    main()
