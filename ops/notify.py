"""Push a notification to the phone via ntfy.sh.

Importable helper for agents and ops scripts:  from notify import notify
Also runnable directly:  python ops/notify.py "title" "message" [priority]

Setup:
  1. Generate a long random topic (it is the only credential — anyone who
     knows it can read and send):
       node -e "console.log('voiceos-'+crypto.randomBytes(16).toString('base64url'))"
  2. Subscribe to it in the ntfy iPhone app.
  3. Write ops/ntfy.json (gitignored — holds the topic):  {"topic": "voiceos-..."}

Until the config exists, calls are silent no-ops (returns False).
Messages transit and are cached on ntfy.sh in plaintext — keep alert content
non-sensitive (say "check the dashboard", not the numbers).
"""

import base64
import json
import os
import sys
import urllib.request

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ntfy.json")


def notify(title: str, message: str, priority: int = 3) -> bool:
    """Send a push; priority 1 (min) .. 5 (urgent), default 3. False = not sent."""
    try:
        with open(CONFIG, encoding="utf-8") as f:
            topic = json.load(f)["topic"]
    except (OSError, KeyError, ValueError):
        return False
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={
            # Header values must be latin-1; RFC 2047 encoding keeps Chinese titles intact.
            "Title": "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?=",
            "Priority": str(priority),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except OSError:
        return False


def notify_discord(title: str, message: str) -> bool:
    """Post to the Discord webhook in DISCORD_WEBHOOK_URL (repo-root .env). False = not sent."""
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        try:
            with open(os.path.join(os.path.dirname(CONFIG), "..", ".env"), encoding="utf-8") as f:
                for line in f:
                    key, _, value = line.strip().partition("=")
                    if key == "DISCORD_WEBHOOK_URL":
                        url = value.strip().strip('"')
        except OSError:
            pass
    if not url:
        return False
    req = urllib.request.Request(
        url,
        data=json.dumps({"content": (f"**{title}**" + chr(10) + message)[:2000]}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "voice-ecosystem-notify"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: notify.py <title> <message> [priority 1-5]")
    ok = notify(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 3)
    print("sent" if ok else "not sent (no config or network error)")
