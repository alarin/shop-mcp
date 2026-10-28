"""Avito messenger: list chats, read a chat, send a text message.

Reading goes through the messenger's own JSON-RPC socket (wss://socket.avito.ru), opened from inside
the logged-in Avito tab, so cookies stay in Chrome. Reading does not mark chats as read.
Sending types into the chat's input on the page and presses Enter, the way a person would,
then checks the chat history for the new message.
"""
import json
import time
from datetime import datetime

from cdp import tab_for
from shoplog import log

SOCKET = "wss://socket.avito.ru/?use_seq=true&id_version=v3&app_name=web&app_version=v440"

# One socket per page, kept in window.__shopMessenger and reused by later calls: opening a new one
# right after closing the previous got no session at all (2026-10-06).
RPC_JS = r"""
(async (socket, calls) => {
  const open = () => new Promise((res, rej) => {
    const c = {ws: new WebSocket(socket), pending: {}, id: 1000};
    const t = setTimeout(() => { c.ws.close(); rej(new Error('no session in 10 s')); }, 10000);
    c.ws.onerror = () => { clearTimeout(t); rej(new Error('socket error')); };
    c.ws.onclose = () => { if (window.__shopMessenger === c) window.__shopMessenger = null; };
    c.ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.type === 'session') { clearTimeout(t); res(c); }
      else if (c.pending[m.id]) { c.pending[m.id](m); delete c.pending[m.id]; }
    };
  });
  let c = window.__shopMessenger;
  if (!c || c.ws.readyState !== WebSocket.OPEN) {
    try { c = await open(); }
    catch (e) { await new Promise(r => setTimeout(r, 3000)); c = await open(); }
    window.__shopMessenger = c;
  }
  const out = [];
  for (const [method, params] of calls) {
    const i = ++c.id;
    out.push(await Promise.race([
      new Promise(res => { c.pending[i] = res; c.ws.send(JSON.stringify({method, params, id: i, jsonrpc: '2.0'})); }),
      new Promise(res => setTimeout(() => res({error: {message: 'timeout 15 s'}}), 15000)),
    ]));
  }
  return {out};
})
"""
INPUT = 'textarea[data-marker="reply/input"]'


def _on_avito(tab):
    """The socket wants an avito.ru page; a fresh blank tab is moved there first."""
    if "avito.ru" not in (tab.js("location.host") or ""):
        tab.goto("https://www.avito.ru/profile/messenger", "avito", wait_js="document.body")


def _rpc(tab, *calls):
    """Results of JSON-RPC calls, in order; raises on an RPC error."""
    _on_avito(tab)
    r = tab.js(f"{RPC_JS}({json.dumps(SOCKET)}, {json.dumps([list(c) for c in calls])})", timeout=30 + 16 * len(calls))
    res = []
    for (method, params), m in zip(calls, r["out"]):
        if "error" in m:
            log.warning("avito rpc %s %s: %s", method, params, m["error"])
            raise RuntimeError(f"Авито {method}: {m['error'].get('message') or m['error']}")
        res.append(m["result"])
    return res


def _when(created):
    """Message and chat times are in 100 ns since the epoch."""
    return datetime.fromtimestamp(created / 1e7).strftime("%d.%m %H:%M") if created else ""


def _text(msg):
    if not msg:
        return ""
    body = msg.get("body") or {}
    t = body.get("text") or (msg.get("preview") or {}).get("text")
    if not t and body.get("chunks"):
        t = " ".join((c.get("value") or {}).get("text", "") for c in body["chunks"])
    if not t or t.startswith("Сообщение не поддерживается"):
        t = (msg.get("preview") or {}).get("text") or t or f"[{msg.get('type')}]"
    return " ".join(t.split())


def _mine(msg):
    return bool(msg) and msg.get("fromUid") is not None and msg.get("fromUid") == msg.get("uid")


def _channel_id(chat):
    """Channel id from an id or a messenger link."""
    return chat.strip().rstrip("/").rsplit("/", 1)[-1].split("?")[0]


