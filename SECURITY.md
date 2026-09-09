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
shapes. (Google Calendar was the reference implementation for reads; removed
2026-07-29 — no paid Google Workspace license, so OAuth setup was never
completable. No reads integration is currently live; follow the shape below
when the next one is built.)

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

## Log header audit (2026-09-09)

Closes the last open item on the hardening checklist: *are `Authorization`
headers or audio bodies reaching Vercel, Cloudflare, or local logs?* Answer:
no, on every surface that could be inspected.

**Code paths.** The gateway has 8 `console.error` calls (all in
`gateway/api/slack.ts`); every one logs a Slack API error code or an HTTP
status, none logs a header, body, or env value. `gateway/api/voice.ts` reads
`authorization` (line 65) and sets `CF-Access-Client-Id/Secret` (lines 123-124)
but never logs either. No logging call in `asr/*.py` touches a header or token.

**Vercel.** Runtime-log retention on this plan is 1 hour, so a retrospective
audit is not possible by construction — the evidence has to be generated. A
probe request was sent to production carrying a canary value in the
`Authorization` header (wrong key, so it 401s at `keyMatches` and writes
nothing to `history.jsonl` or Notion). The only line Vercel recorded was:

    POST /api/voice 0 [info/serverless]  dep=dpl_7zxJNDbeSd287azYpU9eU3FmZkrM

No headers, no body. Full-text searches for the canary value and for `Bearer`
over the same window returned nothing. Vercel records request metadata
(method, path, status, duration, deployment), not request headers.

**Cloudflare.** `logs/cloudflared.log` covers the whole life of the system
(2026-07-11 → 2026-09-08, ~2 MB) at `level=info` with zero `DBG` lines. Zero
matches for `authorization`, `bearer`, `cf-access`, `client-secret`, `token=`
or `key=`. Note that raising cloudflared's log level to `debug` would change
this — leave it at info.

**Local.** `asr/logs/service.log`, the rest of `asr/logs/`, and the 16 MB
`logs/ollama.log` are all clean on the same patterns. Audio capture
(`ASR_CAPTURE_DIR`, `asr/server.py:97`) is opt-in, is **not** set in the live
`VoiceASR` service environment, and no capture directory or audio file exists
on disk.

**Limit of this audit.** Cloudflare Access's own dashboard-side authentication
log could not be inspected from here. It records the service token's *client
ID* on each auth event by design; the client *secret* is never logged. Transcripts
in `history.jsonl` are deliberate and governed by § Transcript retention — they
are not a finding.

## Latency benchmark (2026-09-09)

Closes the last hardening item. Measured per hop so the budget is attributable,
not just a single end-to-end number. Test clips were **synthesised locally**
(Windows SAPI -> ffmpeg AAC/m4a mono 44.1 kHz, matching what iOS sends) at 2.0 s
and 7.2 s, since the real captured clips were deleted in 2026-07-16 per the
retention rules. Content is English, which is irrelevant to timing — Whisper
cost tracks audio duration, not language — but it does mean these numbers
measure the *pipeline*, not Cantonese accuracy (that is `ops/asr_bench.py`'s
job). Each clip carried unique metadata so the gateway's 60 s body-hash dedupe
could not 409 the repeats.

| Hop | 2.0 s clip | 7.2 s clip |
|---|---|---|
| A. ASR direct (`localhost:9000`) | 1.25-1.48 s | 1.49-2.77 s |
| B. + Cloudflare tunnel | 1.18-1.51 s | 1.67-1.78 s |
| C. + Vercel gateway (full path) | 1.93 s warm, 2.79-3.52 s cold | 2.45-3.30 s |
| Router only (matched command) | 0.21 s | — |
| Chat fallback (Ollama, unmatched) | 16.9-32.3 s | — |

**The tunnel is free.** Hop B is indistinguishable from hop A — TLS handshake is
~0.04 s on a reused connection and ASR compute dominates. Cloudflare is not
where latency goes, so don't optimise it.

**Verdict: the <3 s target holds for the normal case and not the edges.** A warm
matched voice command is ~1.93 s of transcription plus ~0.21 s of routing,
about **2.1 s end-to-end**. Two things break it: a cold Vercel function adds
roughly 0.9-1.6 s (first two runs were 2.79 s and 3.52 s before settling), and a
7 s utterance costs ~3.3 s. Both are tolerable; neither is worth engineering
away for a personal system.

**The real outlier is the chat fallback at 17-32 s**, 6-10x over target. That is
local LLM generation, not the ASR path, and the <3 s target was only ever about
command routing — but it is the number a user actually feels when speech does
not match a command, so it is the one to attack if latency ever becomes a
complaint. A first cold ASR request also costs ~2.5 s against ~1.3 s warm.

## Hardening checklist (2026-07-11 design review)

Moved here from `CLAUDE.md` on 2026-08-27: 15 of 17 items are closed, so this is a
record rather than live guidance. The two still-open items are also tracked in
`CLAUDE.md` § Hardening checklist, which is where they get worked.

### Hardening checklist

Findings from the 2026-07-11 design review, in priority order. Check items off as they are implemented.

#### Critical — do before anything goes live

