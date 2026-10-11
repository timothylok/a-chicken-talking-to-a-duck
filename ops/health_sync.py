"""Mirror the daily fasting / weight / workout log to a Notion database.

Runs every 5 min via the "VoiceOS Health Sync" scheduled task as the
logged-in user (the Notion key stays out of the VoiceASR service, as with
notion_sync.py). Mirror only: asr/logs/health.json (written by the router) is
the source of truth and edits made in Notion are never read back.

One row per date. A row is created or PATCHed only when its properties changed
since the last run (hash kept in asr/logs/health_sync.json with the Notion page
id), so a quiet day costs no API calls. A failed call leaves that date's hash
unchanged and the next run retries it.

Setup:  python ops/health_sync.py --setup <parent_page_id>
        (creates the database, saves health_database_id in ops/notion.json)
Until configured, runs are silent no-ops. --dry prints the rows instead.
"""

import datetime as dt
import hashlib
import json
import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "asr", "logs", "health_sync.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", f"health_sync-{dt.date.today():%Y-%m-%d}.log")
sys.path.insert(0, os.path.join(ROOT, "asr"))
sys.path.insert(0, os.path.join(ROOT, "ops"))

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("health_sync")

import health as hl  # noqa: E402
import notion_sync as ns  # noqa: E402  (reuses its Notion HTTP helper and config loader)


def create_database(parent_page_id: str, api_key: str) -> str:
    result = ns._notion("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": "體重斷食運動 Weight Fasting Workout"}}],
        "properties": {
            "Day": {"title": {}},
            "Date": {"date": {}},
            "Eating start": {"rich_text": {}},
            "Eating end": {"rich_text": {}},
            "Eating hours": {"number": {"format": "number"}},
            "Fast hours": {"number": {"format": "number"}},
            "Window ≤8h": {"checkbox": {}},
            "Fast ≥16h": {"checkbox": {}},
            "Weight kg": {"number": {"format": "number"}},
            "7-day avg kg": {"number": {"format": "number"}},
            "To target kg": {"number": {"format": "number"}},
            "Workout": {"checkbox": {}},
        },
    }, api_key)
    return result["id"]


def _weight_avg(state: dict, date: str) -> "float | None":
    d = dt.date.fromisoformat(date)
    vals = [w for k, w in state["weights"].items()
            if d - dt.timedelta(days=6) <= dt.date.fromisoformat(k) <= d]
    return round(sum(vals) / len(vals), 2) if vals else None


def _num(v):
    return {"number": v}


def row_properties(row: dict, state: dict) -> dict:
    avg = _weight_avg(state, row["date"])
    props = {
        "Day": {"title": ns._rich(row["date"])},
        "Date": {"date": {"start": row["date"]}},
        "Eating start": {"rich_text": ns._rich(row["start"] or "")},
        "Eating end": {"rich_text": ns._rich(row["end"] or "")},
        "Eating hours": _num(row["span_hours"]),
        "Fast hours": _num(row["fast_hours"]),
        "Window ≤8h": {"checkbox": row["window_ok"] is True},
        "Fast ≥16h": {"checkbox": row["fast_ok"] is True},
        "Weight kg": _num(row["weight"]),
        "7-day avg kg": _num(avg),
        "To target kg": _num(round(row["weight"] - hl.TARGET_WEIGHT, 1) if row["weight"] is not None else None),
        "Workout": {"checkbox": row["workout"]},
    }
    return props


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(synced: dict) -> None:
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(synced, f, ensure_ascii=False)


def sync(dry: bool = False) -> int:
    cfg = ns.load_config()
    if not dry and (not cfg or not cfg.get("api_key") or not cfg.get("health_database_id")):
        log.info("not configured (health_database_id missing); skipping")
        return 0
    now = dt.datetime.now(hl.NZ_TZ)
    state = hl.load()
    rows = hl.day_rows(state, now)
    synced = _load_state()
    changed = 0
    for date, row in rows.items():
        props = row_properties(row, state)
        digest = hashlib.sha1(json.dumps(props, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        entry = synced.get(date, {})
        if entry.get("hash") == digest:
            continue
        if dry:
            print(date, json.dumps(row, ensure_ascii=False))
            changed += 1
            continue
        page_id = entry.get("page_id")
        if not page_id:  # state lost or first run: reuse an existing row for this date
            found = ns._notion("POST", f"/databases/{cfg['health_database_id']}/query", {
                "filter": {"property": "Day", "title": {"equals": date}}, "page_size": 1,
            }, cfg["api_key"])["results"]
            page_id = found[0]["id"] if found else None
        if page_id:
            ns._notion("PATCH", f"/pages/{page_id}", {"properties": props}, cfg["api_key"])
        else:
            page_id = ns._notion("POST", "/pages", {
                "parent": {"database_id": cfg["health_database_id"]}, "properties": props,
            }, cfg["api_key"])["id"]
        synced[date] = {"hash": digest, "page_id": page_id}
        _save_state(synced)
        changed += 1
        log.info("synced %s", date)
        time.sleep(0.4)  # Notion allows ~3 requests/s
    return changed


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--setup":
        cfg = ns.load_config()
        if not cfg or not cfg.get("api_key"):
            print("ops/notion.json needs an api_key first")
            sys.exit(1)
        db_id = create_database(sys.argv[2], cfg["api_key"])
        cfg["health_database_id"] = db_id
        with open(ns.CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print(f"database created and saved to config: {db_id}")
        return
    try:
        sync(dry="--dry" in sys.argv)
    except Exception as exc:
        log.error("sync stopped: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
