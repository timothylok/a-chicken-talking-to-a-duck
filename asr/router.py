"""Command router: maps transcribed phrases to allowlisted commands.

Hardening rules (CLAUDE.md checklist): destructive commands match only exact
allowlisted phrases after normalization, and require a confirmation phrase
within CONFIRM_TTL_SECONDS before executing.
"""

import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import zoneinfo

log = logging.getLogger("router")

CONFIRM_TTL_SECONDS = 60
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# gemma3:4b produces the most natural spoken Cantonese of the local models
# (qwen2.5:7b mixes in English words; qwen3:8b answers in written Chinese).
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")
OLLAMA_SYSTEM_PROMPT = (
    "你係香港人語音助手。只可以用香港口語廣東話回答，一至兩句。"
    "絕對唔准用英文單詞、唔准用emoji、唔准用書面語、唔准用普通話詞彙，唔好講粗口。"
)

# Populated by server.py at startup.
status_info = {"model": "?", "device": "?", "started": time.time()}


def warm_ollama() -> None:
    # Preload the chat model (an empty messages array makes Ollama load the
    # model and return) so the first chat or headline translation after a
    # reboot doesn't burn its 30 s timeout on weight loading.
    payload = json.dumps({"model": OLLAMA_MODEL, "messages": [], "stream": False}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300):
            pass
        log.info("ollama model %s warmed", OLLAMA_MODEL)
    except Exception as exc:
        log.warning("ollama warm-up failed (chat fallback will load lazily): %s", exc)


def _system_status() -> str:
    hours = (time.time() - status_info["started"]) / 3600
    return (
        f"系統正常，模型 {status_info['model']}，"
        f"設備 {status_info['device']}，已運行 {hours:.1f} 小時"
    )


def _list_commands() -> str:
    return "可用指令：" + "、".join(spec["phrases"][0] for spec in COMMANDS.values())


def _restart_service() -> str:
    # The router runs inside the VoiceASR service, so it cannot stop/start
    # itself via the SCM. Instead: reply first, then exit — NSSM's default
    # AppExit action relaunches the app. Daemon thread so tests don't die.
    timer = threading.Timer(2.0, os._exit, [0])
    timer.daemon = True
    timer.start()
    log.warning("restart requested via voice command; exiting in 2 s")
    return "語音系統重啟中，約三十秒後恢復"


# TheColab petrolmate skill (vetted 2026-07-13, see D:\ai\thecolab-skills):
# keyless Gaspy-backed fuel prices, cheapest first. FUEL_LOCATION needs a
# disambiguated name ("glenfield auckland") — the API covers AU too and a bare
# suburb can resolve across the Tasman.
FUEL_CLI = "D:/ai/thecolab-skills/skills/petrolmate-nz-au/scripts/cli.py"
FUEL_LOCATION = os.environ.get("FUEL_LOCATION", "auckland")


def _fuel_prices() -> tuple[str, dict] | str:
    stations = _run_skill(
        FUEL_CLI, "search", "--location", FUEL_LOCATION,
        "--fuel", "PULP95", "--limit", "3", "--json", timeout=20,
    )["stations"]
    if not stations:
        return "攞唔到油價資料"
    parts = [f"{s['name']}每公升{float(s['price']) / 100:.2f}蚊" for s in stations]
    reply = f"{FUEL_LOCATION.split()[0].title()}附近最平95汽油：" + "，".join(parts)
    data = {
        "fuel": "PULP95",
        "stations": [
            {"name": s["name"], "price": round(float(s["price"]) / 100, 2)}
            for s in stations
        ],
    }
    return reply, data


# TheColab at-transport skill: live AT departures. BUS_STOPS holds AT stop
# codes; defaults are the two Glenfield Mall stops (one per direction).
AT_CLI = "D:/ai/thecolab-skills/skills/at-transport/scripts/cli.py"
BUS_STOPS = os.environ.get("BUS_STOPS", "3881,4010")
NZ_TZ = zoneinfo.ZoneInfo("Pacific/Auckland")


