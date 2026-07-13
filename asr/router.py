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


def _fuel_prices() -> str:
    out = subprocess.run(
        [sys.executable, FUEL_CLI, "search", "--location", FUEL_LOCATION,
         "--fuel", "PULP95", "--limit", "3", "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    stations = json.loads(out.stdout)["stations"]
    if not stations:
        return "攞唔到油價資料"
    parts = [f"{s['name']}每公升{float(s['price']) / 100:.2f}蚊" for s in stations]
    return f"{FUEL_LOCATION.split()[0].title()}附近最平95汽油：" + "，".join(parts)


# TheColab at-transport skill: live AT departures. BUS_STOPS holds AT stop
# codes; defaults are the two Glenfield Mall stops (one per direction).
AT_CLI = "D:/ai/thecolab-skills/skills/at-transport/scripts/cli.py"
BUS_STOPS = os.environ.get("BUS_STOPS", "3881,4010")
NZ_TZ = zoneinfo.ZoneInfo("Pacific/Auckland")


def _bus_times() -> str:
    now = dt.datetime.now(dt.timezone.utc)
    stop_name, departures = "", []
    for stop in BUS_STOPS.split(","):
        out = subprocess.run(
            [sys.executable, AT_CLI, "departures", stop.strip(), "--json"],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        data = json.loads(out.stdout)
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
    return f"{stop_name}巴士：" + "，".join(parts)


# TheColab nz-tides-surf skill: LINZ tide predictions, keyless.
TIDES_CLI = "D:/ai/thecolab-skills/skills/nz-tides-surf/scripts/cli.py"
TIDE_PORT = os.environ.get("TIDE_PORT", "auckland")


def _speak_time(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    period = "朝早" if 6 <= h < 12 else "晏晝" if 12 <= h < 18 else "夜晚" if h >= 18 else "半夜"
    h12 = h % 12 or 12
    return f"{period}{h12}點{m:02d}分" if m else f"{period}{h12}點"


def _tide_times() -> str:
    out = subprocess.run(
        [sys.executable, TIDES_CLI, "next-tide", TIDE_PORT, "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    data = json.loads(out.stdout)
    events = data["events"][:2]
    if not events:
        return "攞唔到潮汐資料"
    names = {"high": "潮漲", "low": "潮退"}
    parts = [
        f"{names.get(e['type'], e['type'])}{_speak_time(e['time_local'])}，{e['height_m']}米"
        for e in events
    ]
    return f"{data['resolved_port']}潮汐：" + "，".join(parts)


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


def _weather_today() -> str:
    with urllib.request.urlopen(OPEN_METEO_URL, timeout=10) as resp:
        data = json.loads(resp.read())
    current = data["current"]
    daily = data["daily"]
    return (
        f"奧克蘭而家{round(current['temperature_2m'])}度，"
        f"{_describe_weather(current['weather_code'])}。"
        f"今日最高{round(daily['temperature_2m_max'][0])}度，"
        f"最低{round(daily['temperature_2m_min'][0])}度，"
        f"落雨機會百分之{daily['precipitation_probability_max'][0]}"
    )


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
    "FUEL_PRICES": {
        "phrases": [
            "油價", "油价", "油價幾多", "今日油價", "汽油價錢", "汽油价钱",
            "邊度入油平", "入油", "fuel price", "fuel prices", "petrol price",
            "gas price",
        ],
        "destructive": False,
        "run": _fuel_prices,
    },
    "BUS_TIMES": {
        "phrases": [
            "巴士", "巴士幾時", "幾時有巴士", "巴士時間", "巴士时间", "巴士班次",
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
    "RESTART_ASR": {
        "phrases": [
            "重啟語音系統", "重启语音系统", "重新啟動語音系統", "重新启动语音系统",
            "重啟语音系统",  # observed 2026-07-13: Whisper mixes scripts
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


def _pause_english(text: str) -> str:
    # iOS TTS reading a Chinese sentence runs adjacent English words together;
    # a Chinese comma between them forces a clear pause.
    return re.sub(r"(?<=[A-Za-z'])[ ](?=[A-Za-z'])", "，", text)


def _execute(command_id: str) -> dict:
    try:
        reply = _pause_english(COMMANDS[command_id]["run"]())
        log.info("command %s reply: %r", command_id, reply)
        return {"command": command_id, "status": "executed", "reply": reply}
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
