"""Google Calendar read-only sync -> asr/cache/calendar.json.

Reference implementation of the external-integration pattern (SECURITY.md
"External integrations"): OAuth credentials live in ops/google.json
(gitignored, deny-ACLed); this script runs as the logged-in user via the
"VoiceOS Calendar Sync" scheduled task every 10 min; the ASR service only
ever reads the sanitized cache — event titles and times, nothing else.

Setup:
  1. console.cloud.google.com -> new project -> enable Google Calendar API
  2. OAuth consent screen: External, add yourself as a user, then PUBLISH
     to production — apps left in "Testing" expire refresh tokens after
     7 days and the sync dies silently a week later
  3. Credentials -> Create OAuth client ID -> type "Desktop app"; write
     ops/google.json:  {"client_id": "...", "client_secret": "..."}
  4. python ops/google_calendar.py --auth   (browser consent; saves the
     refresh token into the config)

Until the config holds a refresh_token, sync runs are silent no-ops.
"""

import datetime as dt
import http.server
import json
import logging
import os
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "ops", "google.json")
CACHE = os.path.join(ROOT, "asr", "cache", "calendar.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", "calendar_sync.log")

NZ_TZ = ZoneInfo("Pacific/Auckland")
# Least privilege: read events only — not calendar management.
SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("calendar_sync")


def load_config() -> dict | None:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _token_request(fields: dict) -> dict:
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"token endpoint {exc.code}: {detail}") from exc


def auth(cfg: dict) -> None:
    # Installed-app flow with loopback redirect, stdlib only.
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })

    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = query.get("code", [None])[0]
            result["state"] = query.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Done - you can close this tab.".encode())

        def log_message(self, *args):  # keep the console clean
            pass

    server.RequestHandlerClass = Handler
    print("Open this URL to authorize (read-only calendar access):\n\n" + url + "\n")
    webbrowser.open(url)
    print(f"Waiting for the browser redirect on {redirect_uri} ...")
    server.handle_request()

    if not result.get("code") or result.get("state") != state:
        sys.exit("authorization failed: no code returned (or state mismatch)")

    tokens = _token_request({
        "code": result["code"],
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    refresh = tokens.get("refresh_token")
    if not refresh:
        sys.exit("no refresh_token in response — remove the app's prior grant at "
                 "myaccount.google.com/permissions and rerun (prompt=consent should prevent this)")
    cfg["refresh_token"] = refresh
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print("Refresh token saved. Run without --auth to sync now.")


def sync(cfg: dict) -> None:
    tokens = _token_request({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": cfg["refresh_token"],
        "grant_type": "refresh_token",
    })
    access = tokens["access_token"]

    now = dt.datetime.now(NZ_TZ)
    # Today's remaining events plus all of tomorrow (briefing may want it).
    time_max = dt.datetime.combine(
        now.date() + dt.timedelta(days=2), dt.time.min, tzinfo=NZ_TZ)
    query = urllib.parse.urlencode({
        "timeMin": now.isoformat(),
        "timeMax": time_max.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "50",
    })
    req = urllib.request.Request(
        f"{EVENTS_URL}?{query}", headers={"Authorization": f"Bearer {access}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        items = json.loads(resp.read()).get("items", [])

    # Sanitize (data minimization): only titles and times reach the file the
    # ASR service can read — no attendees, description, location, or IDs.
    events = []
    for item in items:
        start, end = item.get("start", {}), item.get("end", {})
        all_day = "date" in start
        events.append({
            "title": str(item.get("summary") or "(無標題)")[:100],
            "start": start.get("date") or start.get("dateTime"),
            "end": end.get("date") or end.get("dateTime"),
            "all_day": all_day,
        })

    cache = {"fetched_at": now.isoformat(timespec="seconds"), "events": events}
    tmp = CACHE + ".tmp"
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE)
    log.info("synced %d events", len(events))


def main() -> None:
    cfg = load_config()
    if "--auth" in sys.argv:
        if not cfg or not cfg.get("client_id") or not cfg.get("client_secret"):
            sys.exit(f"write {CONFIG} with client_id/client_secret first (see docstring)")
        auth(cfg)
        return
    if not cfg or not cfg.get("refresh_token"):
        log.info("not configured (%s); skipping", CONFIG)
        return
    try:
        sync(cfg)
    except Exception as exc:
        # Revoked grant / network trouble: the command's staleness check
        # surfaces this to the user; the log has the detail.
        log.error("sync failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
