"""Daily Auckland <-> Queenstown round-trip fare watch (1 adult, carry-on fares).

Departures 29 Dec 2026 .. 11 Jan 2027, return 4-6 days after departure (a "5 day
trip" +/-1), nothing later than 15 Jan 2027. Cheapest fare per leg per day across
Air NZ and Jetstar (airlines may be mixed across legs), then the cheapest
(depart, return) pair. Jetstar is one fare-cache call per direction; Air NZ is
one call per date (TheColab skills, no login). Air NZ answers a request-auth /
CAPTCHA wall with schedules only (`fare_search_blocked`) -- that day counts as
"no Air NZ price", never as a price.

Pushes one ntfy + Discord alert when the best total is a new low, or at least
DROP_PCT below the last recorded day; the first run reports the current best.
Own daily state in asr/logs/flightwatch_state.json (same-day reruns do not
rebaseline; a failed push holds the baseline). A run where most legs come back
empty pushes a "tracker broken" alert instead of reporting silence.

Run: python ops/flightwatch.py          (scheduled task "VoiceOS Flight Watch")
     python ops/flightwatch.py --dry    (print, no push, no state write)
"""

import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "asr", "logs", "flightwatch_state.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", f"flightwatch-{dt.date.today():%Y-%m-%d}.log")
NZ_TZ = ZoneInfo("Pacific/Auckland")

SKILLS = "D:/ai/thecolab-skills/skills"
JETSTAR_CLI = f"{SKILLS}/jetstar-flights/scripts/cli.mjs"
AIRNZ_CLI = f"{SKILLS}/airnz-flights/scripts/cli.py"

HOME, DEST = "AKL", "ZQN"
FIRST_DEPART = dt.date(2026, 12, 29)
LAST_RETURN = dt.date(2027, 1, 15)
TRIP_DAYS = (4, 5, 6)
DROP_PCT = 0.05
AIRNZ_PAUSE_S = 1.5
STATE_KEEP_DAYS = 60

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("flightwatch")


def daterange(a: dt.date, b: dt.date) -> list:
    return [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]


OUT_DATES = daterange(FIRST_DEPART, LAST_RETURN - dt.timedelta(days=min(TRIP_DAYS)))
RET_DATES = daterange(FIRST_DEPART + dt.timedelta(days=min(TRIP_DAYS)), LAST_RETURN)


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

def best_trips(out: dict, ret: dict, limit: int = 5) -> list:
    """out/ret: {date: (price, airline)}. Cheapest (depart, return) pairs first."""
    trips = []
    for d, (p_out, a_out) in out.items():
        for n in TRIP_DAYS:
            r = d + dt.timedelta(days=n)
            if r > LAST_RETURN or r not in ret:
                continue
            p_ret, a_ret = ret[r]
            trips.append({"depart": d.isoformat(), "return": r.isoformat(),
                          "total": p_out + p_ret, "out_air": a_out, "ret_air": a_ret,
                          "out_price": p_out, "ret_price": p_ret})
    trips.sort(key=lambda t: (t["total"], t["depart"]))
    return trips[:limit]


