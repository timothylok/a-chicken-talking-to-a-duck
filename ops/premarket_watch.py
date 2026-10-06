"""US pre-market watch: one ntfy push at 22:30 NZT when a watchlist stock is
trading well away from its last close before the US open.

US pre-market is 04:00-09:30 ET = 21:00-02:30 NZDT (22:00-03:30 once US DST
ends in November); 22:30 NZT is 60-90 minutes in -- late enough for overnight
earnings/news reactions to show, early enough to see before bed. The session
window comes from Yahoo's own currentTradingPeriod, so DST, weekends and US
holidays need no calendar here: outside the window the run is a silent no-op.

Alerts when |pre-market price / last close - 1| >= MIN_MOVE_PCT, but only on an
actively traded book. Yahoo reports 0 volume for extended-hours bars, so
activity is measured by bar count instead -- a 5-minute bar only exists when
something traded. On 2026-10-06 the eight liquid tickers traded in 18 of 18
slots by 22:30, while AON printed 4: +2.07% on those four trades, then
closed +0.96%. A ticker needs MIN_ACTIVE_SHARE of the elapsed slots, and its
latest bar must be under MAX_STALE old.

No state file: one run a day, nothing to compare across days. A run where every
ticker fails to fetch pushes a "壞咗" alert instead of staying silent.

Run: python ops/premarket_watch.py          (scheduled task "VoiceOS Premarket Watch")
     python ops/premarket_watch.py --dry    (fetch + log + print, no push)
"""

import datetime as dt
import json
import logging
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ops"))
LOG_PATH = os.path.join(ROOT, "asr", "logs", f"premarket_watch-{dt.date.today():%Y-%m-%d}.log")
NZ_TZ = ZoneInfo("Pacific/Auckland")

WATCHLIST = [
    t.strip().upper()
    for t in os.environ.get("STOCK_WATCHLIST", "AAPL,MSFT,NVDA,TSLA,GOOGL,AMZN,META,AON,SPCX").split(",")
    if t.strip()
]
MIN_MOVE_PCT = 2.0
MIN_ACTIVE_SHARE = 2 / 3     # of elapsed 5-minute slots that must hold a trade
MIN_ACTIVE_BARS = 6          # first 30 min: too few slots for a share to mean much
MAX_STALE = dt.timedelta(minutes=20)
BAR = dt.timedelta(minutes=5)

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("premarket_watch")


def assess(ticker: str, chart: dict, now: dt.datetime) -> dict:
    """Pure: one ticker's pre-market reading from a Yahoo chart result.

    Returns {"ticker", "status", ...}; status is "alert", "quiet", "thin",
    "stale" or "closed" (now outside the pre-market window).
    """
    meta = chart["meta"]
    pre = meta["currentTradingPeriod"]["pre"]
    start = dt.datetime.fromtimestamp(pre["start"], dt.timezone.utc)
    end = dt.datetime.fromtimestamp(pre["end"], dt.timezone.utc)
    if not start <= now < end:
        return {"ticker": ticker, "status": "closed"}
    quote = chart["indicators"]["quote"][0]
    bars = [
        (dt.datetime.fromtimestamp(t, dt.timezone.utc), c)
        for t, c in zip(chart.get("timestamp") or [], quote.get("close") or [])
        if c is not None and pre["start"] <= t <= now.timestamp()
    ]
    slots = int((now - start) / BAR) + 1  # the bar in progress counts
    reading = {"ticker": ticker, "bars": len(bars), "slots": slots}
    if len(bars) < max(MIN_ACTIVE_BARS, MIN_ACTIVE_SHARE * slots):
        return {**reading, "status": "thin"}
    last_at, price = bars[-1]
    if now - last_at > MAX_STALE:
        return {**reading, "status": "stale"}
    prev = meta["chartPreviousClose"]
    move = (price / prev - 1) * 100
    status = "alert" if abs(move) >= MIN_MOVE_PCT else "quiet"
    return {**reading, "status": status, "price": price, "prev": prev, "move": move}


def format_alert(r: dict) -> str:
    return f"{r['ticker']} 盤前 {r['move']:+.1f}%（${r['price']:.2f}，上次收市 ${r['prev']:.2f}）"


def _fetch(ticker: str) -> dict:
    import stock_fundamentals as sf

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           "?interval=5m&range=1d&includePrePost=true")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(sf.urlopen_retry(req, timeout=20))["chart"]["result"][0]


def main() -> None:
    from notify import notify as push, notify_discord

    def notify(title: str, message: str, priority: int = 3) -> bool:
        sent = push(title, message, priority)
        if not notify_discord(title, message):
            log.warning("discord post failed or not configured")
        return sent

    dry = "--dry" in sys.argv
    now = dt.datetime.now(dt.timezone.utc)
    results, failed = [], []
    for ticker in WATCHLIST:
        try:
            r = assess(ticker, _fetch(ticker), now)
        except Exception as exc:
            log.warning("%s: fetch failed: %s", ticker, exc)
            failed.append(ticker)
            continue
        log.info("%s: %s", ticker, json.dumps(r, ensure_ascii=False))
        results.append(r)

    if not results:
        log.error("every ticker failed")
        if not dry:
            notify("美股盤前監察壞咗", f"{len(failed)} 隻股票都攞唔到盤前價，"
                   f"睇下 asr/logs/premarket_watch-{dt.date.today():%Y-%m-%d}.log", priority=4)
        return
    if all(r["status"] == "closed" for r in results):
        log.info("outside the pre-market window (weekend, US holiday or off-schedule run)")
        return
    alerts = sorted((r for r in results if r["status"] == "alert"), key=lambda r: -abs(r["move"]))
    if not alerts:
        log.info("no moves over %.1f%%", MIN_MOVE_PCT)
        if dry:
            print("no alerts")
        return
    message = "\n".join(format_alert(r) for r in alerts)
    log.info("alerts:\n%s", message)
    if dry:
        print(message)
    elif not notify("美股盤前異動", message, priority=3):
        log.warning("ntfy failed")


if __name__ == "__main__":
    main()
