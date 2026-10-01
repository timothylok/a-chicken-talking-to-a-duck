"""Daily HKG -> AKL round-trip fare tracker (direct flights, economy, 1 adult, HKD).

Searches every December 2026 departure x every 14-21 day trip length (248
round trips) on Google Flights once a day, stores the cheapest fare per
airline in SQLite, writes a markdown report to content/flight-tracker/, and
pushes one ntfy alert when any combination drops >10% day-over-day or hits a
new low.

Data source: Google Flights through the `fast-flights` library (pip install
fast-flights into asr/.venv). Chosen 2026-10-01 because every free official API
is gone or unusable for this: Amadeus Self-Service shut down 2026-07-17, Kiwi
Tequila has been invite-only since 2024, SerpApi's free tier (~100-250
searches/month) can't cover a 248-search daily grid, and Travelpayouts only
has fares other users searched in the last 48 h. fast-flights is unofficial --
it rebuilds Google's protobuf search URL -- so a Google page change breaks it;
a run where most searches come back empty pushes a "tracker broken" alert
instead of reporting silence as "no fares".

Prices are quoted by Google natively in HKD (curr=HKD), so there is no FX
conversion step. Google's round-trip result page lists outbound flights priced
at the full round-trip fare (verified: 2026-12-05 one-way HK$6,694 vs round
trip HK$13,218), and the airline recorded is the outbound carrier.

Run: python ops/flightwatch.py            (scheduled task "VoiceOS Flight Watch")
     python ops/flightwatch.py --report   (rebuild today's report from the DB, no searching)
"""

import datetime as dt
import logging
import os
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "asr", "logs", "flightwatch.db")
OUTPUT_DIR = os.path.join(ROOT, "content", "flight-tracker")
LOG_PATH = os.path.join(ROOT, "asr", "logs", f"flightwatch-{dt.date.today():%Y-%m-%d}.log")
NZ_TZ = ZoneInfo("Pacific/Auckland")

ORIGIN, DEST = "HKG", "AKL"
DEPARTURES = [dt.date(2026, 12, d) for d in range(1, 32)]
TRIP_LENGTHS = range(14, 22)
DROP_PCT = 0.10
# Peak: departures after ~18 Dec, or a return inside the Christmas/New Year window.
PEAK_DEPART_AFTER = dt.date(2026, 12, 18)
PEAK_RETURN = (dt.date(2026, 12, 24), dt.date(2027, 1, 5))
SEARCH_PAUSE_S = 2

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("flightwatch")
# fast-flights' HTTP client (primp) logs every request URL at INFO.
logging.getLogger("primp").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Pure logic (stdlib only -- covered by tests/pure_logic.py)
# ---------------------------------------------------------------------------

def is_peak(depart: dt.date, ret: dt.date) -> bool:
    return depart > PEAK_DEPART_AFTER or PEAK_RETURN[0] <= ret <= PEAK_RETURN[1]


