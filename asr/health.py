"""Fasting (16/8), weight and workout tracking: parsing, state and replies.

Stdlib-only so the router (VoiceASR service) and the ops/ scheduled tasks
(health_alerts.py, health_sync.py) share one implementation. The router is the
only writer of asr/logs/health.json; the ops tasks only read it and keep their
own state files, so there are no byte cursors to keep in step with pruning.

Everything here is deterministic: numbers and times are parsed with regexes,
never by an LLM (a fasting log must not be hallucinated).
"""

import datetime as dt
import json
import os
import re
import threading
import zoneinfo

NZ_TZ = zoneinfo.ZoneInfo("Pacific/Auckland")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "health.json")

WINDOW_HOURS = 8          # max eating span
FAST_HOURS = 16           # min fast between windows
WEEKLY_WORKOUTS = 5
START_WEIGHT = 89.0
TARGET_WEIGHT = 80.0
MIN_KG, MAX_KG = 40.0, 200.0

_lock = threading.Lock()

_ZH_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


# --- number / time parsing -------------------------------------------------

def zh_int(s: str) -> "int | None":
    """Arabic digits or Chinese numerals up to 99 (十二, 二十五, 兩, 8)."""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not s or any(c not in _ZH_DIGITS and c != "十" for c in s):
        return None
    if "十" not in s:
        return _ZH_DIGITS[s] if len(s) == 1 else None
    head, _, tail = s.partition("十")
    tens = _ZH_DIGITS[head] if head else 1
    if head and head not in _ZH_DIGITS:
        return None
    if tail and (len(tail) != 1 or tail not in _ZH_DIGITS):
        return None
    return tens * 10 + (_ZH_DIGITS[tail] if tail else 0)


_NUM = r"(?:\d+|[零〇一二兩两三四五六七八九十]+)"
_PERIOD = r"(朝早|朝頭早|早上|早晨|上午|凌晨|半夜|下晝|下午|晏晝|中午|晚上|夜晚|今晚|am|pm)"
_TIME_RES = [
    # 12:30 / 12.30 / 12:30pm
    re.compile(rf"{_PERIOD}?\s*(\d{{1,2}})[:：](\d{{2}})\s*(am|pm)?", re.I),
    # 12點半 / 12點30 / 十二點 / 12點30分 / 下午2點
    re.compile(rf"{_PERIOD}?\s*({_NUM})\s*(?:點鐘|點|时|時)\s*(半|{_NUM}\s*分?)?", re.I),
    # 12pm / 7 am
    re.compile(r"()(\d{1,2})\s*(am|pm)\b", re.I),
]
_PM_WORDS = {"下晝", "下午", "晏晝", "晚上", "夜晚", "今晚", "pm"}
_AM_WORDS = {"朝早", "朝頭早", "早上", "早晨", "上午", "凌晨", "半夜", "am"}


def parse_clock(text: str) -> "tuple[tuple[int, int | None, bool | None], str] | None":
    """Find a clock time in text.

    Returns ((hour, minute, pm), remaining_text) where pm is True/False when the
    speaker said am/pm (or a part of day) and None when ambiguous; None if no
    time is present.
    """
    for i, rx in enumerate(_TIME_RES):
        m = rx.search(text)
        if not m:
            continue
        if i == 0:
            period, h, mi, suffix = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        elif i == 1:
            period, h = m.group(1), zh_int(m.group(2))
            tail = (m.group(3) or "").replace("分", "").strip()
            mi = 30 if tail == "半" else (zh_int(tail) if tail else 0)
            suffix = None
            if h is None or mi is None:
                continue
        else:
            period, h, mi, suffix = "", int(m.group(2)), 0, m.group(3)
        word = (suffix or period or "").lower()
        pm = True if word in _PM_WORDS else False if word in _AM_WORDS else None
        if not (0 <= h <= 24 and 0 <= mi <= 59):
            continue
        return (h % 24 if h == 24 else h, mi, pm), (text[:m.start()] + text[m.end():]).strip()
    return None


def resolve_past(clock: "tuple[int, int, bool | None]", now: dt.datetime) -> dt.datetime:
    """Most recent instant <= now matching the clock time (within the last 24h)."""
    h, mi, pm = clock
    if pm is True and h < 12:
        hours = [h + 12]
    elif pm is False and h == 12:
        hours = [0]
    elif pm is None and h < 12:
        hours = [h, h + 12]
    else:
        hours = [h]
    best = None
    for hh in hours:
        for back in (0, 1):
            cand = (now - dt.timedelta(days=back)).replace(hour=hh, minute=mi, second=0, microsecond=0)
            if cand <= now and (best is None or cand > best):
                best = cand
    return best


