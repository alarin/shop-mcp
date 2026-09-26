"""Telegram notifications for watch.py.

Config: telegram.json next to this file (gitignored): {"token": "...", "chat_id": 123}.
Setup: put {"token": "<from @BotFather>"} there, send /start to the bot, run `python3 tg.py setup` —
it finds your chat id from the bot's updates and sends a test message.
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CONF = Path(__file__).parent / "telegram.json"
LIMIT = 4000  # Telegram caps a message at 4096 characters


def _api(conf, method, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{conf['token']}/{method}", data, timeout=20) as r:
        return json.load(r)["result"]


def send(text):
    """Send text (HTML) to the configured chat, split on blank lines to stay under the size limit."""
    conf = json.loads(CONF.read_text())
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > LIMIT:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{block}" if cur else block
    chunks.append(cur)
    for c in chunks:
        _api(conf, "sendMessage", chat_id=conf["chat_id"], text=c, parse_mode="HTML", disable_web_page_preview="true")


def setup():
    conf = json.loads(CONF.read_text())
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("username") for u in _api(conf, "getUpdates") if "message" in u}
    if not chats:
        sys.exit("No messages yet: send /start to the bot and run again.")
    conf["chat_id"] = list(chats)[-1]
    CONF.write_text(json.dumps(conf))
    CONF.chmod(0o600)
    send("Бот мониторинга Авито подключён ✅")
    print(f"chat_id {conf['chat_id']} (@{chats[conf['chat_id']]}) saved, test message sent")


if __name__ == "__main__" and sys.argv[1:] == ["setup"]:
    setup()