def _bus_times() -> tuple[str, dict] | str:
    now = dt.datetime.now(dt.timezone.utc)
    stop_name, departures = "", []
    for stop in BUS_STOPS.split(","):
        data = _run_skill(AT_CLI, "departures", stop.strip(), "--json", timeout=20)
        stop_name = stop_name or data["stop"]["stop_name"]
        for d in data["departures"]:
            if d.get("expected_time"):
                when = dt.datetime.fromisoformat(d["expected_time"].replace("Z", "+00:00"))
            elif d.get("scheduled_time"):
                h, m, s = map(int, d["scheduled_time"].split(":"))
                local = dt.datetime.now(NZ_TZ)
                when = local.replace(hour=h % 24, minute=m, second=s)
            else:
                continue
            minutes = round((when - now).total_seconds() / 60)
            if 0 <= minutes <= 120:
                departures.append((minutes, d["route_short_name"]))
    if not departures:
        return "而家實時系統未見有巴士喺路上，遲啲再問下"
    departures.sort()
    parts = [
        f"{route}號{minutes}分鐘後" if minutes else f"{route}號即刻到"
        for minutes, route in departures[:3]
    ]
    data = {
        "stop": stop_name,
        "departures": [
            {"route": route, "minutes": minutes} for minutes, route in departures[:3]
        ],
    }
    return f"{stop_name}巴士：" + "，".join(parts), data


# TheColab nz-tides-surf skill: LINZ tide predictions, keyless.
TIDES_CLI = "D:/ai/thecolab-skills/skills/nz-tides-surf/scripts/cli.py"
TIDE_PORT = os.environ.get("TIDE_PORT", "auckland")


def _speak_time(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    period = "朝早" if 6 <= h < 12 else "晏晝" if 12 <= h < 18 else "夜晚" if h >= 18 else "半夜"
    h12 = h % 12 or 12
    return f"{period}{h12}點{m:02d}分" if m else f"{period}{h12}點"


def _tide_times() -> tuple[str, dict] | str:
    data = _run_skill(TIDES_CLI, "next-tide", TIDE_PORT, "--json", timeout=20)
    events = data["events"][:2]
    if not events:
        return "攞唔到潮汐資料"
    names = {"high": "潮漲", "low": "潮退"}
    parts = [
        f"{names.get(e['type'], e['type'])}{_speak_time(e['time_local'])}，{e['height_m']}米"
        for e in events
    ]
    structured = {
        "port": data["resolved_port"],
        "events": [
            {"type": e["type"], "time": e["time_local"], "height_m": e["height_m"]}
            for e in events
        ],
    }
    return f"{data['resolved_port']}潮汐：" + "，".join(parts), structured


# TheColab auckland-bin-schedule skill. Collection days are per-property, so
# BIN_ADDRESS must be a real street address (or numeric property ID) — set it
# in the service env, not here: a bare suburb fuzzy-matches the wrong area
# (e.g. "glenfield auckland" resolves to Glenfield Road, Papakura).
BINS_CLI = "D:/ai/thecolab-skills/skills/auckland-bin-schedule/scripts/cli.py"
BIN_ADDRESS = os.environ.get("BIN_ADDRESS", "")

_WEEKDAYS_YUE = {
    "Monday": "禮拜一", "Tuesday": "禮拜二", "Wednesday": "禮拜三",
    "Thursday": "禮拜四", "Friday": "禮拜五", "Saturday": "禮拜六",
    "Sunday": "禮拜日",
}
_MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}


def _speak_date(text: str) -> str:
    # "Thursday, 16 July" -> "禮拜四7月16號"
    try:
        weekday, rest = text.split(", ")
        day, month = rest.split(" ")
        return f"{_WEEKDAYS_YUE[weekday]}{_MONTHS[month]}月{day}號"
    except (ValueError, KeyError):
        return text


_BIN_STREAMS = [("rubbish", "垃圾"), ("food_scraps", "廚餘"), ("recycling", "回收")]


def _bin_next_dates() -> dict:
    args = ["--property-id", BIN_ADDRESS] if BIN_ADDRESS.isdigit() else [BIN_ADDRESS]
    return _run_skill(BINS_CLI, "--json", *args)["household"]["next_dates"]


def _bin_day() -> tuple[str, dict] | str:
    if not BIN_ADDRESS:
        return "未設定屋企地址，要喺服務環境變數加BIN_ADDRESS"
    dates = _bin_next_dates()
    by_date = {}
    for key, label in _BIN_STREAMS:
        if dates.get(key) and dates[key] != "—":
            by_date.setdefault(dates[key], []).append(label)
    if not by_date:
        return "攞唔到收垃圾日資料"
    parts = [f"{'同'.join(ls)}{_speak_date(d)}收" for d, ls in by_date.items()]
    reply = "，".join(parts) + "。記住前一晚或者朝早七點前擺出嚟"
    data = {key: dates[key] for key, _ in _BIN_STREAMS if dates.get(key) and dates[key] != "—"}
    return reply, data


