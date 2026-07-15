"""Push a ntfy alert when the cheapest 3L milk is cheaper than yesterday.

Runs daily via the "VoiceOS Milk Watch" scheduled task as the logged-in user
(same isolation as notion_sync: the ntfy topic never enters the VoiceASR
service environment). Reuses the briefing's _milk_drop_line() — silent unless
today's cheapest standard 3L beats its last pre-today grocer history price.
"""

import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "asr", "logs", "milk_watch.log")
sys.path.insert(0, os.path.join(ROOT, "asr"))
sys.path.insert(0, os.path.join(ROOT, "ops"))

logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("milk_watch")


def main() -> None:
    from router import _milk_drop_line
    from notify import notify

    try:
        line = _milk_drop_line()
    except Exception:
        log.exception("milk price check failed")
        return
    if line is None:
        log.info("no drop")
        return
    sent = notify("牛奶減價", line, priority=3)
    log.info("drop alert %s: %s", "sent" if sent else "NOT sent", line)


if __name__ == "__main__":
    main()
