"""Command router: maps transcribed phrases to allowlisted commands.

Hardening rules (CLAUDE.md checklist): destructive commands match only exact
allowlisted phrases after normalization, and require a confirmation phrase
within CONFIRM_TTL_SECONDS before executing.
"""

import logging
import os
import re
import time
import urllib.request

log = logging.getLogger("router")

CONFIRM_TTL_SECONDS = 60


def _system_status() -> str:
    return "voice OS online"


def _trigger_deploy() -> str:
    hook = os.environ.get("DEPLOY_HOOK_URL")
    if not hook:
        return "deploy hook not configured"
    with urllib.request.urlopen(hook, data=b"") as resp:
        return f"deploy triggered ({resp.status})"


COMMANDS = {
    "SYSTEM_STATUS": {
        "phrases": ["系統狀態", "系统状态", "system status", "status"],
        "destructive": False,
        "run": _system_status,
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

    return {"command": None, "status": "no_match", "reply": f"no command for: {text}"}
