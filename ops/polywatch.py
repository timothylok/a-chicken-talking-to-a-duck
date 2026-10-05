"""Daily Polymarket odds watch on the Magnificent 7 (NVDA MSFT GOOGL AAPL AMZN META TSLA).

Four market families, all rediscovered every run because Polymarket rolls most
of them over weekly/monthly (a fixed slug list would go stale within a month):

  largest  "Largest Company end of December <year>" -- each Mag 7's odds of
           being #1 by market cap. By far the most liquid (~$7.7M, 2026-10-05).
  earnings "Will <X> beat quarterly earnings?" -- only exists around each
           report date; a newly listed one alerts once on first sighting.
  pricehit "What will <X> hit in <Month> <year>?" -- the month's price ladder
           (one Yes/No market per level), rolls to the next month by slug.
  mcap     "<X>'s Market Cap end of ..." -- year-end bands where they exist;
           Meta only has monthly ones and Tesla none (checked 2026-10-05).

Silent unless a market's Yes odds moved at least MIN_MOVE_PTS for its family
since the last day it was recorded; then ONE combined ntfy push. Same baseline
rules as ops/pricewatch.py: the job records its own daily observation (never an
upstream history series), a same-day rerun doesn't overwrite the baseline, and
a failed push leaves the alerted baselines alone so the move re-raises next run.
Zero-volume markets are skipped: with no trades the listed price is a default,
not a crowd estimate.

Data via the owner's read-only skill D:\\ai\\polymarket-skill (keyless public APIs).

Run: python ops/polywatch.py          (scheduled task "VoiceOS Polymarket Watch")
     python ops/polywatch.py --dry    (fetch + log + print alerts, no push, no state write)
"""

import datetime as dt
import json
import logging
import os
import sys
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = "D:/ai/polymarket-skill/scripts"
STATE_PATH = os.path.join(ROOT, "asr", "logs", "polywatch_state.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", f"polywatch-{dt.date.today():%Y-%m-%d}.log")
NZ_TZ = ZoneInfo("Pacific/Auckland")

# Ticker -> the company name Polymarket uses in titles and option labels.
COMPANIES = {
    "NVDA": "NVIDIA", "MSFT": "Microsoft", "GOOGL": "Alphabet", "AAPL": "Apple",
    "AMZN": "Amazon", "META": "Meta", "TSLA": "Tesla",
}
# Percentage points per family. A price-ladder rung swings 10+ points on an
# ordinary 3% stock day, while the $7M largest-company market rarely moves 5.
MIN_MOVE_PTS = {"largest": 5, "earnings": 10, "mcap": 10, "pricehit": 15}
FAMILY_YUE = {"largest": "全球最大公司", "earnings": "業績勝預期", "mcap": "市值", "pricehit": "本月股價"}
MAX_LINES = 10
STATE_KEEP_DAYS = 40  # rolled-over monthly/weekly markets age out of the state file

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("polywatch")


# ---------------------------------------------------------------------------
# Discovery (network, via the polymarket skill)
# ---------------------------------------------------------------------------
def _observations(event: dict, family: str, ticker: "str | None") -> list:
    obs = []
    for m in event["markets"]:
        if m["closed"] or not m["volume"] or [o["name"] for o in m["outcomes"]] != ["Yes", "No"]:
            continue
        label = m["label"] or m["question"]
        tkr = ticker
        if family == "largest":
            tkr = next((t for t, name in COMPANIES.items() if name == label), None)
            if tkr is None:
                continue  # Saudi Aramco, SpaceX, placeholder "Company B" rows
        price = m["outcomes"][0]["price"]
        if price is None:
            continue
        obs.append({"key": m["slug"], "family": family, "ticker": tkr, "label": label,
                    "price": price, "event": event["title"], "url": event["url"]})
    return obs