def find_alert(best: dict, state: dict, today: str) -> "str | None":
    """Reason string if `best` deserves a push, else None. state: {"runs": {date: trip}}."""
    prior = {d: t for d, t in state.get("runs", {}).items() if d < today}
    if not prior:
        return "first check"
    last_day = max(prior)
    prev = prior[last_day]["total"]
    low = min(t["total"] for t in prior.values())
    if best["total"] < low:
        return f"new low (was ${low})"
    if best["total"] <= prev * (1 - DROP_PCT):
        return f"-{(prev - best['total']) / prev:.0%} vs {last_day} (${prev})"
    return None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _jetstar(origin: str, dest: str, start: dt.date, days: int) -> dict:
    out = subprocess.run(
        ["node", JETSTAR_CLI, "search", origin, dest, start.isoformat(), "--days", str(days), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    prices = {}
    for f in json.loads(out.stdout)["flights"]:
        if f.get("sold_out") or f.get("member") or not isinstance(f.get("price"), (int, float)):
            continue
        d = dt.date.fromisoformat(f["date"])
        if d not in prices or f["price"] < prices[d][0]:
            prices[d] = (f["price"], "Jetstar")
    return prices


def _airnz(origin: str, dest: str, day: dt.date) -> "tuple[float, str] | None":
    out = subprocess.run(
        [sys.executable, AIRNZ_CLI, origin, dest, day.isoformat(), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=90,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    data = json.loads(out.stdout)
    if data.get("fare_search_blocked"):
        return None
    prices = [fare["price"] for f in data.get("flights", []) for fare in f.get("fares", [])
              if fare.get("available") and isinstance(fare.get("price"), (int, float))]
    return (min(prices), "Air NZ") if prices else None


def _cheaper(a: "dict", b: "dict") -> dict:
    merged = dict(a)
    for d, v in b.items():
        if d not in merged or v[0] < merged[d][0]:
            merged[d] = v
    return merged


def fetch_leg(origin: str, dest: str, dates: list) -> dict:
    prices = {}
    try:
        prices = _jetstar(origin, dest, dates[0], len(dates))
    except Exception as exc:
        log.error("jetstar %s-%s failed: %s", origin, dest, exc)
    airnz = {}
    for d in dates:
        try:
            hit = _airnz(origin, dest, d)
            if hit:
                airnz[d] = hit
        except Exception as exc:
            log.error("airnz %s-%s %s failed: %s", origin, dest, d, exc)
        time.sleep(AIRNZ_PAUSE_S)
    log.info("%s-%s: jetstar %d/%d days, airnz %d/%d days", origin, dest,
             len(prices), len(dates), len(airnz), len(dates))
    return _cheaper({d: v for d, v in prices.items() if d in dates}, airnz)


# ---------------------------------------------------------------------------
# State + output
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    # Write-then-rename: a torn file would read back as {} and reset the baseline.
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _fmt(t: dict) -> str:
    d = dt.date.fromisoformat(t["depart"])
    r = dt.date.fromisoformat(t["return"])
    return (f"${t['total']:.0f}  {d:%a %d %b} → {r:%a %d %b}  "
            f"({t['out_air']} ${t['out_price']:.0f} + {t['ret_air']} ${t['ret_price']:.0f})")


def main() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from notify import notify, notify_discord

    dry = "--dry" in sys.argv
    today = dt.datetime.now(NZ_TZ).date().isoformat()
    out = fetch_leg(HOME, DEST, OUT_DATES)
    ret = fetch_leg(DEST, HOME, RET_DATES)
    # Fewer than half the days priced means a source broke, not a quiet day.
    if len(out) < len(OUT_DATES) / 2 or len(ret) < len(RET_DATES) / 2:
        log.error("most legs empty: out %d/%d, ret %d/%d", len(out), len(OUT_DATES), len(ret), len(RET_DATES))
        if not dry:
            notify("機票監察壞咗", f"AKL-ZQN 好多日攞唔到價，睇下 asr/logs/flightwatch-{today}.log", priority=4)
        return
    trips = best_trips(out, ret)
    if not trips:
        log.error("no round-trip combination priced")
        return
    best = trips[0]
    log.info("best: %s", _fmt(best))

    state = _load_state()
    reason = find_alert(best, state, today)
    held = False
    if reason:
        message = "\n".join([f"最平：{_fmt(best)}", f"原因：{reason}", "", "其他選擇："]
                            + [_fmt(t) for t in trips[1:4]]
                            + ["", "Air NZ / Jetstar 官網訂；呢個係未計行李嘅飛機票價"])
        log.info("alert (%s):\n%s", reason, message)
        if dry:
            print(message)
        else:
            held = not notify("AKL-ZQN 機票監察", message, priority=3)
            notify_discord("AKL-ZQN 機票監察", message)
            if held:
                log.warning("ntfy failed -- baseline held for next run")
    elif dry:
        print("no alert; best:", _fmt(best))
    if dry:
        return
    if held:
        return
    runs = state.setdefault("runs", {})
    runs.setdefault(today, best)
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=STATE_KEEP_DAYS)).isoformat()
    state["runs"] = {d: t for d, t in runs.items() if d >= cutoff}
    _save_state(state)


if __name__ == "__main__":
    main()
