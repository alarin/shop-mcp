"""Minimal Chrome DevTools client for the shop-chrome instance on 127.0.0.1:9222.

One reusable tab per site (matched by domain), so the browser does not pile up tabs.
"""
import itertools
import json
import time
import urllib.request

from websockets.sync.client import connect

CDP = "http://127.0.0.1:9222"
_ids = itertools.count(1)
_last_nav: dict[str, float] = {}
MIN_GAP = 4.0  # seconds between page loads on one site, keeps us under anti-bot radar


def _http(path: str, method: str = "GET"):
    req = urllib.request.Request(CDP + path, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


class Tab:
    def __init__(self, ws_url: str):
        self.ws = connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=10)

    def close(self):
        self.ws.close()

    def call(self, method: str, timeout: float = 60, **params):
        mid = next(_ids)
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        deadline = time.time() + timeout
        while True:
            msg = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.time())))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr: str, timeout: float = 60):
        """Evaluate JS (may return a Promise) and return the JSON value."""
        r = self.call("Runtime.evaluate", timeout=timeout, expression=expr,
                      awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError(d.get("exception", {}).get("description") or d.get("text"))
        return r["result"].get("value")

    def click(self, selector: str) -> bool:
        """Real mouse click (isTrusted) on the element; some sites ignore el.click()."""
        pos = self.js(f"""(() => {{
          const e = document.querySelector({json.dumps(selector)});
          if (!e) return null;
          e.scrollIntoView({{block: 'center'}});
          const b = e.getBoundingClientRect();
          return [b.x + b.width / 2, b.y + b.height / 2];
        }})()""")
        if not pos:
            return False
        time.sleep(0.4)
        for typ in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=typ, x=pos[0], y=pos[1], button="left", clickCount=1)
        return True

    def goto(self, url: str, site: str, wait_js: str | None = None, timeout: float = 30):
        gap = MIN_GAP - (time.time() - _last_nav.get(site, 0))
        if gap > 0:
            time.sleep(gap)
        _last_nav[site] = time.time()
        self.call("Page.navigate", url=url)
        deadline = time.time() + timeout
        time.sleep(1.5)
        while time.time() < deadline:
            try:
                ready = self.js("document.readyState", timeout=5)
                if ready in ("interactive", "complete") and (not wait_js or self.js(wait_js, timeout=5)):
                    return
            except Exception:
                pass  # page is swapping documents mid-navigation
            time.sleep(0.7)
        # fall through: caller extracts whatever is there and reports emptiness


def tab_for(domain: str) -> Tab:
    try:
        tabs = _http("/json/list")
    except OSError as e:
        raise RuntimeError("Chrome на макмини не отвечает на 127.0.0.1:9222 "
                           "(launchctl kickstart -k gui/501/local.shop-chrome)") from e
    pages = [t for t in tabs if t.get("type") == "page"]
    for t in pages:
        if domain in t.get("url", ""):
            return Tab(t["webSocketDebuggerUrl"])
    blank = [t for t in pages if t.get("url", "").startswith(("about:blank", "chrome://newtab"))]
    t = blank[0] if blank else _http("/json/new?about:blank", method="PUT")
    return Tab(t["webSocketDebuggerUrl"])
