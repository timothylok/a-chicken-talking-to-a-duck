# Security — credential isolation

Why and how the voice OS separates the internet-facing ASR service from the
credentials the automations use. Companion to the hardening checklist in
CLAUDE.md.

## Threat model

The VoiceASR service is the one process on the Win11 box that continuously
consumes input from the internet: audio bytes arrive through the Cloudflare
tunnel and are fed into ffmpeg/faster-whisper decoders, plus JSON text into
the command router. Decoders are the classic soft spot — a memory-safety bug
in audio parsing is the realistic way an attacker who obtained the gateway
key (or Cloudflare Access token) turns "can send requests" into "can run code
on the box". The router's exact-match allowlist protects against *prompt*
injection, but not against a bug in the parsing layer beneath it.

Until 2026-07-16 the service ran as `LocalSystem`: a compromise of the
service was a compromise of the entire machine, making credential placement
moot.

## Measure 1 — run the service as a low-privilege account

VoiceASR runs as the virtual service account `NT SERVICE\VoiceASR` (no
password to manage; identity exists only for this service). A compromise of
the service is then contained to what that account can touch:

| Path | Access | Why |
|---|---|---|
| `D:\ai\voice-ecosystem` | read/execute | code, venv, data files |
| `D:\ai\voice-ecosystem\asr\logs` | modify | service.log, history.jsonl |
| `D:\ai\voice-ecosystem\asr\cache` | modify | grocer parquet cache (`GROCER_NZ_CACHE`), temp (`TMP`/`TEMP`) |
| `D:\ai\thecolab-skills` | read/execute | NZ data skill CLIs |
| `C:\Users\timlo\.cache\huggingface` | modify | model cache (revision checks write lock files) |
| `ops/notion.json`, `ops/ntfy.json`, `gateway/.env` | **explicit deny** | secrets — see Measure 2 |

Supporting changes (all applied by `ops/harden_voiceasr.ps1`):

- `nssm.exe` copied to `C:\Program Files\nssm\` — the WinGet install lives
  inside the user profile, unreadable to a service account.
- `SeServiceLogonRight` granted to the virtual account via secedit (`sc.exe`
  does not grant it automatically).
- `TMP`/`TEMP` and `GROCER_NZ_CACHE` pointed at `asr\cache` — the virtual
  account has no usable home directory, so anything that writes to `~` or
  the system temp must be redirected explicitly.

Cloudflared still runs as `LocalSystem`. It only proxies bytes and does not
parse untrusted content, so it is lower risk — but moving it to
`NT SERVICE\Cloudflared` (read access to `C:\Users\timlo\.cloudflared`) is a
sensible follow-up.

## Measure 2 — keep agent credentials out of the service's reach

Design rule: **the ASR service holds zero credentials.** Anything that needs
a secret runs in a separate process under the user account, consuming data
the service wrote.

Current inventory:

| Credential | Where it lives | Who reads it |
|---|---|---|
| Notion API key | `ops/notion.json` (gitignored, deny-ACLed) | "VoiceOS Notion Sync" task, runs as user |
| ntfy topic | `ops/ntfy.json` (gitignored, deny-ACLed) | heartbeat / milk-watch tasks, run as user |
| Gateway key + CF Access token | `gateway/.env` (gitignored, deny-ACLed) + Vercel env | Vercel only; local copy for reference |
| VoiceASR service env | registry `AppEnvironmentExtra` | model path, fuel location, property ID — nothing sensitive |

The deny ACEs make the separation enforcement, not convention: even with
read access to the repo, the service account cannot open the three secret
files.

**Future commands that need secrets** (e.g. `TRIGGER_DEPLOY` +
`DEPLOY_HOOK_URL`) must use a hand-off pattern instead of putting the
credential in the service env: the router writes a "requested" marker file
under `asr\logs`, and a scheduled task running as the user watches for the
marker and performs the privileged action. Costs up to a minute of latency;
buys a service that never holds a secret.

## Residual risks / open items

- ~~`Authenticated Users` Modify on the `D:\ai` tree~~ **Closed 2026-07-16**
  via `ops/tighten_acls.ps1`: inheritance broken on `D:\ai\voice-ecosystem`
  and `D:\ai\thecolab-skills`, blanket Modify removed, `timlo` granted
  explicit Full Control. The service account is now read-only on code
  (write only to `asr\logs` and `asr\cache`). Side effect: the service can
  no longer write `__pycache__`, so Python skips bytecode caching at startup.
- Cloudflared on `LocalSystem` (lower risk, see above).
- Ollama listens on localhost with no auth; a compromised service account
  can use it (reply-only — it cannot trigger commands).

## Transcript retention

Decided 2026-07-16, enforced by `ops/prune_logs.py` via the daily
"VoiceOS Log Prune" scheduled task (03:32, runs as the user):

| Data | Where | Retention |
|---|---|---|
| Chat transcripts (`command == null`) | `asr/logs/history.jsonl` | **30 days** |
| Command entries | `asr/logs/history.jsonl` | forever (mirrored to Notion) |
| Rotated service logs (all transcripts) | `asr/logs/service-*.log` | **90 days** (phrase-mining window) |
| Notion 語音歷史 DB | Notion cloud | forever — commands only, chat never leaves the machine |

The pruner rewrites `history.jsonl` atomically and only within the region the
Notion sync's byte-offset cursor has already passed, shifting the cursor by
the bytes removed — nothing is double-synced or lost. If the service appends
mid-prune, the run aborts and retries the next day.
