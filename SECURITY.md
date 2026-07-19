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
| ntfy topic | `ops/ntfy.json` (gitignored, deny-ACLed) | heartbeat / milk-watch / reminder-alerts tasks, run as user |
| Gateway key + CF Access token | `gateway/.env` (gitignored, deny-ACLed) + Vercel env | Vercel only; local copy for reference |
| Google OAuth client + refresh token (`calendar.events.readonly` only) | `ops/google.json` (gitignored, deny-ACLed) | "VoiceOS Calendar Sync" task, runs as user |
| Slack signing secret + bot token | `gateway/.env` (same file as above) + Vercel env | Vercel only; never on the Win11 box |
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

## External integrations (the "plugin" pattern, added 2026-07-17)

Every integration with an authenticated external service follows one of two
shapes. Google Calendar (`ops/google_calendar.py` + `TODAY_AGENDA`) is the
reference implementation for reads.

**Reads — synced sanitized cache:**
a user-context scheduled task holds the credentials, pulls from the provider
on a cadence, sanitizes the response to the minimum fields the reply needs,
and atomically writes a cache file under `asr\cache`. Commands only ever
read the file. The service never holds a token; a compromised service sees
only pre-minimized data; a dead task or revoked grant shows up as staleness,
which the command reports in friendly Cantonese.

**Writes — marker-file hand-off** (as above), plus an idempotency key in the
marker and the existing spoken-確認 flow for anything destructive. Not yet
exercised; build it when the first write integration lands.

Rules for every integration, no exceptions:

- Narrowest OAuth scope that works (calendar: `calendar.events.readonly`,
  not `calendar`).
- Credentials in `ops/<provider>.json` — gitignored **and** deny-ACLed to
  the service account (add the file to `ops/harden_voiceasr.ps1`).
- Silent no-op when unconfigured; its own log under `asr\logs`.
- Sanitized data only in service-readable files (titles/times, never
  attendees, bodies, or IDs).
- Errors map to spoken Cantonese, never leaked internals; staleness beats
  silence.
- No LLM ever selects or parameterizes an integration call — commands are
  exact-phrase allowlist entries like everything else.

**Workflow rules** (`ops/workflows.json` + "VoiceOS Workflows" task) follow
the same discipline: the config is owner-edited only (the service account is
read-only on the code tree), actions are limited to ntfy pushes, allowlisted
**non-destructive** commands (destructive IDs are refused so a workflow can
never arm the spoken-確認 window), and plain webhooks; every firing is
logged to `asr\logs\workflows.log`, and workflow-run commands are tagged
`source: "workflow"` in history. Workflow-triggered history entries are
ignored as triggers (no feedback loops).

## Slack bridge surface (added 2026-07-17)

`gateway/api/slack.ts` is a second inbound path, but it terminates at Vercel —
nothing new reaches the Win11 box directly:

- Every request must carry a valid Slack HMAC-SHA256 signature
  (timing-safe compare, ±5 min replay bound); unsigned/forged traffic gets 401.
- Only `app_mention` events are processed (the app is deliberately not
  subscribed to channel messages); other bots' messages are ignored.
- The extracted text is forwarded through the **existing** `/api/voice`
  path with the gateway key, so auth, rate limiting, idempotency, and
  Cloudflare Access all apply unchanged — the router's exact-match allowlist
  and confirmation flow are the same as for voice.
- Slack delivery retries are acked and ignored (no double execution);
  each forwarded command carries the Slack event timestamp so distinct
  requests are never falsely deduplicated.
- Per-channel throttle (2026-07-18): max 3 command executions per channel
  per minute, so a mention burst can't queue slow commands into silent
  reply drops or tie up the ASR box; over-limit mentions get an immediate
  "too fast" reply. Reply posting retries once (429 `Retry-After`
  honoured) — feedback is never dropped silently.
- The bot token's scopes are `chat:write` + `app_mentions:read` +
  `files:write` (image uploads for GENERATE_IMAGE); a leaked token can post
  messages and files, not read history or join channels.
- GENERATE_IMAGE (2026-07-19) keeps credential isolation: the image is
  generated locally (CPU, offline HF model) and returned as base64 in the
  command response; the bridge does the Slack upload — the local box still
  never holds a Slack token. The prompt is free text but only ever a
  subprocess argument (arg list, no shell) and file content, never a
  command. Slack-source only, enforced in the router.
- Command history entries record their channel (`source`: voice/text/slack)
  for auditability in `history.jsonl`, `service.log`, and the Notion mirror.

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

## File handling (audited 2026-07-16)

The request path never turns client input into a path or command:

- Client filenames are never read — the ASR server takes `upload.read()`
  bytes; the gateway forwards the raw body without parsing the multipart.
- Audio decodes in-memory (`io.BytesIO` → faster-whisper's embedded PyAV);
  there is no ffmpeg command line and no shell anywhere in the request path.
- The only subprocesses (`_run_skill` skill CLIs) use argument lists (never
  `shell=True`) built from owner-set env vars or `int()`-validated IDs.
- Validation at both hops: content-type allowlist, 4 MB gateway / 16 MB local
  size caps before decode, 120 s duration cap before GPU inference,
  undecodable audio → 400. Residual risk is a decoder-library parsing bug,
  contained by the service-account isolation above.
- The only file written from request data is `history.jsonl` (JSON-encoded,
  fixed path).

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