def chats(limit=20, unread_only=False, before=None):
    """Chats, newest first."""
    tab = tab_for("avito.ru")
    try:
        found, seen, offset = [], set(), before
        while len(found) < limit:
            params = {"limit": 30}
            if offset:
                params["offsetTimestamp"] = int(offset)
            (r,) = _rpc(tab, ("avito.getChats.v5", params))
            page = r.get("channels") or []
            # offsetTimestamp sometimes returns the boundary chat again (one without messages, 2026-10-06)
            found += [c for c in page if c["channelId"] not in seen and (not before or c["updated"] < int(before))
                      and (not unread_only or not c.get("isRead"))]
            seen.update(c["channelId"] for c in page)
            if not r.get("hasMore") or not page:
                break
            offset = page[-1]["updated"]
    finally:
        tab.close()
    more = len(found) > limit
    found = found[:limit]
    rows = []
    for c in found:
        v = (c.get("context") or {}).get("value") or {}
        name = (c.get("info") or {}).get("name", "")
        lm = c.get("lastMessage")
        who = "я" if _mine(lm) else (name if lm and lm.get("type") == "text" else "")
        last = f"{who}: {_text(lm)}" if who else _text(lm)
        rows.append("\t".join([c["channelId"], "" if c.get("isRead") else "●", _when(c.get("updated")), name,
                               v.get("title", ""), v.get("priceString", ""), last[:200], v.get("urlPath", "")]))
    head = "чат\tнепрочитан\tкогда\tсобеседник\tобъявление\tцена\tпоследнее сообщение\tобъявление (путь на avito.ru)"
    tail = f"\nследующая страница: before={found[-1]['updated']}" if found and (more or len(found) == limit) else ""
    return head + "\n" + ("\n".join(rows) if rows else "(пусто)") + tail


def chat(chat_id, limit=30, offset=0):
    """One chat: who and which listing, then messages oldest first."""
    cid = _channel_id(chat_id)
    tab = tab_for("avito.ru")
    try:
        info, hist = _rpc(tab, ("avito.getChatById.v3", {"channelId": cid}),
                          ("messenger.history.v2", {"channelId": cid, "limit": int(limit), "offset": int(offset)}))
    finally:
        tab.close()
    v = (info.get("context") or {}).get("value") or {}
    name = (info.get("info") or {}).get("name", "")
    head = f"чат {cid} с {name}"
    if v.get("urlPath"):
        head += f" · {v.get('title', '')} · {v.get('priceString', '')} · https://www.avito.ru{v['urlPath']}"
    lines = [head]
    if (info.get("inputState") or {}).get("isDisabled"):
        lines.append("⛔ писать в этот чат нельзя")
    items = hist.get("items") or []
    for m in reversed(items):
        if m.get("isDeleted"):
            continue
        who = "я" if _mine(m) else ("Авито" if m.get("type") == "system" else name)
        lines.append(f"{_when(m.get('created'))}\t{who}\t{_text(m)}")
    if len(items) == int(limit):
        lines.append(f"(есть сообщения раньше: offset={int(offset) + int(limit)})")
    return "\n".join(lines)


def _wait(tab, js, seconds):
    for _ in range(int(seconds * 2)):
        if tab.js(js):
            return True
        time.sleep(0.5)
    return False


def _click_biggest(tab, selector):
    """Real mouse click on the largest visible element matching selector."""
    pos = tab.js(f"""(() => {{
      const area = e => {{ const r = e.getBoundingClientRect(); return r.width * r.height; }};
      const b = [...document.querySelectorAll({json.dumps(selector)})].filter(e => e.offsetParent !== null).sort((x, y) => area(y) - area(x))[0];
      if (!b) return null;
      b.scrollIntoView({{block: 'center'}});
      const r = b.getBoundingClientRect();
      return [r.x + r.width / 2, r.y + r.height / 2];
    }})()""")
    if not pos:
        return False
    time.sleep(0.4)
    for typ in ("mouseMoved", "mousePressed", "mouseReleased"):
        tab.call("Input.dispatchMouseEvent", type=typ, x=pos[0], y=pos[1], button="left", clickCount=1)
    return True


