"""Daily batch stock analysis for a fixed watchlist, saved to Notion.

Runs at 09:30 NZT via the "VoiceOS Stock Daily" scheduled task (runs as the
user, same isolation as notion_sync.py/nvidia_daily.py — not the VoiceASR
service, so the Notion key never enters the service environment). Reuses
asr/router.py's STOCK_ANALYSIS data fetch + Ollama narrative for each
ticker in STOCK_WATCHLIST, then writes one Notion row per ticker.

Setup:
  1. Requires ops/notion.json already configured with api_key + database_id
     (see notion_sync.py's own --setup — this reuses the same integration).
  2. python ops/stock_daily.py --setup
     (creates the "股票分析 Stock Analysis" database under the same parent
     page as the existing Voice History database — no new page_id needed —
     and adds stock_database_id to the config)

Until stock_database_id exists in the config, runs are silent no-ops.
"""

import datetime as dt
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "asr"))
from router import _fetch_stock_stats, _stock_narrative  # noqa: E402

LOG_PATH = os.path.join(ROOT, "asr", "logs", "stock_daily.log")
CONFIG = os.path.join(ROOT, "ops", "notion.json")
NOTION_VERSION = "2022-06-28"
NZ_TZ = ZoneInfo("Pacific/Auckland")
WATCHLIST = [
    t.strip().upper()
    for t in os.environ.get("STOCK_WATCHLIST", "AAPL,MSFT,NVDA,TSLA,GOOGL,AMZN,META,SPCX").split(",")
    if t.strip()
]

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("stock_daily")


def _notion(method: str, path: str, payload: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"notion {exc.code} on {path}: {detail}") from exc


def _rich(text: str) -> list:
    # Notion caps a text block at 2000 chars.
    return [{"type": "text", "text": {"content": text[:2000]}}]


def load_config() -> "dict | None":
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _existing_parent_page(cfg: dict) -> str:
    # Reuse the Voice History database's parent page so --setup doesn't need
    # a fresh page_id from the user.
    result = _notion("GET", f"/databases/{cfg['database_id']}", {}, cfg["api_key"])
    parent = result.get("parent", {})
    if parent.get("type") != "page_id":
        raise RuntimeError(f"unexpected parent type for existing database: {parent}")
    return parent["page_id"]


def create_database(parent_page_id: str, api_key: str) -> str:
    result = _notion("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": "股票分析 Stock Analysis"}}],
        "properties": {
            "Ticker": {"title": {}},
            "Date": {"date": {}},
            "Price": {"number": {"format": "number"}},
            "Currency": {"select": {}},
            "Day Change %": {"number": {"format": "percent"}},
            "Week Change %": {"number": {"format": "percent"}},
            "SMA50": {"number": {"format": "number"}},
            "SMA200": {"number": {"format": "number"}},
            "RSI14": {"number": {"format": "number"}},
            "AI Take": {"rich_text": {}},
        },
    }, api_key)
    return result["id"]


def _page_properties(ticker: str, stats: dict, narrative: str, when: dt.datetime) -> dict:
    # Notion's "percent" number format displays the stored value * 100, so
    # day_change_pct=3.53 (already a percentage) is stored as the 0.0353 fraction.
    props = {
        "Ticker": {"title": _rich(ticker)},
        "Date": {"date": {"start": when.date().isoformat()}},
        "Price": {"number": stats["price"]},
        "Currency": {"select": {"name": stats["currency"] or "?"}},
        "Day Change %": {"number": round(stats["day_change_pct"] / 100, 4)},
        "AI Take": {"rich_text": _rich(narrative)},
    }
    if stats.get("week_change_pct") is not None:
        props["Week Change %"] = {"number": round(stats["week_change_pct"] / 100, 4)}
    if stats.get("sma50") is not None:
        props["SMA50"] = {"number": stats["sma50"]}
    if stats.get("sma200") is not None:
        props["SMA200"] = {"number": stats["sma200"]}
    if stats.get("rsi14") is not None:
        props["RSI14"] = {"number": stats["rsi14"]}
    return props


def run() -> int:
    cfg = load_config()
    if not cfg or not cfg.get("api_key") or not cfg.get("stock_database_id"):
        log.info("not configured (%s); skipping", CONFIG)
        return 0
    now = dt.datetime.now(NZ_TZ)
    written = 0
    for ticker in WATCHLIST:
        try:
            stats = _fetch_stock_stats(ticker)
            narrative = _stock_narrative(ticker, stats)
            _notion("POST", "/pages", {
                "parent": {"database_id": cfg["stock_database_id"]},
                "properties": _page_properties(ticker, stats, narrative, now),
            }, cfg["api_key"])
            written += 1
        except Exception as exc:
            log.error("ticker %s failed: %s", ticker, exc)
    log.info("wrote %d/%d tickers", written, len(WATCHLIST))
    return written


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--setup":
        cfg = load_config()
        if not cfg or not cfg.get("api_key") or not cfg.get("database_id"):
            print(f"{CONFIG} needs an existing api_key + database_id first "
                  "(see notion_sync.py --setup)")
            sys.exit(1)
        parent_page_id = _existing_parent_page(cfg)
        db_id = create_database(parent_page_id, cfg["api_key"])
        cfg["stock_database_id"] = db_id
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print(f"database created and saved to config: {db_id}")
        return
    try:
        run()
    except Exception as exc:
        log.error("run failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
