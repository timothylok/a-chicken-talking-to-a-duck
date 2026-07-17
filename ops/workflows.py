"""Declarative IF->THEN workflow rules (the from-scratch IFTTT replacement).

Evaluates ops/workflows.json every minute via the "VoiceOS Workflows"
scheduled task (runs as the user, like notion_sync/reminder_alerts).
Triggers are signals this system already has; actions are things it can
already do — no event bus, no cloud, no cost.

Rule shape (all "if" fields optional, AND-ed; see ops/workflows.json):

  {"id": "umbrella-alert", "enabled": true,
   "trigger": {"schedule": {"at": "07:30", "days": ["mon", ..]}},
   "if": {"command": "今日天氣", "data_gte": ["rain_prob", 70]},
   "then": [{"ntfy": {"title": "帶遮", "message": "{reply}"}}]}

Triggers:
  schedule {at, days?}   — fires once per day when now >= at (late catch-up ok)
  history  {command?, status?, source?, text_contains?}
                         — new history.jsonl entries, byte cursor
Conditions ("if"): command (phrase to run for its reply/data — schedule rules
  only), reply_contains, data_contains [path, substr], data_gte/data_lte
  [path, number]. Strings may use {today}/{tomorrow} (rendered "17 July" to
  match skill date formats) and, in action templates, {reply}/{data.path}.
Actions ("then"): ntfy {title, message, priority?};
  command {text} — resolved against the router allowlist, destructive
  commands refused (a workflow must never arm the 確認 window);
  webhook {url, body?}.

Security: this config is owner-edited only (code tree is read-only to the
service account); no LLM writes or selects anything here.
"""

import datetime as dt
import json
import logging
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "ops", "workflows.json")
HISTORY = os.path.join(ROOT, "asr", "logs", "history.jsonl")
STATE = os.path.join(ROOT, "asr", "logs", "workflows_state.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", "workflows.log")
COMMAND_URL = "http://localhost:9000/command"
sys.path.insert(0, os.path.join(ROOT, "asr"))
sys.path.insert(0, os.path.join(ROOT, "ops"))

NZ_TZ = ZoneInfo("Pacific/Auckland")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("workflows")

DRY_RUN = "--dry-run" in sys.argv


def _date_token(d: dt.date) -> str:
    # "17 July" — matches the skill CLIs' spoken-date fragments (no zero pad).
    return f"{d.day} {d:%B}"


def _render(text: str, outcome: dict) -> str:
    now = dt.datetime.now(NZ_TZ)
    text = text.replace("{today}", _date_token(now.date()))
    text = text.replace("{tomorrow}", _date_token(now.date() + dt.timedelta(days=1)))
    text = text.replace("{reply}", str(outcome.get("reply") or ""))
    text = text.replace("{text}", str(outcome.get("text") or ""))
    text = text.replace("{command}", str(outcome.get("command") or ""))
    while "{data." in text:
        start = text.index("{data.")
        end = text.index("}", start)
        value = _dig(outcome.get("data"), text[start + 6:end])
        text = text[:start] + str(value if value is not None else "") + text[end + 1:]
    return text


