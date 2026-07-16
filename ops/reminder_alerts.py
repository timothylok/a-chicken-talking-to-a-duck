"""Push an ntfy alert on the phone when a voice reminder falls due.

The iOS Reminder created by the Shortcut carries no alert (iOS 2026 Shortcuts
rejects dynamic alert times — see _create_reminder in asr/router.py), so this
task delivers the actual notification. Runs every minute via the "VoiceOS
Reminder Alerts" scheduled task as the logged-in user (same isolation as
notion_sync: the ntfy topic never enters the VoiceASR service environment).

Reads CREATE_REMINDER entries from asr/logs/history.jsonl with a byte cursor,
keeps not-yet-due reminders in a pending list, and pushes each via ops/notify
once its due time arrives. A failed push retries next run; reminders more than
24 h overdue (machine off, first run over old history) are dropped with a log
line instead of alerting late. State lives in asr/logs/reminder_alerts.json.
"""

import datetime as dt
import json
import logging
import os
import sys
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY = os.path.join(ROOT, "asr", "logs", "history.jsonl")
STATE = os.path.join(ROOT, "asr", "logs", "reminder_alerts.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", "reminder_alerts.log")
sys.path.insert(0, os.path.join(ROOT, "ops"))

NZ_TZ = ZoneInfo("Pacific/Auckland")
MAX_LATE = dt.timedelta(hours=24)

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("reminder_alerts")


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"offset": 0, "pending": []}


def _scan_history(state: dict, now: dt.datetime) -> None:
    if not os.path.exists(HISTORY):
        return
    if state["offset"] > os.path.getsize(HISTORY):  # truncated/rotated
        state["offset"] = 0
    with open(HISTORY, "rb") as f:
        f.seek(state["offset"])
        raw = f.read()
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break  # partially written line; pick it up next run
        state["offset"] += len(line)
        try:
            entry = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if entry.get("command") != "CREATE_REMINDER" or entry.get("status") != "executed":
            continue
        data = entry.get("data") or {}
        if not data.get("due"):
            continue
        due = dt.datetime.strptime(data["due"], "%Y-%m-%d %H:%M").replace(tzinfo=NZ_TZ)
        if now - due > MAX_LATE:  # old history (first run) — never alert
            continue
        state["pending"].append({"title": data["title"], "due": data["due"]})
        log.info("queued: %s @ %s", data["title"], data["due"])


def _send_due(state: dict, now: dt.datetime) -> None:
    from notify import notify

    keep = []
    for item in state["pending"]:
        due = dt.datetime.strptime(item["due"], "%Y-%m-%d %H:%M").replace(tzinfo=NZ_TZ)
        if due > now:
            keep.append(item)
        elif now - due > MAX_LATE:
            log.warning("dropped (>24h overdue): %s @ %s", item["title"], item["due"])
        elif notify("提醒", f"{item['title']}（{due:%H:%M}）", priority=4):
            log.info("alert sent: %s @ %s", item["title"], item["due"])
        else:
            keep.append(item)  # no config or network error; retry next run
            log.warning("alert NOT sent, will retry: %s @ %s", item["title"], item["due"])
    state["pending"] = keep


def main() -> None:
    now = dt.datetime.now(NZ_TZ)
    state = _load_state()
    before = json.dumps(state, sort_keys=True)
    _scan_history(state, now)
    _send_due(state, now)
    if json.dumps(state, sort_keys=True) != before:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
