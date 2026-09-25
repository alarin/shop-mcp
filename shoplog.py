"""File log on the mini: logs/shop.log (rotated) plus full bodies of bad responses in logs/bodies/.

stdout is the MCP stdio channel, so nothing may be printed there.
"""
import logging
import logging.handlers
import time
from pathlib import Path

DIR = Path(__file__).parent / "logs"
BODIES = DIR / "bodies"
BODIES.mkdir(parents=True, exist_ok=True)
KEEP_BODIES = 50

log = logging.getLogger("shop")
log.setLevel(logging.INFO)
if not log.handlers:
    h = logging.handlers.RotatingFileHandler(DIR / "shop.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.propagate = False


def dump(site: str, status, body: str, ext: str = "html") -> Path:
    """Save a full response body and return its path; old dumps beyond KEEP_BODIES are removed."""
    path = BODIES / f"{time.strftime('%Y%m%d-%H%M%S')}-{site}-{status}.{ext}"
    path.write_text(body or "", encoding="utf-8")
    for old in sorted(BODIES.iterdir())[:-KEEP_BODIES]:
        old.unlink(missing_ok=True)
    return path