def _run_skill(cli: str, *args: str, timeout: int = 30) -> dict:
    # PYTHONIOENCODING forces the child's stdout to UTF-8: under the service
    # it defaults to cp1252, and JSON containing macrons (rūnanga, Whangārei)
    # kills the child with UnicodeEncodeError before it prints anything.
    out = subprocess.run(
        [sys.executable, cli, *args],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return json.loads(out.stdout)


# TheColab geonet-nz skill: GeoNet public quake feed, keyless. MMI >= 3
# limits it to quakes people actually felt.
QUAKE_CLI = "D:/ai/thecolab-skills/skills/geonet-nz/scripts/cli.py"

_DIRECTIONS_YUE = {
    "north": "北", "south": "南", "east": "東", "west": "西",
    "north-east": "東北", "north-west": "西北",
    "south-east": "東南", "south-west": "西南",
}


def _speak_locality(text: str) -> str:
    # "5 km south-east of Tokomaru Bay" -> "Tokomaru Bay東南面5公里"
    m = re.fullmatch(r"(\d+) km ([a-z-]+) of (.+)", text)
    if m and m.group(2) in _DIRECTIONS_YUE:
        return f"{m.group(3)}{_DIRECTIONS_YUE[m.group(2)]}面{m.group(1)}公里"
    m = re.fullmatch(r"Within (\d+) km of (.+)", text)
    if m:
        return f"{m.group(2)}附近"
    return text


def _speak_ago(iso: str) -> str:
    when = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    minutes = round((dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 60)
    if minutes < 60:
        return f"{max(minutes, 1)}分鐘前"
    if minutes < 24 * 60:
        return f"{minutes // 60}個鐘前"
    return f"{minutes // (24 * 60)}日前"


def _earthquakes() -> str:
    quakes = _run_skill(
        QUAKE_CLI, "quakes", "--mmi", "3", "--limit", "5", "--json", timeout=20,
    )["quakes"]
    if not quakes:
        return "最近冇有感地震"
    latest = quakes[0]
    return (
        f"最近一次有感地震喺{_speak_ago(latest['time'])}，"
        f"{_speak_locality(latest['locality'])}，"
        f"{latest['magnitude']:.1f}級，深{round(latest['depth_km'])}公里"
    )


# TheColab nz-news skill: keyless RSS aggregation across major NZ outlets.
NEWS_CLI = "D:/ai/thecolab-skills/skills/nz-news/scripts/cli.py"


def _translate_headline(title: str) -> str:
    # iOS TTS reads English headlines poorly mid-Cantonese, so translate them
    # locally; person and place names stay in English (translating them makes
    # the TTS worse, not better). One call per headline: batch translation
    # proved unparseable (the model adds preambles or splits lines).
    prompt = (
        "將呢條新聞標題翻譯做香港口語廣東話，人名、地名同機構名保留英文原文。"
        "只准輸出譯文嗰一句，唔好加編號、引號、解釋：\n" + title
    )
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        reply = json.loads(resp.read())["message"]["content"]
    line = next(ln for ln in reply.strip().splitlines() if ln.strip())
    return line.strip().strip('"「」').rstrip("。.．")


def _news_headlines() -> tuple[str, dict]:
    items = _run_skill(NEWS_CLI, "headlines", "--limit", "3", "--json")["items"]
    if not items:
        return "攞唔到新聞", {"headlines_en": []}
    spoken = []
    for item in items[:3]:
        try:
            spoken.append(_translate_headline(item["title"]))
        except Exception as exc:
            log.error("headline translation failed, using English: %s", exc)
            spoken.append(item["title"])
    parts = [f"{o}，{t}" for o, t in zip(("第一", "第二", "第三"), spoken)]
    # Original English headlines are the pre-translation source of truth.
    data = {"headlines_en": [i["title"] for i in items[:3]], "headlines_yue": spoken}
    return "今日新聞：" + "。".join(parts), data


# Auckland, New Zealand. Open-Meteo is keyless; forecast_days=1 keeps it to today.
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=-36.85&longitude=174.76"
    "&current=temperature_2m,weather_code"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&timezone=Pacific%2FAuckland&forecast_days=1"
)

# WMO weather codes -> spoken Cantonese, as (upper bound, description) ranges.
_WEATHER_DESCRIPTIONS = [
    (0, "天晴"), (2, "少雲"), (3, "多雲"), (48, "有霧"), (57, "毛毛雨"),
    (67, "落緊雨"), (77, "落緊雪"), (82, "有驟雨"), (86, "有驟雪"), (99, "有雷暴"),
]


def _describe_weather(code: int) -> str:
    for upper, desc in _WEATHER_DESCRIPTIONS:
        if code <= upper:
            return desc
    return ""


def _weather_today() -> tuple[str, dict]:
    with urllib.request.urlopen(OPEN_METEO_URL, timeout=10) as resp:
        payload = json.loads(resp.read())
    current = payload["current"]
    daily = payload["daily"]
    data = {
        "temp": round(current["temperature_2m"]),
        "high": round(daily["temperature_2m_max"][0]),
        "low": round(daily["temperature_2m_min"][0]),
        "rain_prob": daily["precipitation_probability_max"][0],
        "code": current["weather_code"],
    }
    reply = (
        f"奧克蘭而家{data['temp']}度，"
        f"{_describe_weather(data['code'])}。"
        f"今日最高{data['high']}度，"
        f"最低{data['low']}度，"
        f"落雨機會百分之{data['rain_prob']}"
    )
    return reply, data


def _yesterday_weather_data() -> dict | None:
    # Latest WEATHER_TODAY data recorded yesterday (NZ time) in the local
    # history — comparisons never query Notion mid-request.
    target = (dt.datetime.now(NZ_TZ) - dt.timedelta(days=1)).date().isoformat()
    found = None
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if (entry.get("command") == "WEATHER_TODAY"
                        and entry.get("data")
                        and entry.get("ts", "").startswith(target)):
                    found = entry["data"]
    except FileNotFoundError:
        return None
    return found


def _weather_compare() -> str:
    yesterday = _yesterday_weather_data()
    if not yesterday:
        return "冇琴日嘅天氣紀錄，今日問咗天氣，聽日先比較得"
    _, today = _weather_today()
    parts = []
    for key, label in (("high", "最高"), ("low", "最低")):
        diff = today[key] - yesterday[key]
        if diff > 0:
            parts.append(f"{label}{today[key]}度，比琴日高{diff}度")
        elif diff < 0:
            parts.append(f"{label}{today[key]}度，比琴日低{-diff}度")
        else:
            parts.append(f"{label}{today[key]}度，同琴日一樣")
    return "今日" + "，".join(parts)


# CREATE_REMINDER: the one deliberate exception to exact matching — any
# utterance starting with these (normalized) prefixes routes here. The action
# is fixed-type and non-destructive (create one iOS reminder); the LLM only
# extracts {title, due} as data, validated below — it never picks a command.
# The router returns the payload; the iPhone Shortcut does the actual write
# (Apple exposes no server-side Reminders API).
REMINDER_PREFIXES = ("提醒我", "提我", "remindme")


def _extract_reminder(text: str) -> tuple[str, "dt.datetime | None"]:
    now = dt.datetime.now(NZ_TZ)
    # Spell out the dates — gemma3:4b copies reliably but computes "tomorrow"
    # wrongly (observed 聽日 resolving two days out).
    calendar = "。".join(
        f"{label}係{now + dt.timedelta(days=i):%Y-%m-%d}"
        for i, label in ((0, "今日"), (1, "聽日"), (2, "後日"))
    ) + "。" + "。".join(
        f"{_WEEKDAYS_YUE[(now + dt.timedelta(days=i)).strftime('%A')]}"
        f"係{now + dt.timedelta(days=i):%Y-%m-%d}"
        for i in range(1, 8)
    )
    prompt = (
        f"而家係{now:%Y-%m-%d %H:%M}，紐西蘭時間。{calendar}。"
        "從下面呢句廣東話抽取提醒事項，只輸出JSON，格式："
        '{"title": "要做嘅嘢", "due": "YYYY-MM-DD HH:MM"}。'
        '如果冇講時間，due用null。title要簡短，唔好包時間字眼。\n'
        "說話：" + text
    )
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "format": "json",
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        parsed = json.loads(json.loads(resp.read())["message"]["content"])
    title = str(parsed.get("title") or "").strip()[:100]
    if not title:
        raise ValueError("no title extracted")
    due = None
    if parsed.get("due"):
        due = dt.datetime.strptime(str(parsed["due"]), "%Y-%m-%d %H:%M").replace(tzinfo=NZ_TZ)
        if not (now - dt.timedelta(minutes=5) <= due <= now + dt.timedelta(days=366)):
            raise ValueError(f"due out of range: {due}")
    return title, due


def _speak_due(due: "dt.datetime") -> str:
    days = (due.date() - dt.datetime.now(NZ_TZ).date()).days
    if days == 0:
        day = "今日"
    elif days == 1:
        day = "聽日"
    elif days == 2:
        day = "後日"
    elif days < 7:
        day = _WEEKDAYS_YUE[due.strftime("%A")]
    else:
        day = f"{due.month}月{due.day}號"
    return day + _speak_time(f"{due.hour}:{due.minute:02d}")


def _create_reminder(text: str) -> dict:
    try:
        title, due = _extract_reminder(text)
    except Exception as exc:
        log.error("reminder extraction failed for %r: %s", text, exc)
        return {
            "command": "CREATE_REMINDER", "status": "error",
            "reply": "唔明你想提咩，再講一次，例如提我聽日朝早九點買牛奶",
        }
    # A time is required: the Shortcut always sets an alert, which keeps it
    # free of nested If blocks (reminder payload => due always present).
    if due is None:
        return {
            "command": "CREATE_REMINDER", "status": "error",
            "reply": f"要講埋幾點提你{title}，例如提我聽日朝早九點{title}",
        }
    # No alert on the phone at all: iOS 2026 Shortcuts "Add New Reminder"
    # rejects every dynamic alert value (variable as date, formatted text,
    # even time-only) with "alert time provided was invalid" — only static
    # picker values work. The due time rides in the title instead, absolute
    # (not 聽日) so it still reads correctly days later.
    when = f"{due.month}月{due.day}號{_speak_time(f'{due.hour}:{due.minute:02d}')}"
    reminder = {"title": f"{title}（{when}）"}
    data = {"title": title, "due": due.strftime("%Y-%m-%d %H:%M")}
    reply = f"好，{_speak_due(due)}提你{title}"
    log.info("reminder: %r -> %s", text, reminder)
    # "reminder" rides back to the Shortcut, which creates the iOS Reminder;
    # "data" goes to history/Notion like any other command.
    return {
        "command": "CREATE_REMINDER", "status": "executed", "reply": reply,
        "reminder": reminder, "data": data,
    }


def _briefing_bins() -> str:
    # Bin reminder only when collection is today or tomorrow — the full
    # schedule is BIN_DAY's job.
    if not BIN_ADDRESS:
        return ""
    dates = _bin_next_dates()
    now = dt.datetime.now(NZ_TZ)
    for offset, word in ((0, "今日"), (1, "聽日")):
        d = now + dt.timedelta(days=offset)
        key = f"{d.strftime('%A')}, {d.day} {d.strftime('%B')}"
        streams = [label for k, label in _BIN_STREAMS if dates.get(k) == key]
        if streams:
            return f"{word}收{'同'.join(streams)}，記住朝早七點前擺出嚟"
    return ""


def _morning_briefing() -> str:
    # Compose existing sections; a failed source drops out instead of
    # killing the whole briefing.
    sections = []
    for fn in (_weather_today, _bus_times, _briefing_bins, _news_headlines):
        try:
            part = fn()
            if isinstance(part, tuple):  # runners that also return history data
                part = part[0]
            if part:
                sections.append(part)
        except Exception as exc:
            log.error("briefing section %s failed: %s", fn.__name__, exc)
    if not sections:
        return "攞唔到簡報資料"
    return "早晨！" + "。".join(sections)


def _trigger_deploy() -> str:
    hook = os.environ.get("DEPLOY_HOOK_URL")
    if not hook:
        return "deploy hook not configured"
    with urllib.request.urlopen(hook, data=b"") as resp:
        return f"deploy triggered ({resp.status})"


COMMANDS = {
    "SYSTEM_STATUS": {
        "phrases": [
            "系統狀態", "系统状态", "系統狀況", "系统状况",
            "檢查系統狀態", "检查系统状态", "健康檢查", "健康检查",
            "system status", "system health", "status", "health check",
        ],
        "destructive": False,
        "run": _system_status,
    },
    "LIST_COMMANDS": {
        "phrases": [
            "有咩指令", "有什麼指令", "有什么指令", "指令列表",
            "list commands", "what commands",
        ],
        "destructive": False,
        "run": _list_commands,
    },
    "WEATHER_TODAY": {
        "phrases": [
            "今日天氣", "今日天气", "今天天氣", "今天天气",
            "今日天氣如何", "今日天气如何", "今日天氣點樣", "天氣報告", "天气报告",
            "weather today", "today's weather", "weather report", "weather",
        ],
        "destructive": False,
        "run": _weather_today,
    },
    "WEATHER_COMPARE": {
        "phrases": [
            "同琴日比", "同尋日比", "今日同琴日比", "天氣比較", "比較天氣",
            "今日凍啲定熱啲",
            "compare weather", "compare with yesterday", "weather compare",
        ],
        "destructive": False,
        "run": _weather_compare,
    },
    "FUEL_PRICES": {
        "phrases": [
            "油價", "油价", "油價幾多", "今日油價", "汽油價錢", "汽油价钱",
            "邊度入油平", "入油", "fuel price", "fuel prices", "petrol price",
            "gas price",
            "有加",  # observed 2026-07-14: 油價 misheard on a short clip
            "casaprice",  # observed 2026-07-14: "gas price" run together
        ],
        "destructive": False,
        "run": _fuel_prices,
    },
    "BUS_TIMES": {
        "phrases": [
            "巴士", "巴士幾時", "幾時有巴士", "巴士時間", "巴士时间", "巴士班次",
            "巴西",  # observed 2026-07-14: Whisper mishears 巴士 on short clips
            "bus", "bus times", "bus time", "when is the bus", "next bus",
        ],
        "destructive": False,
        "run": _bus_times,
    },
    "TIDE_TIMES": {
        "phrases": [
            "潮汐", "潮漲", "潮退", "幾時潮漲", "幾時潮退", "潮水", "潮汐時間",
            "tide", "tides", "tide times", "high tide", "next tide",
        ],
        "destructive": False,
        "run": _tide_times,
    },
    "BIN_DAY": {
        "phrases": [
            "幾時收垃圾", "邊日收垃圾", "收垃圾", "倒垃圾", "垃圾日", "垃圾",
            "幾時倒垃圾", "rubbish day", "bin day", "rubbish collection",
            "when is rubbish day",
        ],
        "destructive": False,
        "run": _bin_day,
    },
    "EARTHQUAKES": {
        "phrases": [
            "地震", "有冇地震", "最近地震", "最近有冇地震", "地震消息",
            "earthquake", "earthquakes", "recent earthquakes", "any earthquakes",
        ],
        "destructive": False,
        # Locality keeps English place names ("Tokomaru Bay") — word-by-word
        # pauses would mangle them.
        "pause_english": False,
        "run": _earthquakes,
    },
    "NEWS_HEADLINES": {
        "phrases": [
            "新聞", "新闻", "今日新聞", "今日新闻", "有咩新聞", "新聞頭條",
            "news", "news headlines", "headlines", "what's the news",
        ],
        "destructive": False,
        # Full English headlines: word-by-word pauses would mangle them.
        "pause_english": False,
        "run": _news_headlines,
    },
    "MORNING_BRIEFING": {
        "phrases": [
            "早晨", "早晨簡報", "今日簡報", "簡報", "早安",
            "good morning", "morning briefing", "briefing", "daily briefing",
        ],
        "destructive": False,
        # Includes the news section's full English names.
        "pause_english": False,
        "run": _morning_briefing,
    },
    "CREATE_REMINDER": {
        # Matched by prefix in route(), not exact phrase — listed here so it
        # appears in LIST_COMMANDS, the home page, and the Whisper prompt.
        "phrases": ["提我", "提醒我", "remind me"],
        "destructive": False,
        "run": lambda: "講提我加埋內容同時間，例如提我聽日朝早九點買牛奶",
    },
    "RESTART_ASR": {
        "phrases": [
            "重啟語音系統", "重启语音系统", "重新啟動語音系統", "重新启动语音系统",
            "重啟语音系统",  # observed 2026-07-13: Whisper mixes scripts
            # observed 2026-07-14: user naturally says 重置/系統重置/重啟系統
            "重置語音系統", "重置语音系统", "系統重置", "系统重置",
            "重啟系統", "重启系统", "重启系統",  # last: mixed-script as Whisper emits it
            "restart voice system",
        ],
        "destructive": False,
        "run": _restart_service,
    },
    "TRIGGER_DEPLOY": {
        "phrases": ["重新部署", "重新部署網站", "redeploy", "deploy"],
        "destructive": True,
        "run": _trigger_deploy,
    },
}

CONFIRM_PHRASES = {"確認", "确认", "confirm", "yes"}
CANCEL_PHRASES = {"取消", "cancel"}

# Single-user system: one pending confirmation at a time.
_pending = {"command": None, "expires": 0.0}


def _normalize(text: str) -> str:
    # Keep word chars and CJK; drop spaces and the punctuation Whisper appends.
    return re.sub(r"[^\w一-鿿]+", "", text.lower())


def _ollama_fallback(text: str) -> dict:
    # Chat mode for phrases that match no command. Reply-only: the LLM's
    # output is spoken back, never routed into COMMANDS.
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": OLLAMA_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            reply = json.loads(resp.read()).get("message", {}).get("content", "")
    except Exception as exc:
        log.error("ollama fallback failed: %s", exc)
        return {"command": None, "status": "chat_error", "reply": "chat engine unavailable"}
    # Thinking models (qwen3, deepseek-r1) wrap reasoning in <think> tags.
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S).strip()
    if not reply:
        return {"command": None, "status": "chat_error", "reply": "chat engine returned nothing"}
    log.info("chat reply (%s): %r", OLLAMA_MODEL, reply)
    return {"command": None, "status": "chat", "reply": reply}


HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "history.jsonl")


def record_history(text: str, outcome: dict) -> None:
    # Local source of truth for conversation history (chat included);
    # ops/notion_sync.py mirrors command entries to Notion. Best-effort:
    # a history write failure must never break the spoken reply.
    entry = {
        "ts": dt.datetime.now(NZ_TZ).isoformat(timespec="seconds"),
        "text": text,
        "command": outcome.get("command"),
        "status": outcome.get("status"),
        "reply": outcome.get("reply"),
    }
    if outcome.get("data") is not None:
        entry["data"] = outcome["data"]
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.error("history write failed: %s", exc)


def _pause_english(text: str) -> str:
    # iOS TTS reading a Chinese sentence runs adjacent English words together;
    # a Chinese comma between them forces a clear pause.
    return re.sub(r"(?<=[A-Za-z'])[ ](?=[A-Za-z'])", "，", text)


def _execute(command_id: str) -> dict:
    try:
        out = COMMANDS[command_id]["run"]()
        # Runners may return (reply, data): data is structured history for
        # later comparisons (e.g. today vs yesterday), never spoken.
        reply, data = out if isinstance(out, tuple) else (out, None)
        if COMMANDS[command_id].get("pause_english", True):
            reply = _pause_english(reply)
        log.info("command %s reply: %r", command_id, reply)
        result = {"command": command_id, "status": "executed", "reply": reply}
        if data is not None:
            result["data"] = data
        return result
    except Exception as exc:
        log.error("command %s failed: %s", command_id, exc)
        return {"command": command_id, "status": "error", "reply": "command failed"}


def route(text: str) -> dict:
    phrase = _normalize(text)
    if not phrase:
        return {"command": None, "status": "no_match", "reply": "nothing heard"}

    if phrase in CONFIRM_PHRASES:
        pending = _pending["command"]
        if pending and time.time() < _pending["expires"]:
            _pending["command"] = None
            return _execute(pending)
        return {"command": None, "status": "no_match", "reply": "nothing to confirm"}

    if phrase in CANCEL_PHRASES:
        _pending["command"] = None
        return {"command": None, "status": "cancelled", "reply": "cancelled"}

    if phrase.startswith(REMINDER_PREFIXES):
        return _create_reminder(text)

    for command_id, spec in COMMANDS.items():
        if phrase in (_normalize(p) for p in spec["phrases"]):
            if spec["destructive"]:
                _pending["command"] = command_id
                _pending["expires"] = time.time() + CONFIRM_TTL_SECONDS
                return {
                    "command": command_id,
                    "status": "needs_confirmation",
                    "reply": f"say 確認 to run {command_id}",
                }
            return _execute(command_id)

    return _ollama_fallback(text)