- [x] **Lock the tunnel with Cloudflare Access service tokens.** *(Done 2026-07-11: app `voice-asr`, service token `voice-gateway`.)* The tunnel hostname is public and bypasses the Vercel gateway key entirely. Gateway sends `CF-Access-Client-Id`/`CF-Access-Client-Secret` headers; Cloudflare rejects all other traffic at the edge so the Win11 box never sees unauthenticated requests.
- [x] **Command allowlist, not fuzzy matching.** *(Done 2026-07-11: `asr/router.py`, exact normalized-phrase match only.)* Destructive commands (deploys, writes) match only exact allowlisted phrases. If an LLM router is added, it may only output a command ID from a fixed enum — never free-form actions (prompt-injection guard).
- [x] **Confirmation step for destructive commands.** *(Done 2026-07-11: destructive commands return `needs_confirmation`; 確認/confirm within 60 s executes, 取消/cancel clears.)* Echo the transcription back and require confirm before executing (also guards against Cantonese misrecognition).
- [x] **Upload limits at the gateway.** *(Done 2026-07-14: 4 MB body cap, content-type validation, and per-instance rate limit (10/min) in `gateway/api/voice.ts`; audio duration capped at 120 s in `asr/server.py`.)* Max file size, max audio duration, content-type validation, rate limiting. ASR inference is compute-heavy — unlimited uploads = trivial DoS of the Win11 box. Note Vercel's ~4.5 MB body limit: enforce compressed audio (AAC/Opus) client-side.

#### High — do before daily use

- [x] **Key hygiene.** *(Done 2026-07-15: timing-safe compare was already in `gateway/api/voice.ts`; added required `X-Timestamp` header (ISO 8601, ±5 min) to bound replay; rotation procedure documented under Security model. Shortcut must send the new header — see `iphone-shortcut.md` step 2.)* Never share the iOS Shortcut containing the key via iCloud.
- [x] **Isolate ASR from agent credentials.** *(Done 2026-07-16: VoiceASR runs as virtual account `NT SERVICE\VoiceASR` via `ops/harden_voiceasr.ps1`; deny ACEs on `ops/notion.json`, `ops/ntfy.json`, `gateway/.env`; grocer cache + TMP redirected to `asr/cache`. See SECURITY.md — incl. the Authenticated Users residual risk.)* ASR service runs under a low-privilege account/container; agent credentials (Notion, Vercel hooks, quant jobs) live in a separate process the ASR service cannot read.
- [x] **File handling safety.** *(Audited 2026-07-16 — satisfied by design, no changes needed: client filenames never read (server uses `upload.read()` only; gateway forwards raw bytes); decoding is in-memory BytesIO→PyAV, no ffmpeg CLI, no shell; `_run_skill` subprocesses use arg lists with owner-set/int-validated values; size+content-type validated at both hops, duration capped before inference. See SECURITY.md.)* Never use client-supplied filenames in paths or shell commands (path traversal / command injection via ffmpeg); validate audio before decoding.
- [x] **Keep secrets and audio out of logs.** *(First pass 2026-07-16: benchmark audio capture removed and deleted; transcript retention decided and enforced — chat 30 d, rotated logs 90 d, commands forever, via "VoiceOS Log Prune" daily task + `ops/prune_logs.py`. Closed 2026-09-09 by the Vercel/Cloudflare header audit — see § Log header audit below.)* Authorization headers and audio bodies must not appear in Vercel, Cloudflare, or local logs; decide deliberately where transcripts are stored and for how long.

#### Reliability / usability

- [x] **Run cloudflared and the ASR server as auto-restarting Windows services**; disable sleep/hibernate on the Win11 box. *(Done 2026-07-11: services `Cloudflared` and `VoiceASR` (NSSM), both auto-start; AC sleep/hibernate disabled.)*
- [x] **Health check + external uptime ping** so silent failure (sleep, Windows Update reboot, dead tunnel) gets noticed. *(Done 2026-07-12: `ops/heartbeat.ps1` via "VoiceOS Heartbeat" scheduled task every 10 min → healthchecks.io, 30-min period; /fail ping with reason on detected failure.)*
- [x] **User feedback channel.** Push or spoken confirmation of success/failure — never silent execution. *(Done 2026-07-12: shortcut speaks the router's `reply`, with an error branch for failures.)*
- [x] **Idempotency keys at the gateway** so double-taps/retries don't run a command twice. *(Done 2026-07-15: SHA-256 body dedupe in `gateway/api/voice.ts` — identical bytes within 60 s get 409; per-instance best-effort like the rate limit. Catches network retries; two separate recordings are two commands by design.)*
- [x] **Benchmark latency on real hardware.** *(Done 2026-09-09 — see § Latency benchmark below. Warm matched command ~2.1 s end-to-end, inside the <3 s target; cold Vercel start and long utterances exceed it. The "before locking model size" framing was already moot: the model was locked 2026-07-15 on routing accuracy.)*
- [x] **Validate Cantonese accuracy early.** *(Done 2026-07-15: capture mode + `ops/asr_bench.py` benchmark on 12 real phone clips; switched production to the `JackyHoCL/whisper-small-cantonese-yue-english-ct2` fine-tune — 100% vs 58% command routing, 3× faster. faster-whisper decodes iOS `audio/mp4` directly, no conversion needed. SenseVoice not needed unless the fine-tune regresses in daily use.)*
