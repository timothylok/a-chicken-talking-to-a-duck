"""Record the national average NZ petrol / diesel price once a day to Notion.

Runs daily via the "VoiceOS Fuel Watch" scheduled task as the logged-in user
(the Notion key stays out of the VoiceASR service, as with notion_sync.py).
Source is the TheColab `gaspy-nz` skill (Gaspy's public crowd-sourced national
averages, no login). The petrolmate skill behind FUEL_PRICES returns zero NZ
stations since at least 2026-10-19, and Gaspy's station search needs a login,
so this is a national average, not a Glenfield one.

Own daily record in asr/logs/fuelwatch_state.json ({date: {fuel: $/L}}) so the
7-day average does not depend on Notion. One Notion row per date, upserted by
title, so a same-day rerun updates the row instead of duplicating it. A failed
fetch or Notion call records nothing for that run and exits 1.

Setup:  python ops/fuelwatch.py --setup [parent_page_id]
        (creates the database beside the health database unless a parent is
        given, saves fuel_database_id in ops/notion.json)
Until configured, runs are silent no-ops. --dry prints without saving.
"""

import datetime as dt
import json
import logging
import os
import subprocess
import sys
import zoneinfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "asr", "logs", "fuelwatch_state.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", f"fuelwatch-{dt.date.today():%Y-%m-%d}.log")
GASPY_CLI = "D:/ai/thecolab-skills/skills/gaspy-nz/scripts/cli.py"
NZ_TZ = zoneinfo.ZoneInfo("Pacific/Auckland")
FUELS = {"91": "91", "95": "95", "diesel": "Diesel"}
sys.path.insert(0, os.path.join(ROOT, "ops"))

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("fuelwatch")

import notion_sync as ns  # noqa: E402  (reuses its Notion HTTP helper and config loader)


def fetch_prices() -> dict:
    out = subprocess.run(
        [sys.executable, GASPY_CLI, "prices", "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=True,
    ).stdout
    by_fuel = {p["fuel"]: p for p in json.loads(out)["prices"]}
    missing = [f for f in FUELS if f not in by_fuel]
    if missing:
        raise RuntimeError(f"gaspy returned no price for {missing}")
    return {f: round(by_fuel[f]["average"] / 100, 3) for f in FUELS} | {
        "stations_95": by_fuel["95"]["station_count"],
    }


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _avg7(state: dict, date: str, fuel: str) -> float:
    d = dt.date.fromisoformat(date)
    vals = [v[fuel] for k, v in state.items()
            if d - dt.timedelta(days=6) <= dt.date.fromisoformat(k) <= d]
    return round(sum(vals) / len(vals), 3)


def row_properties(date: str, state: dict) -> dict:
    today = state[date]
    props = {
        "Day": {"title": ns._rich(date)},
        "Date": {"date": {"start": date}},
        "95 stations": {"number": today["stations_95"]},
    }
    for fuel, label in FUELS.items():
        props[f"{label} $/L"] = {"number": today[fuel]}
        props[f"{label} 7-day avg"] = {"number": _avg7(state, date, fuel)}
    return props


def create_database(parent_page_id: str | None, cfg: dict) -> str:
    if not parent_page_id:
        health = ns._notion("GET", f"/databases/{cfg['health_database_id']}", {}, cfg["api_key"])
        parent_page_id = health["parent"]["page_id"]
    props = {"Day": {"title": {}}, "Date": {"date": {}}, "95 stations": {"number": {"format": "number"}}}
    for label in FUELS.values():
        props[f"{label} $/L"] = {"number": {"format": "number"}}
        props[f"{label} 7-day avg"] = {"number": {"format": "number"}}
    return ns._notion("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": "油價 NZ Fuel Prices"}}],
        "properties": props,
    }, cfg["api_key"])["id"]


def run(dry: bool = False) -> None:
    cfg = ns.load_config()
    if not dry and (not cfg or not cfg.get("api_key") or not cfg.get("fuel_database_id")):
        log.info("not configured (fuel_database_id missing); skipping")
        return
    date = dt.datetime.now(NZ_TZ).date().isoformat()
    state = _load_state()
    state[date] = fetch_prices()
    props = row_properties(date, state)
    if dry:
        print(date, json.dumps(props, ensure_ascii=False, indent=1))
        return
    db = cfg["fuel_database_id"]
    found = ns._notion("POST", f"/databases/{db}/query", {
        "filter": {"property": "Day", "title": {"equals": date}}, "page_size": 1,
    }, cfg["api_key"])["results"]
    if found:
        ns._notion("PATCH", f"/pages/{found[0]['id']}", {"properties": props}, cfg["api_key"])
    else:
        ns._notion("POST", "/pages", {"parent": {"database_id": db}, "properties": props}, cfg["api_key"])
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)
    log.info("recorded %s: %s", date, state[date])


def main() -> None:
    if "--setup" in sys.argv:
        cfg = ns.load_config()
        if not cfg or not cfg.get("api_key"):
            print("ops/notion.json needs an api_key first")
            sys.exit(1)
        rest = [a for a in sys.argv[2:] if not a.startswith("--")]
        cfg["fuel_database_id"] = create_database(rest[0] if rest else None, cfg)
        with open(ns.CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print(f"database created and saved to config: {cfg['fuel_database_id']}")
        return
    try:
        run(dry="--dry" in sys.argv)
    except Exception as exc:
        log.error("fuelwatch stopped: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
