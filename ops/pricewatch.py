"""Push a ntfy alert when a watched PriceSpy product's price drops from the
last day it was checked.

Runs daily via the "VoiceOS Price Watch" scheduled task as the logged-in
user (same ntfy isolation as milk_watch.py -- the topic never enters the
VoiceASR service environment). Unlike milk_watch.py, this does NOT depend
on any upstream "price history" dataset for the day-over-day baseline --
each run records its own observed price to a local state file, and the
next day's run compares against that. This sidesteps the exact class of
bug found in milk_watch.py 2026-07-29 (an upstream history dataset that
silently stalled for days, causing the same stale "drop" to be re-reported
every morning) -- our own daily observation is always the source of truth.

Products: PRICEWATCH_PRODUCTS env var, comma-separated PriceSpy product ids
(default: 13779039 "Pokemon Legends Z-A (Switch)" +
13101596 "Nintendo Switch 2" +
5774154 "Corsair Vengeance LPX White DDR4 3200MHz 2x16GB" +
5848136 "Kingston Fury Beast Black DDR4 3200MHz 2x16GB" +
5241360 "G.Skill Ripjaws V Black DDR4 3600MHz 2x16GB",
https://pricespy.co.nz/product.php?p=13779039 /
https://pricespy.co.nz/product.php?p=13101596 /
https://pricespy.co.nz/product.php?p=5774154 /
https://pricespy.co.nz/product.php?p=5848136 /
https://pricespy.co.nz/product.php?p=5241360).

Uses the thecolab-ai nz-pricewatch skill's CLI directly (no login, no
account, public PriceSpy product pages only).

Run manually: python ops/pricewatch.py
"""

import datetime as dt
import json
import logging
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "asr", "logs", "pricewatch.log")
STATE_PATH = os.path.join(ROOT, "asr", "logs", "pricewatch_state.json")
CLI = "D:/ai/thecolab-skills/skills/nz-pricewatch/scripts/cli.py"
PRODUCTS = [p.strip() for p in os.environ.get("PRICEWATCH_PRODUCTS", "13779039,13101596,5774154,5848136,5241360").split(",") if p.strip()]
NZ_TZ = ZoneInfo("Pacific/Auckland")
sys.path.insert(0, os.path.join(ROOT, "ops"))

logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("pricewatch")


def _run_skill(*args: str, timeout: int = 30) -> dict:
    # PYTHONIOENCODING forces the child's stdout to UTF-8 -- under a
    # scheduled task it can default to cp1252, same trap asr/router.py's
    # own _run_skill() guards against for the voice-command skill CLIs.
    out = subprocess.run(
        [sys.executable, CLI, *args],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return json.loads(out.stdout)


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main() -> None:
    from notify import notify

    state = _load_state()
    today = dt.datetime.now(NZ_TZ).date().isoformat()

    for product_id in PRODUCTS:
        try:
            data = _run_skill("product", product_id, "--json")
        except Exception:
            log.exception("%s: fetch failed", product_id)
            continue

        price = data.get("current_lowest_price")
        title = data.get("title", product_id)
        if price is None:
            log.warning("%s: no current_lowest_price in response", product_id)
            continue

        prev = state.get(product_id)
        prev_price = prev.get("price") if prev else None
        prev_date = prev.get("date") if prev else None

        if prev_price is not None and prev_date != today and price < prev_price:
            line = f"{title} 平咗：${prev_price:.2f} → ${price:.2f}\n{data.get('url', '')}"
            sent = notify("價錢監察", line, priority=3)
            log.info("%s: drop $%.2f -> $%.2f, alert %s", title, prev_price, price,
                      "sent" if sent else "NOT sent")
        else:
            log.info("%s: current $%.2f, last recorded $%s -- no alert", title, price,
                      f"{prev_price:.2f}" if prev_price is not None else "n/a (first run)")

        # Only record once per day so a same-day rerun (manual test, retry)
        # doesn't overwrite tomorrow's "yesterday" baseline with today's price.
        if prev_date != today:
            state[product_id] = {"date": today, "price": price, "title": title}

    _save_state(state)


if __name__ == "__main__":
    main()
