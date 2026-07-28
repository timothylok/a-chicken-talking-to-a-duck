"""Push an ntfy alert if stock_earnings.py generated any reports overnight.

Deliberately a separate scheduled task from stock_earnings.py itself (the
"VoiceOS Earnings Watch" run is meant to fire early, e.g. 1:00 AM NZT, well
before the user is awake; this one runs later, e.g. 10:00 AM, so the alert
lands at a reasonable hour instead of paging the phone at 1 AM). Reads the
run-summary stock_earnings.py always writes to
asr/logs/earnings_watch_last_run.json ({"date", "tickers", "notified"}) --
no SEC/Notion calls of its own.

Per ops/notify.py's own rule: alert content stays non-sensitive (ticker
names only, no figures) -- ntfy.sh caches messages in plaintext.
"""

import datetime as dt
import json
import logging
import os
import sys
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAST_RUN = os.path.join(ROOT, "asr", "logs", "earnings_watch_last_run.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", "earnings_watch_notify.log")
NZ_TZ = ZoneInfo("Pacific/Auckland")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
log = logging.getLogger("earnings_watch_notify")
log.propagate = False
log.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)


def main() -> None:
    from notify import notify

    try:
        with open(LAST_RUN, encoding="utf-8") as f:
            run = json.load(f)
    except (OSError, ValueError):
        log.info("no run summary yet (%s) -- stock_earnings.py hasn't run", LAST_RUN)
        return

    today = dt.datetime.now(NZ_TZ).date().isoformat()
    if run.get("date") != today:
        log.info("run summary is stale (date=%s, today=%s) -- earnings watch didn't run today", run.get("date"), today)
        return
    if run.get("notified"):
        log.info("already notified today, skipping")
        return
    tickers = run.get("tickers") or []
    if not tickers:
        log.info("ran today, nothing generated -- no alert")
        run["notified"] = True
        with open(LAST_RUN, "w", encoding="utf-8") as f:
            json.dump(run, f, ensure_ascii=False, indent=2)
        return

    sent = notify("季度業績報告", f"已生成 {len(tickers)} 份: {', '.join(tickers)}（詳見Notion）", priority=3)
    if sent:
        run["notified"] = True
        with open(LAST_RUN, "w", encoding="utf-8") as f:
            json.dump(run, f, ensure_ascii=False, indent=2)
        log.info("alert sent: %s", tickers)
    else:
        log.warning("alert NOT sent (no ntfy config or network error), will retry next run: %s", tickers)


if __name__ == "__main__":
    main()
