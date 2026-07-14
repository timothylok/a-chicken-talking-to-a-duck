"""Mirror voice command history (asr/logs/history.jsonl) to a Notion database.

Runs every 5 min via the "VoiceOS Notion Sync" scheduled task as the logged-in
user, so the Notion key never enters the VoiceASR service environment
(CLAUDE.md credential-isolation item). Chat entries (command=null) stay
local-only by design.

Setup:
  1. notion.so/my-integrations -> new internal integration -> copy the key
  2. Share a Notion page with the integration (page ... -> Connections)
  3. Write ops/notion.json (gitignored — holds the key):  {"api_key": "ntn_..."}
  4. python ops/notion_sync.py --setup <parent_page_id>
     (creates the database and adds database_id to the config)

Until the config exists, runs are silent no-ops.
"""

import json
import logging
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY = os.path.join(ROOT, "asr", "logs", "history.jsonl")
CURSOR = os.path.join(ROOT, "asr", "logs", "notion_sync.cursor")
LOG_PATH = os.path.join(ROOT, "asr", "logs", "notion_sync.log")
CONFIG = os.path.join(ROOT, "ops", "notion.json")
NOTION_VERSION = "2022-06-28"

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("notion_sync")


def _notion(method: str, path: str, payload: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"notion {exc.code} on {path}: {detail}") from exc


def _rich(text: str) -> list:
    # Notion caps a text block at 2000 chars.
    return [{"type": "text", "text": {"content": text[:2000]}}]


def _page_properties(entry: dict) -> dict:
    props = {
        "Transcript": {"title": _rich(entry.get("text") or "")},
        "When": {"date": {"start": entry["ts"]}},
        "Command": {"select": {"name": entry["command"]}},
        "Status": {"select": {"name": entry.get("status") or "unknown"}},
        "Reply": {"rich_text": _rich(entry.get("reply") or "")},
    }
    if entry.get("data") is not None:
        props["Data"] = {"rich_text": _rich(json.dumps(entry["data"], ensure_ascii=False))}
    return props


def create_database(parent_page_id: str, api_key: str) -> str:
    result = _notion("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": "語音歷史 Voice History"}}],
        "properties": {
            "Transcript": {"title": {}},
            "When": {"date": {}},
            "Command": {"select": {}},
            "Status": {"select": {}},
            "Reply": {"rich_text": {}},
            "Data": {"rich_text": {}},
        },
    }, api_key)
    return result["id"]


def load_config() -> dict | None:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def sync() -> int:
    cfg = load_config()
    if not cfg or not cfg.get("api_key") or not cfg.get("database_id"):
        log.info("not configured (%s); skipping", CONFIG)
        return 0
    if not os.path.exists(HISTORY):
        return 0

    offset = 0
    if os.path.exists(CURSOR):
        with open(CURSOR, encoding="ascii") as f:
            offset = int(f.read().strip() or 0)
    if offset > os.path.getsize(HISTORY):  # history was truncated/rotated
        offset = 0

    with open(HISTORY, "rb") as f:
        f.seek(offset)
        raw = f.read()

    synced = 0
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break  # partially written line; pick it up next run
        text = line.decode("utf-8", "replace").strip()
        if text:
            try:
                entry = json.loads(text)
            except ValueError:
                log.warning("skipping corrupt history line at offset %d", offset)
                entry = None
            # Commands only: chat transcripts stay on this machine.
            if entry and entry.get("command"):
                _notion("POST", "/pages", {
                    "parent": {"database_id": cfg["database_id"]},
                    "properties": _page_properties(entry),
                }, cfg["api_key"])
                synced += 1
        offset += len(line)
        with open(CURSOR, "w", encoding="ascii") as f:
            f.write(str(offset))
    if synced:
        log.info("synced %d entries", synced)
    return synced


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--setup":
        cfg = load_config()
        if not cfg or not cfg.get("api_key"):
            print(f"write {CONFIG} with {{\"api_key\": \"ntn_...\"}} first")
            sys.exit(1)
        db_id = create_database(sys.argv[2], cfg["api_key"])
        cfg["database_id"] = db_id
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print(f"database created and saved to config: {db_id}")
        return
    try:
        sync()
    except Exception as exc:
        # Leave the cursor where it is; next run retries from the failed line.
        log.error("sync stopped: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