def find_alerts(today: dict, history: dict) -> list:
    """today: {(dep, ret): price}. history: {(dep, ret): [(run_date, price), ...]}
    from earlier days only, oldest first. Returns alert dicts, cheapest first."""
    alerts = []
    for combo, price in today.items():
        prior = history.get(combo)
        if not prior:
            continue  # first sighting: nothing to compare against
        prev = prior[-1][1]
        reasons = []
        if price <= prev * (1 - DROP_PCT):
            reasons.append(f"-{(prev - price) / prev:.0%} vs {prior[-1][0]}")
        if price < min(p for _, p in prior):
            reasons.append("new low")
        if reasons:
            alerts.append({"combo": combo, "price": price, "prev": prev, "reasons": reasons})
    return sorted(alerts, key=lambda a: a["price"])


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS quotes (
        run_date TEXT, checked_at TEXT, depart TEXT, ret TEXT,
        airline TEXT, price_hkd INTEGER,
        PRIMARY KEY (run_date, depart, ret, airline))""")
    return con


def _cheapest_by_day(con) -> dict:
    """{run_date: {(dep, ret): cheapest price across airlines}}"""
    out = {}
    for run_date, dep, ret, price in con.execute(
            "SELECT run_date, depart, ret, MIN(price_hkd) FROM quotes GROUP BY run_date, depart, ret"):
        out.setdefault(run_date, {})[(dt.date.fromisoformat(dep), dt.date.fromisoformat(ret))] = price
    return out


def _airlines_on(con, run_date: str) -> dict:
    """{(dep, ret): 'CX 9,638 / NZ 10,238'} for one run."""
    out = {}
    for dep, ret, airline, price in con.execute(
            "SELECT depart, ret, airline, price_hkd FROM quotes WHERE run_date=? ORDER BY price_hkd",
            (run_date,)):
        key = (dt.date.fromisoformat(dep), dt.date.fromisoformat(ret))
        out.setdefault(key, []).append(f"{airline} {price:,}")
    return {k: " / ".join(v) for k, v in out.items()}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _search(depart: dt.date, ret: dt.date) -> dict:
    """{airline: cheapest round-trip HKD} for direct flights only."""
    from fast_flights import FlightQuery, create_query, get_flights

    q = create_query(
        flights=[FlightQuery(date=str(depart), from_airport=ORIGIN, to_airport=DEST),
                 FlightQuery(date=str(ret), from_airport=DEST, to_airport=ORIGIN)],
        trip="round-trip", seat="economy", max_stops=0, currency="HKD", language="en-US",
    )
    best = {}
    for f in get_flights(q):
        # max_stops=0 should already guarantee this; checked so a filter
        # Google stops honouring can't slip a one-stop fare into the data.
        if len(f.flights) != 1 or f.flights[0].from_airport.code != ORIGIN \
                or f.flights[0].to_airport.code != DEST or not f.price:
            continue
        airline = f.airlines[0] if f.airlines else "?"
        best[airline] = min(f.price, best.get(airline, f.price))
    return best


def run_searches(con, run_date: str, today_nz: dt.date) -> "tuple[int, int]":
    combos = [(d, d + dt.timedelta(days=n)) for d in DEPARTURES if d >= today_nz for n in TRIP_LENGTHS]
    checked_at = dt.datetime.now(NZ_TZ).isoformat(timespec="seconds")
    con.execute("DELETE FROM quotes WHERE run_date=?", (run_date,))  # same-day rerun replaces
    found = 0
    for i, (dep, ret) in enumerate(combos):
        if i:
            time.sleep(SEARCH_PAUSE_S)
        try:
            best = _search(dep, ret)
        except Exception as exc:
            log.warning("%s -> %s: search failed, retrying: %s", dep, ret, exc)
            time.sleep(10)
            try:
                best = _search(dep, ret)
            except Exception as exc:
                log.error("%s -> %s: search failed twice: %s", dep, ret, exc)
                continue
        if best:
            found += 1
        con.executemany("INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?)",
                        [(run_date, checked_at, str(dep), str(ret), a, p) for a, p in best.items()])
    con.commit()
    log.info("searched %d combos, %d with direct fares", len(combos), found)
    return len(combos), found


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _label(dep: dt.date, ret: dt.date) -> str:
    return f"{dep:%a %d %b} → {ret:%a %d %b} ({(ret - dep).days}d)"


def build_report(con, run_date: str, searched: "int | None", alerts: list) -> str:
    by_day = _cheapest_by_day(con)
    today = by_day.get(run_date, {})
    airlines = _airlines_on(con, run_date)
    days = sorted(by_day)
    prev_day = max((d for d in days if d < run_date), default=None)
    prev = by_day.get(prev_day, {})

    def money(p):
        return f"HK${p:,}" if p is not None else "—"

    lines = [f"# HKG → AKL direct return fares — {run_date}", ""]
    lines.append(f"Checked {dt.datetime.now(NZ_TZ):%Y-%m-%d %H:%M} NZT · economy · 1 adult · "
                 f"non-stop only (Cathay Pacific / Air New Zealand)  ")
    lines.append("Source: Google Flights (via `fast-flights`), prices quoted natively in HKD — no FX conversion.  ")
    if searched is not None:
        lines.append(f"Searched {searched} date combinations; {len(today)} returned direct fares.")
    lines += ["", "† = peak (departs after 18 Dec, or returns 24 Dec – 5 Jan).", ""]

    off_peak = sorted((p, c) for c, p in today.items() if not is_peak(*c))
    peak = sorted((p, c) for c, p in today.items() if is_peak(*c))
    lines.append("## Best windows today")
    lines.append("")
    if off_peak:
        p, c = off_peak[0]
        lines.append(f"- **Cheapest off-peak:** {_label(*c)} — **{money(p)}** ({airlines.get(c, '')})")
    if peak:
        p, c = peak[0]
        lines.append(f"- Cheapest peak†: {_label(*c)} — {money(p)} ({airlines.get(c, '')})")
    if off_peak and peak:
        lines.append(f"- Off-peak saving vs cheapest peak: {money(peak[0][0] - off_peak[0][0])}")
    lines.append("")

    lines.append("## Alerts today")
    lines.append("")
    if alerts:
        lines += [f"- {_label(*a['combo'])}{' †' if is_peak(*a['combo']) else ''}: "
                  f"{money(a['prev'])} → **{money(a['price'])}** ({', '.join(a['reasons'])})"
                  for a in alerts]
    else:
        lines.append("None" + ("" if prev_day else " (first day of data — nothing to compare yet)."))
    lines.append("")

    lines.append("## Fare calendar (cheapest round trip, HKD)")
    lines.append("")
    lines.append("| Depart | " + " | ".join(f"{n}d" for n in TRIP_LENGTHS) + " | Best |")
    lines.append("|---|" + "---:|" * (len(TRIP_LENGTHS) + 1))
    for dep in DEPARTURES:
        cells, row = [], []
        for n in TRIP_LENGTHS:
            c = (dep, dep + dt.timedelta(days=n))
            p = today.get(c)
            if p is not None:
                row.append(p)
            cells.append("—" if p is None else f"{p:,}{'†' if is_peak(*c) else ''}")
        best = min(row) if row else None
        lines.append(f"| {dep:%a %d %b} | " + " | ".join(cells) + f" | {money(best)} |")
    lines.append("")

    lines.append("## Top 15 off-peak combinations")
    lines.append("")
    lines.append("| Dates | Today | vs yesterday | Airlines |")
    lines.append("|---|---:|---:|---|")
    for p, c in off_peak[:15]:
        y = prev.get(c)
        delta = "—" if y is None else f"{p - y:+,}"
        lines.append(f"| {_label(*c)} | {money(p)} | {delta} | {airlines.get(c, '')} |")
    lines.append("")

    lines.append("## Price history per combination")
    lines.append("")
    lines.append(f"{len(days)} day(s) of data, {days[0] if days else '—'} to {days[-1] if days else '—'}. "
                 "Sorted by today's price.")
    lines.append("")
    lines.append("| Dates | Peak | Days | Low (date) | High | Today | vs yesterday |")
    lines.append("|---|:-:|---:|---|---:|---:|---:|")
    for p, c in sorted((p, c) for c, p in today.items()):
        series = [(d, by_day[d][c]) for d in days if c in by_day[d]]
        low_d, low_p = min(series, key=lambda x: x[1])
        high = max(x[1] for x in series)
        y = prev.get(c)
        delta = "—" if y is None else f"{p - y:+,}"
        lines.append(f"| {_label(*c)} | {'†' if is_peak(*c) else ''} | {len(series)} | "
                     f"{low_p:,} ({low_d}) | {high:,} | {p:,} | {delta} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from notify import notify

    report_only = "--report" in sys.argv
    today_nz = dt.datetime.now(NZ_TZ).date()
    run_date = str(today_nz)
    con = _db()
    searched = None
    if not report_only:
        if today_nz > DEPARTURES[-1]:
            log.info("all departure dates are past -- nothing to search")
            return
        searched, found = run_searches(con, run_date, today_nz)
        if found * 2 < searched:
            notify("Flight tracker broken",
                   f"Only {found}/{searched} HKG-AKL searches returned fares -- Google Flights "
                   f"may have changed. Check asr/logs/flightwatch-{run_date}.log", priority=4)

    by_day = _cheapest_by_day(con)
    history = {}
    for d in sorted(by_day):
        if d < run_date:
            for c, p in by_day[d].items():
                history.setdefault(c, []).append((d, p))
    alerts = find_alerts(by_day.get(run_date, {}), history)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"{run_date}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_report(con, run_date, searched, alerts))
    log.info("wrote %s (%d alerts)", out_path, len(alerts))

    if alerts and not report_only:
        top = [f"{a['combo'][0]:%d %b}->{a['combo'][1]:%d %b}{' (peak)' if is_peak(*a['combo']) else ''}"
               f" HK${a['price']:,} ({', '.join(a['reasons'])})" for a in alerts[:5]]
        more = f"\n+{len(alerts) - 5} more" if len(alerts) > 5 else ""
        notify(f"HKG-AKL fares down: {len(alerts)} date pair(s)",
               "\n".join(top) + more + f"\nReport: content/flight-tracker/{run_date}.md")


if __name__ == "__main__":
    main()