def parse_weight(text: str) -> "float | None":
    """Number after 體重/weight: 88.5, 八十八點五, 88點5. None if absent or implausible."""
    m = re.search(r"(?:體重|体重|重量|weight)[^\d零〇一二兩两三四五六七八九十]*"
                  rf"({_NUM})(?:\s*(?:點|点|\.)\s*([\d零〇一二三四五六七八九]+))?", text, re.I)
    if not m:
        return None
    whole = zh_int(m.group(1))
    if whole is None:
        return None
    frac = ""
    if m.group(2):
        for c in m.group(2):
            frac += c if c.isdigit() else str(_ZH_DIGITS[c])
    kg = float(f"{whole}.{frac}") if frac else float(whole)
    return kg if MIN_KG <= kg <= MAX_KG else None


# --- state -----------------------------------------------------------------

def load(path: str = STATE_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    state.setdefault("windows", [])
    state.setdefault("weights", {})
    state.setdefault("workouts", {})
    return state


def _save(state: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _t(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=NZ_TZ)


def _iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M")


def eff_end(w: dict, now: dt.datetime) -> "dt.datetime | None":
    """Window end: the stated one, else start+8h once that has passed (forgot 食完), else open."""
    if w.get("end"):
        return _t(w["end"])
    planned = _t(w["start"]) + dt.timedelta(hours=WINDOW_HOURS)
    return planned if now >= planned else None


def next_eat(end: dt.datetime) -> dt.datetime:
    return end + dt.timedelta(hours=FAST_HOURS)


def week_start(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def week_workouts(state: dict, today: dt.date) -> int:
    ws = week_start(today)
    return sum(1 for k, v in state["workouts"].items()
               if v and ws <= dt.date.fromisoformat(k) <= ws + dt.timedelta(days=6))


# --- spoken helpers (kept local so ops scripts need not import the router) ---

def speak_time(d: dt.datetime) -> str:
    h, m = d.hour, d.minute
    period = "朝早" if 6 <= h < 12 else "晏晝" if 12 <= h < 18 else "夜晚" if h >= 18 else "半夜"
    h12 = h % 12 or 12
    return f"{period}{h12}點{m:02d}分" if m else f"{period}{h12}點"


def speak_when(d: dt.datetime, now: dt.datetime) -> str:
    days = (d.date() - now.date()).days
    day = {0: "今日", 1: "聽日", 2: "後日"}.get(days, f"{d.month}月{d.day}號")
    return day + speak_time(d)


def speak_hours(delta: dt.timedelta) -> str:
    mins = max(0, round(delta.total_seconds() / 60))
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}個鐘又{m}分鐘"
    return f"{h}個鐘" if h else f"{m}分鐘"


def stop_alert_text(start: dt.datetime) -> str:
    """The 8h-mark push body: 食完喇，由而家開始斷食到聽日 X 點."""
    end = start + dt.timedelta(hours=WINDOW_HOURS)
    return f"食完喇，由而家開始斷食到{speak_when(next_eat(end), end)}"


# --- commands --------------------------------------------------------------

def _fast_before(state: dict, start: dt.datetime, idx: int, now: dt.datetime) -> "dt.timedelta | None":
    if idx == 0:
        return None
    prev_end = eff_end(state["windows"][idx - 1], now)
    return start - prev_end if prev_end else None


def start_eating(clock, now: dt.datetime, path: str = STATE_PATH) -> "tuple[str, dict]":
    start = resolve_past(clock, now) if clock else now.replace(second=0, microsecond=0)
    with _lock:
        state = load(path)
        wins = state["windows"]
        last = wins[-1] if wins else None
        correction = False
        if last:
            last_start = _t(last["start"])
            last_end = _t(last["end"]) if last.get("end") else None
            if last_end is None and start - last_start < dt.timedelta(hours=12):
                correction = True
            elif last_end is not None and start < last_end:
                correction = True
        if correction:
            last["start"] = _iso(start)
            if last.get("end") and _t(last["end"]) < start:
                last["end"] = None
            idx = len(wins) - 1
        else:
            if last and not last.get("end"):  # forgot 食完 last time: assume the full 8h
                last["end"] = _iso(min(_t(last["start"]) + dt.timedelta(hours=WINDOW_HOURS), start))
                last["end_assumed"] = True
            wins.append({"start": _iso(start), "end": None})
            idx = len(wins) - 1
        fast = _fast_before(state, start, idx, now)
        wins[idx]["fast_ok"] = fast is None or fast >= dt.timedelta(hours=FAST_HOURS)
        _save(state, path)
    stop = start + dt.timedelta(hours=WINDOW_HOURS)
    reply = f"記低，{speak_when(start, now)}開始食，要{speak_when(stop, now)}食完"
    if fast is not None:
        reply += f"。斷食咗{speak_hours(fast)}"
        if fast < dt.timedelta(hours=FAST_HOURS):
            reply += f"，未夠{FAST_HOURS}個鐘，今日唔計達標"
    data = {"event": "start", "start": _iso(start), "stop_by": _iso(stop),
            "fast_hours": round(fast.total_seconds() / 3600, 1) if fast is not None else None,
            "fast_ok": wins[idx]["fast_ok"], "correction": correction}
    return reply, data


def stop_eating(clock, now: dt.datetime, path: str = STATE_PATH) -> "tuple[str, dict]":
    end = resolve_past(clock, now) if clock else now.replace(second=0, microsecond=0)
    with _lock:
        state = load(path)
        wins = state["windows"]
        if not wins:
            return "未記低你幾時開始食，先講開始食加時間", None
        if wins[-1].get("end") and not clock:
            return "已經記低咗食完，要改就講返時間，例如七點食完", None
        w = wins[-1]
        start = _t(w["start"])
        if end < start:
            return "食完嘅時間早過開始食，再講一次", None
        w["end"] = _iso(end)
        w.pop("end_assumed", None)
        _save(state, path)
    span = end - start
    ok = span <= dt.timedelta(hours=WINDOW_HOURS)
    nxt = next_eat(end)
    reply = f"記低，食咗{speak_hours(span)}"
    if not ok:
        reply += f"，超咗{speak_hours(span - dt.timedelta(hours=WINDOW_HOURS))}"
    reply += f"。斷食到{speak_when(nxt, now)}"
    return reply, {"event": "stop", "start": _iso(start), "end": _iso(end),
                   "span_hours": round(span.total_seconds() / 3600, 1),
                   "window_ok": ok, "next_eat": _iso(nxt)}


def fast_status(now: dt.datetime, path: str = STATE_PATH) -> "tuple[str, dict]":
    state = load(path)
    if not state["windows"]:
        return "未有紀錄，講開始食加時間就開始記", None
    w = state["windows"][-1]
    start = _t(w["start"])
    end = eff_end(w, now)
    if end is None:
        left = start + dt.timedelta(hours=WINDOW_HOURS) - now
        return (f"而家喺進食時間，仲有{speak_hours(left)}，"
                f"{speak_when(start + dt.timedelta(hours=WINDOW_HOURS), now)}要食完"), {"event": "status", "eating": True}
    nxt = next_eat(end)
    if now < nxt:
        return (f"斷食緊，已經斷食咗{speak_hours(now - end)}，仲有{speak_hours(nxt - now)}，"
                f"{speak_when(nxt, now)}先可以食"), {"event": "status", "eating": False}
    return "斷食夠16個鐘喇，可以開始食", {"event": "status", "eating": False}


def log_weight(kg: float, now: dt.datetime, path: str = STATE_PATH) -> "tuple[str, dict]":
    today = now.date().isoformat()
    with _lock:
        state = load(path)
        prev_days = sorted(d for d in state["weights"] if d < today)
        prev = state["weights"][prev_days[-1]] if prev_days else None
        state["weights"][today] = kg
        _save(state, path)
    reply = f"記低，{kg:g}公斤"
    if prev is not None and abs(kg - prev) >= 0.05:
        reply += f"，比上次{'輕' if kg < prev else '重'}咗{abs(kg - prev):.1f}公斤"
    togo = kg - TARGET_WEIGHT
    reply += f"。仲差{togo:.1f}公斤到{TARGET_WEIGHT:g}" if togo > 0 else f"。已經達到{TARGET_WEIGHT:g}公斤目標"
    return reply, {"event": "weight", "kg": kg, "to_target": round(togo, 1)}


def log_workout(now: dt.datetime, path: str = STATE_PATH) -> "tuple[str, dict]":
    today = now.date()
    with _lock:
        state = load(path)
        state["workouts"][today.isoformat()] = True
        _save(state, path)
        n = week_workouts(state, today)
    reply = f"記低，今個禮拜第{n}次運動"
    reply += f"，仲差{WEEKLY_WORKOUTS - n}次" if n < WEEKLY_WORKOUTS else "，今個禮拜目標達成"
    return reply, {"event": "workout", "week_count": n}


# --- utterance matching (used by router.route) --------------------------------

_START_RE = re.compile(r"^我?(?:開始食|开始食|開始吃|开始吃|開始食嘢|开始食嘢|開餐|开餐|starteating|startedeating|firstmeal)(?:喇|啦|了)?$")
_STOP_RE = re.compile(r"^我?(?:食完|食晒|食咗|吃完|吃了|stopeating|stoppedeating|finishedeating|doneeating)(?:喇|啦|了|嘢)?$")
_STATUS = {"斷食狀態", "断食状态", "斷食", "断食", "仲有幾耐", "仲有几耐", "幾時食得", "几时食得",
           "fastingstatus", "faststatus", "fasting"}
_WORKOUT = {"做完運動", "做完运动", "做咗運動", "做咗运动", "運動完", "运动完", "運動做完", "运动做完",
            "完成運動", "完成运动", "練完", "练完", "健身完", "訓練完", "训练完", "運動咗", "运动咗",
            "workout", "workoutdone", "workedout", "iworkedout", "didmyworkout", "doneworkout"}
_FILLER = re.compile(r"^(?:我|今日|今朝|今天)+")


def _norm(text: str) -> str:
    return re.sub(r"[^\w一-鿿]+", "", text.lower())


def handle(text: str, now: "dt.datetime | None" = None, path: str = STATE_PATH) -> "dict | None":
    """Route a health utterance; None when the text is not one (fall through to the router)."""
    now = now or dt.datetime.now(NZ_TZ)
    if len(text) > 40:
        return None
    clock_hit = parse_clock(text)
    clock, rest = clock_hit if clock_hit else (None, text)
    rest_n = _norm(rest)

    def out(cmd, result):
        reply, data = result
        if data is None:
            return {"command": cmd, "status": "error", "reply": reply}
        return {"command": cmd, "status": "executed", "reply": reply, "data": data}

    if _START_RE.match(rest_n):
        return out("FAST_START", start_eating(clock, now, path))
    if _STOP_RE.match(rest_n):
        return out("FAST_STOP", stop_eating(clock, now, path))
    if clock is None and rest_n in _STATUS:
        return out("FAST_STATUS", fast_status(now, path))
    if clock is None and _FILLER.sub("", rest_n) in _WORKOUT:
        return out("LOG_WORKOUT", log_workout(now, path))
    if re.search(r"體重|体重|重量|weight", text, re.I):
        kg = parse_weight(text)
        if kg is None:
            return {"command": "LOG_WEIGHT", "status": "error",
                    "reply": "聽唔清體重幾多，再講一次，例如體重88.5"}
        return out("LOG_WEIGHT", log_weight(kg, now, path))
    return None


# --- per-day rows for the Notion mirror / weekly summary -----------------------

def day_rows(state: dict, now: dt.datetime) -> "dict[str, dict]":
    """date -> row. Eating start/end span the day's windows; fast = gap from the previous day's last window."""
    by_day: dict[str, list] = {}
    for w in state["windows"]:
        by_day.setdefault(w["start"][:10], []).append(w)
    days = set(by_day) | set(state["weights"]) | set(state["workouts"])
    rows = {}
    prev_end = None
    for d in sorted(days):
        row = {"date": d, "weight": state["weights"].get(d), "workout": bool(state["workouts"].get(d)),
               "start": None, "end": None, "span_hours": None, "fast_hours": None,
               "window_ok": None, "fast_ok": None}
        wins = by_day.get(d)
        if wins:
            start = _t(wins[0]["start"])
            ends = [eff_end(w, now) for w in wins]
            row["start"] = wins[0]["start"][11:16]
            if all(e is not None for e in ends):
                end = max(ends)
                row["end"] = end.strftime("%H:%M")
                span = end - start
                row["span_hours"] = round(span.total_seconds() / 3600, 1)
                row["window_ok"] = span <= dt.timedelta(hours=WINDOW_HOURS)
                prev_end_for_next = end
            else:
                prev_end_for_next = None
            if prev_end is not None:
                fast = start - prev_end
                row["fast_hours"] = round(fast.total_seconds() / 3600, 1)
                row["fast_ok"] = fast >= dt.timedelta(hours=FAST_HOURS)
            prev_end = prev_end_for_next
        rows[d] = row
    return rows
