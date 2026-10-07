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

Alerts are scored: each one is appended to asr/logs/premarket_alerts.jsonl, and
the next run after its session closes appends an outcome line (close vs last
close, and whether the pre-market gap held or faded). `--report` prints the
hit rate. The comparison itself stays stateless. A run where every ticker
fails to fetch pushes a "壞咗" alert instead of staying silent.

Run: python ops/premarket_watch.py          (scheduled task "VoiceOS Premarket Watch")
     python ops/premarket_watch.py --dry    (fetch + log + print, no push or ledger writes)
     python ops/premarket_watch.py --report (print how past alerts resolved)
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
LEDGER = os.path.join(ROOT, "asr", "logs", "premarket_alerts.jsonl")

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
    regular = meta["currentTradingPeriod"]["regular"]
    return {**reading, "status": status, "price": price, "prev": prev, "move": move,
            "session_start": regular["start"], "session_end": regular["end"]}


def format_alert(r: dict) -> str:
    return f"{r['ticker']} 盤前 {r['move']:+.1f}%（${r['price']:.2f}，上次收市 ${r['prev']:.2f}）"


def _read_ledger() -> list[dict]:
    if not os.path.exists(LEDGER):
        return []
    with open(LEDGER, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _append_ledger(rows: list[dict]) -> None:
    with open(LEDGER, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def outcome(alert: dict, close: float) -> dict:
    """Pure: how a pre-market alert resolved against the session's close."""
    close_move = (close / alert["prev"] - 1) * 100
    return {
        "type": "outcome", "ticker": alert["ticker"], "session": alert["session"],
        "close": close, "close_move": close_move,
        # held = closed on the same side of the last close, at >= half the gap
        "held": close_move * alert["move"] > 0 and abs(close_move) >= abs(alert["move"]) / 2,
    }


def resolve_pending(now: dt.datetime) -> None:
    """Score alerts whose regular session has closed and have no outcome yet."""
    import stock_fundamentals as sf

    rows = _read_ledger()
    done = {(r["ticker"], r["session"]) for r in rows if r["type"] == "outcome"}
    pending = [r for r in rows if r["type"] == "alert"
               and (r["ticker"], r["session"]) not in done and r["session_end"] < now.timestamp()]
    new = []
    for alert in pending:
        try:
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{alert['ticker']}"
                   "?interval=1d&range=10d")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            chart = json.loads(sf.urlopen_retry(req, timeout=20))["chart"]["result"][0]
            bars = dict(zip(chart["timestamp"], chart["indicators"]["quote"][0]["close"]))
            close = next((c for t, c in bars.items()
                          if abs(t - alert["session_start"]) < 3600 and c is not None), None)
            if close is None:
                continue  # session bar not published yet (or holiday gap): retry next run
            new.append(outcome(alert, close))
            log.info("outcome: %s", json.dumps(new[-1], ensure_ascii=False))
        except Exception as exc:
            log.warning("%s: outcome fetch failed: %s", alert["ticker"], exc)
    _append_ledger(new)


def report() -> None:
    rows = _read_ledger()
    alerts = {(r["ticker"], r["session"]): r for r in rows if r["type"] == "alert"}
    outs = [r for r in rows if r["type"] == "outcome"]
    for o in outs:
        a = alerts[(o["ticker"], o["session"])]
        print(f"{o['session']} {o['ticker']:5} pre {a['move']:+.1f}% -> close {o['close_move']:+.1f}%"
              f"  {'held' if o['held'] else 'faded'}")
    held = sum(o["held"] for o in outs)
    print(f"{held}/{len(outs)} held; {len(alerts) - len(outs)} pending")


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

    if "--report" in sys.argv:
        return report()
    dry = "--dry" in sys.argv
    now = dt.datetime.now(dt.timezone.utc)
    if not dry:
        resolve_pending(now)
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
