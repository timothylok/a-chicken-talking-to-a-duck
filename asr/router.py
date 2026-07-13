"""Command router: maps transcribed phrases to allowlisted commands.

Hardening rules (CLAUDE.md checklist): destructive commands match only exact
allowlisted phrases after normalization, and require a confirmation phrase
within CONFIRM_TTL_SECONDS before executing.
"""

import json
import logging
import os
import re
import threading
import time
import urllib.request

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


def _execute(command_id: str) -> dict:
    try:
        reply = COMMANDS[command_id]["run"]()
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
