"""Enforce the transcript retention policy (SECURITY.md).

Runs daily via the "VoiceOS Log Prune" scheduled task as the logged-in user.
Policy (chosen 2026-07-16): chat entries in history.jsonl (command == null)
are kept 30 days; command entries are kept forever (Notion mirrors them);
rotated service-*.log files are kept 90 days.

history.jsonl is rewritten atomically, and only within the region the Notion
sync's byte-offset cursor has already passed — the cursor is then shifted by
the bytes removed, so nothing is ever double-synced or skipped. If the
service appends mid-prune, the run aborts and retries tomorrow.
"""

import datetime as dt
import glob
import json
import logging
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "asr", "logs")
HISTORY = os.path.join(LOGS, "history.jsonl")
CURSOR = os.path.join(LOGS, "notion_sync.cursor")
LOG_PATH = os.path.join(LOGS, "prune.log")

CHAT_DAYS = 30
ROTATED_LOG_DAYS = 90

logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("prune")


def prune_history() -> None:
    if not os.path.exists(HISTORY):
        return
    cursor = 0
    if os.path.exists(CURSOR):
        with open(CURSOR, encoding="ascii") as f:
            cursor = int(f.read().strip() or 0)
    cutoff = dt.datetime.now().astimezone() - dt.timedelta(days=CHAT_DAYS)
    size = os.path.getsize(HISTORY)
    kept, removed_bytes, dropped = [], 0, 0
    pos = 0
    with open(HISTORY, "rb") as f:
        for line in f:
            end = pos + len(line)
            drop = False
            # Only touch complete lines the Notion cursor has passed.
            if end <= cursor and line.endswith(b"\n"):
                try:
                    entry = json.loads(line.decode("utf-8"))
                    ts = dt.datetime.fromisoformat(entry["ts"])
                    drop = entry.get("command") is None and ts < cutoff
                except (ValueError, KeyError, UnicodeDecodeError):
                    drop = False  # never drop what we can't parse
            if drop:
                removed_bytes += len(line)
                dropped += 1
            else:
                kept.append(line)
            pos = end
    if not dropped:
        log.info("history: nothing to prune")
        return
    if os.path.getsize(HISTORY) != size:
        log.warning("history grew mid-prune; skipping this run")
        return
    tmp = HISTORY + ".tmp"
    with open(tmp, "wb") as f:
        f.writelines(kept)
    os.replace(tmp, HISTORY)
    if os.path.exists(CURSOR):
        with open(CURSOR, "w", encoding="ascii") as f:
            f.write(str(cursor - removed_bytes))
    log.info(
        "history: dropped %d chat entries older than %d days (%d bytes)",
        dropped, CHAT_DAYS, removed_bytes,
    )


def prune_rotated_logs() -> None:
    cutoff = time.time() - ROTATED_LOG_DAYS * 86400
    removed = 0
    for path in glob.glob(os.path.join(LOGS, "service-*.log")):
        if os.path.getmtime(path) < cutoff:
            os.remove(path)
            removed += 1
    log.info(
        "rotated logs: removed %d files older than %d days",
        removed, ROTATED_LOG_DAYS,
    )


if __name__ == "__main__":
    prune_history()
    prune_rotated_logs()
