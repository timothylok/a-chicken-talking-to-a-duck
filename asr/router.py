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
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

# Populated by server.py at startup.
status_info = {"model": "?", "device": "?", "started": time.time()}


def _system_status() -> str:
    hours = (time.time() - status_info["started"]) / 3600
    return (
        f"系統正常，模型 {status_info['model']}，"
        f"設備 {status_info['device']}，已運行 {hours:.1f} 小時"
    )


def _restart_service() -> str:
    # The router runs inside the VoiceASR service, so it cannot stop/start
    # itself via the SCM. Instead: reply first, then exit — NSSM's default
    # AppExit action relaunches the app. Daemon thread so tests don't die.
    timer = threading.Timer(2.0, os._exit, [0])
    timer.daemon = True
    timer.start()
    log.warning("restart requested via voice command; exiting in 2 s")
    return "語音系統重啟中，約三十秒後恢復"


def _trigger_deploy() -> str:
    hook = os.environ.get("DEPLOY_HOOK_URL")
    if not hook:
        return "deploy hook not configured"
    with urllib.request.urlopen(hook, data=b"") as resp:
        return f"deploy triggered ({resp.status})"


COMMANDS = {
    "SYSTEM_STATUS": {
        "phrases": [
            "系統狀態", "系统状态", "檢查系統狀態", "检查系统状态",
            "健康檢查", "健康检查", "system status", "status", "health check",
        ],
        "destructive": False,
        "run": _system_status,
    },
    "RESTART_ASR": {
        "phrases": [
            "重啟語音系統", "重启语音系统", "重新啟動語音系統", "重新启动语音系统",
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
        "prompt": (
            "用香港口語廣東話回覆，一至兩句，唔好用書面語，唔好用普通話詞彙。"
            f"用戶講咗：\n{text}"
        ),
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            reply = json.loads(resp.read()).get("response", "").strip()
    except Exception as exc:
        log.error("ollama fallback failed: %s", exc)
        return {"command": None, "status": "chat_error", "reply": "chat engine unavailable"}
    if not reply:
        return {"command": None, "status": "chat_error", "reply": "chat engine returned nothing"}
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
