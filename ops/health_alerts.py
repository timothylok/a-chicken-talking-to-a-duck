"""Fasting / weight / workout alerts, pushed to ntfy and Discord.

Runs every minute via the "VoiceOS Health Alerts" scheduled task as the
logged-in user (same isolation as reminder_alerts.py: the ntfy topic and
Discord webhook never enter the VoiceASR service environment). Reads
asr/logs/health.json, which only the router writes; keeps its own sent-keys in
asr/logs/health_alerts.json, so there is no cursor on history.jsonl.

  stop-eating   once, when the open window reaches start + 8h (dropped if >2 h
                late, e.g. the laptop was asleep -- a stale "stop eating" is noise)
  morning       Mon-Fri from 10:00 (until 14:00): weight and/or workout not yet
                logged today; skipped entirely when both are logged
  pace          Thu-Sun from 10:00: fewer than 5 workouts reachable this week
  weekly        Sunday from 20:00: workouts, weight trend, fasting compliance

--dry prints what would be sent without sending or saving; --now YYYY-MM-DDTHH:MM
pretends it is that NZ time (for --dry tests).
"""

import datetime as dt
import json
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "asr", "logs", "health_alerts.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", f"health_alerts-{dt.date.today():%Y-%m-%d}.log")
sys.path.insert(0, os.path.join(ROOT, "asr"))
sys.path.insert(0, os.path.join(ROOT, "ops"))

import health as hl  # noqa: E402

STOP_MAX_LATE = dt.timedelta(hours=2)
PROMPT_FROM, PROMPT_UNTIL = dt.time(10, 0), dt.time(14, 0)
WEEKLY_FROM = dt.time(20, 0)

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("health_alerts")


def _load_sent() -> list:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f).get("sent", [])
    except (OSError, ValueError):
        return []


def _save_sent(sent: list) -> None:
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"sent": sent[-200:]}, f, ensure_ascii=False)


def _push(title: str, message: str, priority: int, dry: bool) -> bool:
    if dry:
        print(f"[dry] {title} | {message}")
        return True
    from notify import notify, notify_discord
    a = notify(title, message, priority)
    b = notify_discord(title, message)
    return a or b


def pace_line(state: dict, today: dt.date) -> "str | None":
    """Behind-pace sentence, or None when 5 workouts are still comfortably reachable."""
    done = hl.week_workouts(state, today)
    need = hl.WEEKLY_WORKOUTS - done
    if need <= 0:
        return None
    after = 6 - today.weekday()  # days left in the Mon-Sun week after today
    avail = after + (0 if state["workouts"].get(today.isoformat()) else 1)
    if need < avail:
        return None
    return f"今個禮拜做咗{done}次運動，仲差{need}次，剩返{avail}日"


def weekly_summary(state: dict, now: dt.datetime) -> str:
    today = now.date()
    ws = hl.week_start(today)
    rows = [r for d, r in hl.day_rows(state, now).items()
            if ws <= dt.date.fromisoformat(d) <= today]
    parts = [f"運動{hl.week_workouts(state, today)}/{hl.WEEKLY_WORKOUTS}次"]
    tracked = [r for r in rows if r["start"]]
    if tracked:
        ok = sum(1 for r in tracked if r["window_ok"] is not False and r["fast_ok"] is not False)
        parts.append(f"斷食達標{ok}/{len(tracked)}日")
    this = [r["weight"] for r in rows if r["weight"] is not None]
    if this:
        avg = sum(this) / len(this)
        prev_ws = ws - dt.timedelta(days=7)
        prev = [w for d, w in state["weights"].items()
                if prev_ws <= dt.date.fromisoformat(d) < ws]
        text = f"平均體重{avg:.1f}公斤"
        if prev:
            diff = avg - sum(prev) / len(prev)
            text += f"，比上個禮拜{'輕' if diff < 0 else '重'}{abs(diff):.1f}"
        parts.append(text)
        parts.append(f"最新{this[-1]:g}公斤，仲差{max(0.0, this[-1] - hl.TARGET_WEIGHT):.1f}公斤到{hl.TARGET_WEIGHT:g}")
    return "，".join(parts)


def run(now: dt.datetime, dry: bool = False) -> None:
    state = hl.load()
    sent = _load_sent()
    today = now.date()
    todo = []  # (key, title, message, priority)
    # Nothing logged yet: pace nudges and the weekly summary would only report an empty week.
    started = bool(state["windows"] or state["weights"] or state["workouts"])

    last = state["windows"][-1] if state["windows"] else None
    if last and not last.get("end"):
        start = hl._t(last["start"])
        planned = start + dt.timedelta(hours=hl.WINDOW_HOURS)
        key = f"stop:{last['start']}"
        if now >= planned and key not in sent:
            if now - planned > STOP_MAX_LATE:
                log.warning("stop-eating alert dropped (>2h late): %s", last["start"])
                sent.append(key)
            else:
                todo.append((key, "食完喇", hl.stop_alert_text(start), 4))

    if PROMPT_FROM <= now.time() < PROMPT_UNTIL:
        weekday = today.weekday()
        if weekday < 5 and f"morning:{today}" not in sent:
            missing = []
            if str(today) not in state["weights"]:
                missing.append("體重")
            if not state["workouts"].get(str(today)):
                missing.append("運動")
            if missing:
                todo.append((f"morning:{today}", "記錄",
                             "仲未記" + "同".join(missing) + "，講「體重 88.5」或者「做完運動」", 3))
            else:
                sent.append(f"morning:{today}")
        if weekday >= 3 and started and f"pace:{today}" not in sent:
            line = pace_line(state, today)
            if line:
                todo.append((f"pace:{today}", "運動進度", line, 3))

    if started and today.weekday() == 6 and now.time() >= WEEKLY_FROM:
        key = f"week:{hl.week_start(today)}"
        if key not in sent:
            todo.append((key, "今個禮拜總結", weekly_summary(state, now), 3))

    for key, title, message, priority in todo:
        if _push(title, message, priority, dry):
            sent.append(key)
            log.info("sent %s: %s", key, message)
        else:
            log.warning("NOT sent (no config or network error), will retry: %s", key)
    if not dry:
        _save_sent(sent)


def main() -> None:
    dry = "--dry" in sys.argv
    now = dt.datetime.now(hl.NZ_TZ)
    if "--now" in sys.argv:
        now = dt.datetime.fromisoformat(sys.argv[sys.argv.index("--now") + 1]).replace(tzinfo=hl.NZ_TZ)
    run(now, dry)


if __name__ == "__main__":
    main()