def discover(pm, today: dt.date) -> list:
    obs = []

    def fetch_event(slug):
        try:
            return pm.event(slug)
        except pm.PolymarketError as exc:
            log.warning("event %s unavailable: %s", slug, exc)
            return None

    largest = fetch_event(f"largest-company-end-of-december-{today.year}")
    if largest:
        obs += _observations(largest, "largest", None)

    month = today.strftime("%B").lower()
    for tkr, name in COMPANIES.items():
        ladder = fetch_event(f"what-price-will-{tkr.lower()}-hit-in-{month}-{today.year}")
        if ladder:
            obs += _observations(ladder, "pricehit", tkr)
        try:
            for e in pm.search(f"{name} beat quarterly earnings", limit=10):
                if e["slug"].startswith(f"{tkr.lower()}-quarterly-earnings-"):
                    obs += _observations(e, "earnings", tkr)
            for e in pm.search(f"{name} market cap end of", limit=10):
                # Title check keeps "Largest Company" and other firms' caps out.
                if "market-cap-end-of" in e["slug"] and e["title"].lower().startswith(name.lower()):
                    obs += _observations(e, "mcap", tkr)
        except pm.PolymarketError as exc:
            log.warning("%s search failed: %s", tkr, exc)
    return obs


# ---------------------------------------------------------------------------
# Pure logic (stdlib only)
# ---------------------------------------------------------------------------
def find_alerts(obs: list, state: dict, today: str) -> list:
    """Alert entries for moved markets and newly listed earnings markets."""
    alerts = []
    for o in obs:
        prev = state.get(o["key"])
        if prev is None:
            if o["family"] == "earnings":
                alerts.append({**o, "prev": None, "move": None})
            continue
        if prev["date"] == today:
            continue
        move = (o["price"] - prev["price"]) * 100
        if abs(move) >= MIN_MOVE_PTS[o["family"]]:
            alerts.append({**o, "prev": prev["price"], "move": move})
    # New earnings markets first, then biggest moves.
    alerts.sort(key=lambda a: (a["move"] is not None, -abs(a["move"] or 0)))
    return alerts


def _pct(p: float) -> str:
    return "<1%" if p < 0.01 else ">99%" if p > 0.99 else f"{round(p * 100)}%"


def format_alert(a: dict) -> str:
    fam = FAMILY_YUE[a["family"]]
    if a["move"] is None:
        return f"{a['ticker']} {fam}新盤：{a['event']} → Yes {_pct(a['price'])}"
    label = "" if a["family"] in ("earnings", "largest") else f" {a['label']}"
    return (f"{a['ticker']} {fam}{label}：{_pct(a['prev'])} → {_pct(a['price'])}"
            f"（{a['move']:+.0f}點）")


def update_state(state: dict, obs: list, today: str, held: set) -> dict:
    for o in obs:
        if o["key"] in held:
            continue  # alert didn't send: keep yesterday's baseline so it re-raises
        if state.get(o["key"], {}).get("date") == today:
            continue
        state[o["key"]] = {"date": today, "price": o["price"], "ticker": o["ticker"],
                           "family": o["family"], "label": o["label"]}
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=STATE_KEEP_DAYS)).isoformat()
    return {k: v for k, v in state.items() if v["date"] >= cutoff}


# ---------------------------------------------------------------------------
def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    # Write-then-rename, as in pricewatch: a torn file would read back as {}
    # and silently reset every baseline.
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def main() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, SKILL_DIR)
    import polymarket as pm
    from notify import notify

    dry = "--dry" in sys.argv
    today_nz = dt.datetime.now(NZ_TZ).date()
    today = today_nz.isoformat()
    obs = discover(pm, today_nz)
    by_family = {f: sum(o["family"] == f for o in obs) for f in MIN_MOVE_PTS}
    log.info("observed %d markets %s", len(obs), by_family)
    if not obs:
        # Every family empty means the API or the skill broke, not a quiet day.
        log.error("no markets observed")
        if not dry:
            notify("Polymarket 監察壞咗", f"今日一個盤都攞唔到，睇下 asr/logs/polywatch-{today}.log", priority=4)
        return

    state = _load_state()
    alerts = find_alerts(obs, state, today)
    held = set()
    if alerts:
        lines = [format_alert(a) for a in alerts[:MAX_LINES]]
        if len(alerts) > MAX_LINES:
            lines.append(f"仲有 {len(alerts) - MAX_LINES} 個")
        message = "\n".join(lines)
        log.info("alerts:\n%s", message)
        if dry:
            print(message)
        elif not notify("Polymarket 監察", message, priority=3):
            log.warning("ntfy failed -- %d baselines held for next run", len(alerts))
            held = {a["key"] for a in alerts}
    else:
        log.info("no moves over threshold")
        if dry:
            print("no alerts")
    if dry:
        print(f"observed {len(obs)} markets {by_family}")
        return
    _save_state(update_state(state, obs, today, held))


if __name__ == "__main__":
    main()