def _type_and_send(tab, text):
    """Focus the reply box, insert the text as real input, press Enter. False if there is no reply box."""
    if not _wait(tab, f"!!document.querySelector({json.dumps(INPUT)})", 10):
        return False
    tab.click(INPUT)
    tab.js(f"document.querySelector({json.dumps(INPUT)}).focus()")
    tab.call("Input.insertText", text=text)   # keeps the text's own line breaks
    time.sleep(0.5)
    if (tab.js(f"document.querySelector({json.dumps(INPUT)}).value") or "").strip() != text:
        raise RuntimeError("текст не попал в поле ввода — сообщение не отправлено")
    tab.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter",
             windowsVirtualKeyCode=13, nativeVirtualKeyCode=13, text="\r")
    tab.call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter",
             windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
    return True


def send(text, chat_id=None, item_url=None):
    """Send text to an existing chat, or to the seller of a listing (opens the chat from its page)."""
    text = text.strip()
    if not text:
        return "пустой текст — ничего не отправлено"
    if bool(chat_id) == bool(item_url):
        return "нужно одно из двух: chat_id (из avito_chats) или item_url (ссылка на объявление)"
    flat = " ".join(text.split())
    cid = _channel_id(chat_id) if chat_id else None
    tab = tab_for("avito.ru")
    try:
        # In a background tab the listing's button does nothing (2026-10-06): bring the tab forward
        # and make the page believe it has focus.
        tab.call("Page.bringToFront")
        tab.call("Emulation.setFocusEmulationEnabled", enabled=True)
        if cid:
            tab.goto(f"https://www.avito.ru/profile/messenger/channel/{cid}", "avito",
                     wait_js=f"document.querySelector({json.dumps(INPUT)})")
        else:
            tab.goto(item_url, "avito", wait_js="document.querySelector('[data-marker=\"messenger-button/button\"]')")
            # The page has two such buttons; the first sits in a sticky header off screen. Click the biggest.
            if not _click_biggest(tab, '[data-marker="messenger-button/button"]'):
                # Logged out, the button turns into a link to #login (2026-10-28).
                if tab.js("!!document.querySelector('[data-marker=\"messenger-button/link\"][href*=\"login\"]')"):
                    return "Авито: вход не выполнен — войдите в Chrome на макмини. Ничего не отправлено"
                h1 = " ".join((tab.js("document.querySelector('h1')?.textContent") or "").split())
                return f"на странице объявления нет кнопки «Написать сообщение» (заголовок: «{h1}») — ничего не отправлено"
        if not _type_and_send(tab, text):
            return "не нашёл поле ввода сообщения — ничего не отправлено. Чат закрыт или страница не загрузилась."
        # The message shows up in history within a second or two. A chat opened from a listing
        # is found by its URL, or as the newest chat whose last message is ours.
        for _ in range(10):
            time.sleep(1)
            if not cid:
                href = tab.js("location.href") or ""
                if "/channel/" in href:
                    cid = _channel_id(href)
                else:
                    (r,) = _rpc(tab, ("avito.getChats.v5", {"limit": 5}))
                    hit = [c for c in r.get("channels") or [] if _mine(c.get("lastMessage")) and _text(c["lastMessage"]) == flat]
                    cid = hit[0]["channelId"] if hit else None
                if not cid:
                    continue
            (h,) = _rpc(tab, ("messenger.history.v2", {"channelId": cid, "limit": 5}))
            if any(_mine(m) and _text(m) == flat for m in h.get("items") or []):
                return f"отправлено в чат {cid}"
    finally:
        try:
            tab.call("Emulation.setFocusEmulationEnabled", enabled=False)
        except Exception:
            pass
        tab.close()
    log.warning("avito send: message not seen in history, chat %s", cid)
    return (f"⚠️ Enter нажат, но в истории чата {cid or '(не определён)'} сообщения не видно. "
            "Проверить через avito_chats / avito_chat, прежде чем отправлять ещё раз.")
