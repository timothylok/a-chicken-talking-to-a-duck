"""Push a ntfy alert when the cheapest 3L milk is cheaper than yesterday.

Runs daily via the "VoiceOS Milk Watch" scheduled task as the logged-in user
(same isolation as notion_sync: the ntfy topic never enters the VoiceASR
service environment). Reuses the briefing's _milk_drop_line() — silent unless
today's cheapest standard 3L beats its last pre-today grocer history price.

Dedup guard (added 2026-07-29): the upstream grocer-nz `history` dataset
sometimes stalls for days (confirmed live: stuck at a 2026-07-27 snapshot
across every watched store while the live `prices` table kept updating),
which made _milk_drop_line() re-report the exact same "drop" every morning
since its "yesterday" baseline never advanced. STATE_PATH remembers the
last alert actually sent and skips an identical repeat; a "no drop" day
clears it, so a genuinely new drop (even one that happens to produce the
same wording) always alerts.
"""

import json
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "asr", "logs", "milk_watch.log")
STATE_PATH = os.path.join(ROOT, "asr", "logs", "milk_watch_state.json")
sys.path.insert(0, os.path.join(ROOT, "asr"))
sys.path.insert(0, os.path.join(ROOT, "ops"))

logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("milk_watch")


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def main() -> None:
    from router import _milk_drop_line
    from notify import notify

    try:
        line = _milk_drop_line()
    except Exception:
        log.exception("milk price check failed")
        return

    if line is None:
        if _load_state():
            _save_state({})
        log.info("no drop")
        return

    if _load_state().get("last_line") == line:
        log.info("drop unchanged since last alert, skipping: %s", line)
        return

    sent = notify("牛奶減價", line, priority=3)
    log.info("drop alert %s: %s", "sent" if sent else "NOT sent", line)
    if sent:
        _save_state({"last_line": line})


if __name__ == "__main__":
    main()
