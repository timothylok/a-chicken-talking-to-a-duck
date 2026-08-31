"""Mirror today+tomorrow's Google Calendar events to asr/cache/calendar.json.

Runs every 15 min via the "VoiceOS Calendar Sync" scheduled task as the
logged-in user, so the secret iCal URL never enters the VoiceASR service
environment (CLAUDE.md credential-isolation item). The SCHEDULE_TODAY voice
command only reads the cache file — it never fetches.

Setup:
  1. Google Calendar -> Settings -> Settings for my calendar -> your calendar
     -> Integrate calendar -> copy "Secret address in iCal format".
  2. Put it in the gitignored .env at the repo root:
        GOOGLE_ICAL_URL=https://calendar.google.com/calendar/ical/.../basic.ics
  3. Register the scheduled task (ops/register_calendar_sync.ps1, elevated).

Until GOOGLE_ICAL_URL is set, runs are silent no-ops. A failed fetch leaves
the existing cache untouched and exits non-zero, so a stale-but-valid agenda
keeps working and the task history shows the failure.
"""

import datetime as dt
import json
import logging
import os
import sys
import urllib.request
import zoneinfo

import icalendar
import recurring_ical_events

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")
CACHE = os.path.join(ROOT, "asr", "cache", "calendar.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", "calendar_sync.log")
NZ_TZ = zoneinfo.ZoneInfo("Pacific/Auckland")

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("calendar_sync")


def _load_env(path: str) -> dict:
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                env[key.strip()] = val.strip()
    except OSError:
        pass
    return env


def _event_record(comp) -> dict:
    summary = str(comp.get("SUMMARY", "")).strip() or "（無題）"
    start = comp.get("DTSTART").dt
    end_prop = comp.get("DTEND")
    if isinstance(start, dt.datetime):
        if start.tzinfo is None:
            start = start.replace(tzinfo=NZ_TZ)
        start_s = start.astimezone(NZ_TZ).isoformat()
        if end_prop is not None and isinstance(end_prop.dt, dt.datetime):
            end = end_prop.dt
            if end.tzinfo is None:
                end = end.replace(tzinfo=NZ_TZ)
            end_s = end.astimezone(NZ_TZ).isoformat()
        else:
            end_s = start_s
        return {"title": summary, "start": start_s, "end": end_s, "all_day": False}
    # all-day: DTSTART/DTEND are dates, DTEND is exclusive
    end_s = (end_prop.dt if end_prop is not None
             else start + dt.timedelta(days=1)).isoformat()
    return {"title": summary, "start": start.isoformat(), "end": end_s, "all_day": True}


def _sort_key(ev: dict) -> tuple:
    # group by calendar date, all-day before timed, then by start time
    return (ev["start"][:10], 0 if ev["all_day"] else 1, ev["start"])


def main() -> int:
    url = (_load_env(ENV_PATH).get("GOOGLE_ICAL_URL")
           or os.environ.get("GOOGLE_ICAL_URL", "")).strip()
    if not url:
        log.info("no GOOGLE_ICAL_URL configured — no-op")
        return 0

    req = urllib.request.Request(
        url, headers={"User-Agent": "voice-ecosystem-calendar-sync"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except Exception as exc:
        log.error("fetch failed, keeping existing cache: %s", exc)
        return 1

    if not raw.lstrip().startswith(b"BEGIN:VCALENDAR"):
        log.error("feed did not return an iCalendar body (%d bytes), keeping cache",
                  len(raw))
        return 1

    now = dt.datetime.now(NZ_TZ)
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = window_start + dt.timedelta(days=2)
    try:
        cal = icalendar.Calendar.from_ical(raw)
        occurrences = recurring_ical_events.of(cal).between(window_start, window_end)
        events = sorted((_event_record(c) for c in occurrences), key=_sort_key)
    except Exception as exc:
        log.error("parse failed, keeping existing cache: %s", exc)
        return 1

    payload = {"fetched_at": now.isoformat(), "events": events}
    tmp = CACHE + ".tmp"
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CACHE)
    log.info("wrote %d event(s) for %s .. %s",
             len(events), window_start.date(), window_end.date())
    return 0


if __name__ == "__main__":
    sys.exit(main())