def _dig(data, path: str):
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def _run_command(text: str) -> dict:
    payload = json.dumps({"text": text, "source": "workflow"}).encode()
    req = urllib.request.Request(
        COMMAND_URL, data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def _resolve_command(text: str) -> "tuple[str, bool] | None":
    """(command_id, destructive) from the router allowlist; None = no match."""
    from router import COMMANDS, _normalize

    phrase = _normalize(text)
    for cmd_id, spec in COMMANDS.items():
        if phrase in (_normalize(p) for p in spec["phrases"]):
            return cmd_id, bool(spec.get("destructive"))
    return None


def _condition_passes(cond: dict, outcome: dict) -> bool:
    ok = True
    if cond.get("reply_contains"):
        ok &= _render(cond["reply_contains"], {}) in str(outcome.get("reply") or "")
    if cond.get("data_contains"):
        path, sub = cond["data_contains"]
        ok &= _render(sub, {}) in str(_dig(outcome.get("data"), path) or "")
    if cond.get("data_gte"):
        path, n = cond["data_gte"]
        value = _dig(outcome.get("data"), path)
        ok &= isinstance(value, (int, float)) and value >= n
    if cond.get("data_lte"):
        path, n = cond["data_lte"]
        value = _dig(outcome.get("data"), path)
        ok &= isinstance(value, (int, float)) and value <= n
    return ok


def _run_actions(rule: dict, outcome: dict) -> None:
    from notify import notify

    for action in rule.get("then", []):
        try:
            if "ntfy" in action:
                spec = action["ntfy"]
                if DRY_RUN:
                    print(f"[dry-run] {rule['id']}: ntfy {_render(spec['title'], outcome)!r}")
                    continue
                sent = notify(
                    _render(spec["title"], outcome),
                    _render(spec.get("message", "{reply}"), outcome),
                    int(spec.get("priority", 3)),
                )
                log.info("%s: ntfy %s", rule["id"], "sent" if sent else "NOT sent")
            elif "command" in action:
                text = action["command"]["text"]
                resolved = _resolve_command(text)
                if resolved is None:
                    log.warning("%s: refused %r — not an allowlisted command", rule["id"], text)
                    continue
                cmd_id, destructive = resolved
                if destructive:
                    log.warning("%s: refused %s — destructive commands cannot be "
                                "workflow actions", rule["id"], cmd_id)
                    continue
                if DRY_RUN:
                    print(f"[dry-run] {rule['id']}: command {cmd_id}")
                    continue
                result = _run_command(text)
                log.info("%s: command %s -> %s", rule["id"], cmd_id, result.get("status"))
            elif "webhook" in action:
                spec = action["webhook"]
                if DRY_RUN:
                    print(f"[dry-run] {rule['id']}: webhook {spec['url']}")
                    continue
                body = _render(spec.get("body", "{reply}"), outcome).encode("utf-8")
                req = urllib.request.Request(spec["url"], data=body)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    log.info("%s: webhook %s -> %d", rule["id"], spec["url"], resp.status)
        except Exception as exc:
            log.error("%s: action failed: %s", rule["id"], exc)  # next action still runs


def _fire(rule: dict, outcome: dict) -> None:
    cond = rule.get("if") or {}
    # Schedule rules may name a command to run as their condition source.
    if cond.get("command"):
        try:
            outcome = _run_command(cond["command"])
        except Exception as exc:
            log.error("%s: condition command failed: %s", rule["id"], exc)
            return
    if not _condition_passes(cond, outcome):
        log.info("%s: condition not met", rule["id"])
        return
    _run_actions(rule, outcome)


def _check_schedules(rules: list, state: dict, now: dt.datetime) -> None:
    for rule in rules:
        sched = rule.get("trigger", {}).get("schedule")
        if not sched or not rule.get("enabled", True):
            continue
        if sched.get("days") and DAYS[now.weekday()] not in sched["days"]:
            continue
        at = dt.datetime.strptime(sched["at"], "%H:%M").time()
        today = now.date().isoformat()
        if now.time() >= at and state["last_fired"].get(rule["id"]) != today:
            state["last_fired"][rule["id"]] = today
            log.info("%s: schedule trigger fired", rule["id"])
            _fire(rule, {})


def _check_history(rules: list, state: dict) -> None:
    history_rules = [
        r for r in rules
        if r.get("trigger", {}).get("history") is not None and r.get("enabled", True)
    ]
    if not os.path.exists(HISTORY):
        return
    if state["cursor"] > os.path.getsize(HISTORY):  # truncated/rotated
        state["cursor"] = 0
    with open(HISTORY, "rb") as f:
        f.seek(state["cursor"])
        raw = f.read()
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        state["cursor"] += len(line)
        try:
            entry = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        for rule in history_rules:
            t = rule["trigger"]["history"]
            if t.get("command") and entry.get("command") != t["command"]:
                continue
            if t.get("status") and entry.get("status") != t["status"]:
                continue
            if t.get("source") and entry.get("source") != t["source"]:
                continue
            if t.get("text_contains") and t["text_contains"] not in (entry.get("text") or ""):
                continue
            # Never react to our own workflow-run commands (feedback loop).
            if entry.get("source") == "workflow":
                continue
            log.info("%s: history trigger matched %r", rule["id"], entry.get("text"))
            _fire(rule, entry)


def main() -> None:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            rules = json.load(f)["rules"]
    except FileNotFoundError:
        return  # unconfigured: silent no-op
    try:
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        # First run: start the cursor at end-of-file so old history (weeks of
        # entries) doesn't fire a burst of alerts.
        state = {"cursor": os.path.getsize(HISTORY) if os.path.exists(HISTORY) else 0,
                 "last_fired": {}}
    before = json.dumps(state, sort_keys=True)
    _check_schedules(rules, state, dt.datetime.now(NZ_TZ))
    _check_history(rules, state)
    if not DRY_RUN and json.dumps(state, sort_keys=True) != before:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f)


if __name__ == "__main__":
    main()
