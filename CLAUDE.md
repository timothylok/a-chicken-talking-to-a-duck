# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Copy this file into any new project and fill in the project-specific sections marked with `[FILL IN]`.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

---

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.
- For exploratory questions ("what could we do about X?"), respond in 2–3 sentences with a recommendation and the main tradeoff. Don't implement until the user agrees.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan before starting:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Safety & Security

**Never introduce vulnerabilities. Never take irreversible actions silently.**

Code safety:
- Never introduce SQL injection, command injection, XSS, path traversal, or hardcoded secrets.
- Validate only at system boundaries (user input, external APIs). Trust internal code.
- Never commit `.env`, credentials, or API keys. Warn the user if they try to.

Destructive action guard — pause and confirm before any action that is:
- Hard to reverse: `git reset --hard`, force-push, dropping tables, deleting files.
- Visible to others: pushing code, opening/closing PRs, sending messages.
- Affecting shared state: CI/CD changes, infrastructure modifications, shared config.

One user approval does not authorize the same action in all future contexts. Confirm each time unless the user has explicitly pre-authorized it in this file.

## 6. Dependency & File Discipline

**Don't add weight without a reason.**

- Don't add a new package if the standard library or an already-imported dependency covers it.
- When adding a dependency, name it and state why the existing stack doesn't cover it.
- Prefer editing existing files over creating new ones.
- Never create documentation files (`.md`, `README`) or test scaffolding unless explicitly asked.
- Don't create planning or analysis documents — work from conversation context.

## 7. Git Discipline

**Commits are intentional. Branches are sacred.**

- Never commit unless the user explicitly asks.
- Never amend a published commit — create a new one instead.
- Never skip hooks (`--no-verify`) or bypass signing unless the user explicitly instructs it.
- Never force-push to `main`/`master` — warn the user if they request it.
- Commit messages: one line, imperative mood, explain *why* not *what*.

## 8. Response Style

**Terse and precise. No filler.**

- No emoji unless the user asks for them.
- No trailing summaries of what you just did — the user can read the diff.
- One sentence per update while working. Silent is not acceptable; verbose is.
- When referencing code, include `file_path:line_number` so the user can navigate directly.
- End-of-turn: one or two sentences — what changed and what's next. Nothing else.

---

## 9. Project Memory

**Read memory first. Keep it current. Don't let it go stale.**

### Where memory lives

This project's memory file is at:
```
C:\Users\timlo\.claude\projects\D--ai-voice-ecosystem\memory\
```

The memory index is at:
```
C:\Users\timlo\.claude\projects\D--ai-voice-ecosystem\memory\MEMORY.md
```

### When to read memory

When asked to assess, explain, or verify anything about this project — architecture, pipeline behaviour, scheduling, data flow — **read the memory file first**. Only go to source files if memory is silent or ambiguous.

### When to update memory

At the end of any session where significant changes were made, update the memory file when:
- Architecture changes
- New phases or milestones complete
- Pipeline or scheduler changes
- New canonical file locations are established
- Key decisions are made that aren't obvious from the code

### Project-specific session log

At the end of each session, manually append an entry to `D:\ai\voice-ecosystem\SESSIONS.md`:

```
## YYYY-MM-DD
- What was done
- What changed
- What's next
```

No script — this is a manual step. There is no automation that writes entries.

### Source of truth hierarchy

```
Memory file  >  Source code  >  Generated artifacts (HTML, reports, cached output)
```

Never read generated artifacts (HTML, compiled output, cached reports) for project context — they are human-readable outputs, not authoritative state.

---

## 10. Project-Specific Context

### Overview

A private, Cantonese-capable voice OS:

```
iPhone → Vercel API → Cloudflare Tunnel → Win11 ASR → Private agents
```

### High-level architecture

```
[iPhone mic]
   |
   v
HTTPS (audio)
   |
   v
[Vercel API Gateway]
   |
   v
HTTPS → Cloudflare Tunnel
   |
   v
[Local Win11 ASR Service]
   |
   v
Text → Command Router → Agents
```

### Stack (as built — live since 2026-07-12)

- **iPhone client** — iOS Shortcut (recipe: `iphone-shortcut.md`): Record Audio → POST → speak the `reply` (error branch speaks failures).
- **Vercel API gateway** — `gateway/api/voice.ts` (ESM, `"type": "module"` required). `POST /api/voice` returns transcription; `?mode=command` routes to the command router and passes its response through. Command mode also accepts a JSON `{"text": "早晨"}` body (skips ASR) — used by the iPhone morning-briefing automation (`iphone-shortcut.md`).
- **Slack bridge** — `gateway/api/slack.ts`: @mention the bot → Slack-signature-verified event → text forwarded to `/api/voice?mode=command` (same auth/rate-limit/dedupe path as the phone) → router's Cantonese reply posted back to the channel. Mentions only (scopes `app_mentions:read` + `chat:write`; deliberately not `message.channels`); Slack retries (`X-Slack-Retry-Num`) are acked and ignored to avoid double-execution; slow commands run post-ack via `waitUntil` (`@vercel/functions`). Same allowlist router — no LLM action routing.
- **Cloudflare Tunnel** — Windows service `Cloudflared`, tunnel `voice-asr`, hostname voice.fittertrack.com, locked by Cloudflare Access service token (only the gateway can pass).
- **Local ASR service** — Windows service `VoiceASR` (NSSM): FastAPI + faster-whisper on CUDA, `language=zh`, `initial_prompt` built from command vocabulary for phrase accuracy. Model: `JackyHoCL/whisper-small-cantonese-yue-english-ct2` (Cantonese+English fine-tune; beat stock `medium` 100% vs 58% command routing on real captured clips, 2026-07-15 — see `ops/asr_bench.py`). Python 3.12 at `C:\Program Files\Python312` (Store Python unusable by services).
- **Command router** — `asr/router.py`: exact normalized-phrase allowlist; destructive commands need 確認 within 60 s. Unmatched speech goes to the Ollama chat fallback (`gemma3:4b`, replies in spoken Cantonese, reply-only — chat can never trigger commands). One deliberate exception to exact matching: utterances starting with 提我/提醒我/remind me route to `CREATE_REMINDER` — Ollama extracts `{title, due}` as data only (validated in code: schema, date parse, future-bounded), the router returns a `reminder` payload, and the **iPhone Shortcut** creates the iOS Reminder locally (Apple has no server-side Reminders API). That reminder carries no alert (iOS Shortcuts rejects dynamic alert times), so scheduled task "VoiceOS Reminder Alerts" (`ops/reminder_alerts.py`, every minute, runs as the user — same credential isolation as Notion sync) watches `history.jsonl` and pushes an ntfy notification when each reminder falls due (>24 h overdue = dropped, not alerted late).
- **Monitoring** — scheduled task "VoiceOS Heartbeat" (`ops/heartbeat.ps1`, every 10 min) → healthchecks.io; logs at `asr/logs/service.log` and `logs/cloudflared.log`.
- **Conversation history** — every `/command` interaction (chat included) appends a JSON line to `asr/logs/history.jsonl` (NZ timestamp, transcript, command, Cantonese reply, structured `data`). Command runners may return `(reply, data)` — the dict is stored for later comparisons (e.g. today-vs-yesterday temperature), never spoken. Scheduled task "VoiceOS Notion Sync" (`ops/notion_sync.py`, every 5 min, runs as the user so the Notion key stays out of the VoiceASR service env) mirrors **command entries only** to a Notion database — chat transcripts never leave the machine. Config `ops/notion.json` (gitignored — holds `api_key`, `database_id`); `--setup <parent_page_id>` creates the database. Unconfigured runs are no-ops.
- **Public home page** — https://a-chicken-talking-to-a-duck.vercel.app/ (`gateway/public/index.html`, Cantonese: purpose + command table). Generated by `ops/generate_homepage.py`; the pre-commit hook (`git config core.hooksPath ops/githooks` — rerun on a fresh clone) regenerates it and this file's command list whenever `asr/router.py` is committed. New commands need a description line in the script's `DESCRIPTIONS` dict.
- **NZ data skills** — 102 vetted TheColab connectors (see memory: thecolab-nz-skills) power FUEL_PRICES, BUS_TIMES, TIDE_TIMES, BIN_DAY via subprocess.

### Components

#### 1. iPhone client (Cantonese audio → HTTPS)

- `POST https://a-chicken-talking-to-a-duck.vercel.app/api/voice`
- Headers: `Authorization: Bearer <YOUR_VOICE_KEY>`
- Body: `multipart/form-data` with `file` (audio)

#### 2. Vercel API gateway

Route: `/api/voice`. Responsibilities:

- **Auth** — read `VOICE_GATEWAY_KEY` from env, compare with `Authorization` header.
- **Forward audio** — stream file to the Cloudflare Tunnel URL, e.g. `https://voice.fittertrack.com/inference`.
- **Normalize response** — return JSON:

```json
{
  "text": "（Cantonese transcription）",
  "language": "yue",
  "segments": [...]
}
```

This route is stateless and purely a secure proxy.

#### 3. Cloudflare Tunnel → local Win11

Setup on Win11:

1. Install `cloudflared`
2. Create tunnel
3. Map hostname (e.g. `voice.fittertrack.com`) → `http://localhost:9000`

Config example (`config.yml`):

```yaml
tunnel: 77c3012c-98ad-4e5d-83c0-80e3423fcc40
credentials-file: C:\Users\timlo\.cloudflared\77c3012c-98ad-4e5d-83c0-80e3423fcc40.json

ingress:
  - hostname: voice.fittertrack.com
    service: http://localhost:9000
  - service: http_status:404
```

Run tunnel:

```powershell
cloudflared tunnel run voice-asr
```

Any request to `https://voice.fittertrack.com` is forwarded to the local ASR service.

#### 4. Local ASR service (Win11, Cantonese)

Use Faster-Whisper or whisper.cpp:

- HTTP server on `localhost:9000`
- Endpoint: `POST /inference` with audio
- Response: transcription JSON (Cantonese supported)

Cantonese settings (validated in production):

- `language=zh` with model `medium` handles spoken Cantonese well (produces 口語 like 講嘢); the `yue` token requires `large-v3`, which doesn't fit the 4 GB GPU — server auto-downgrades `yue`→`zh` for other models
- Accuracy comes from the command-vocabulary `initial_prompt`, not model size

This is the private speech-to-text engine.

#### 5. Command router + agents

- **Command router** — `asr/router.py`, `POST /command` on the ASR service. Adding a command = one entry in `COMMANDS`: phrases (include traditional + simplified + English variants; mine `asr/logs/service.log` for real misheard forms), `destructive` flag, `run` callable. The Whisper `initial_prompt` rebuilds from `COMMANDS` at startup, so new phrases automatically improve recognition.
- **Current commands** (auto-synced from `COMMANDS` by the pre-commit hook — do not edit between the markers) — <!-- COMMANDS:BEGIN -->`SYSTEM_STATUS` (系統狀態)、`LIST_COMMANDS` (有咩指令)、`WEATHER_TODAY` (今日天氣)、`WEATHER_COMPARE` (同琴日比)、`FUEL_PRICES` (油價)、`BUS_TIMES` (巴士)、`TIDE_TIMES` (潮汐)、`BIN_DAY` (幾時收垃圾)、`MILK_PRICES` (牛奶價錢)、`MORTGAGE_RATES` (按揭利率)、`EARTHQUAKES` (地震)、`NEWS_HEADLINES` (新聞)、`MORNING_BRIEFING` (早晨)、`QUOTE_OF_DAY` (今日金句)、`MOVIE_QUOTE` (電影金句)、`CREATE_REMINDER` (提我)、`RESTART_ASR` (重啟語音系統)、`TRIGGER_DEPLOY` (重新部署，destructive)<!-- COMMANDS:END -->
- **NZ data commands** run the vetted TheColab skill CLIs (clone at `D:\ai\thecolab-skills`, junctioned into `.claude/skills`, gitignored; update with `git -C D:\ai\thecolab-skills pull`) as subprocesses. Location values must be disambiguated — bare suburb names have fuzzy-matched Sydney (fuel) and Papakura (bins).
- **Replies** pass `_pause_english()` (Chinese comma between adjacent English words for iOS TTS pauses); executed replies are logged to `service.log` (UTF-8 — PowerShell 5.1 needs `Get-Content -Encoding UTF8`).
- **Chat fallback** — unmatched speech → local Ollama (`OLLAMA_MODEL`, default `gemma3:4b` — best spoken Cantonese of the local models), spoken reply returned. Reply-only by design: LLM output is never routed back into `COMMANDS` (prompt-injection guard).
- **Planned agents** — Notion updates, Vercel deploy hooks, MCP pipelines, quant analysis jobs (`RUN_DEMARK_SCAN`).

This layer is the automation brain.

### Environment variables

Gateway (Vercel env; local copy in `gateway/.env`, gitignored):
- `VOICE_GATEWAY_KEY` — shared secret required in the `Authorization` header to hit `/api/voice`
- `ASR_URL` — `https://voice.fittertrack.com/inference`
- `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` — Cloudflare Access service token (`voice-gateway`, expires ~2027-07)
- `SLACK_SIGNING_SECRET` / `SLACK_BOT_TOKEN` — Slack bridge: request-signature verification and `chat.postMessage`

ASR service (AppEnvironmentExtra REG_MULTI_SZ under the service's Parameters key — write it with elevated `Set-ItemProperty`, not `nssm set`, whose quoting mangles values with spaces; currently set: HF_HOME, FUEL_LOCATION, BIN_ADDRESS, ASR_MODEL):
- `HF_HOME` — model cache (`C:\Users\timlo\.cache\huggingface`)
- `FUEL_LOCATION` — fuel search center, `glenfield auckland` (must include "auckland": the API covers AU and bare "glenfield" resolves to Sydney)
- `BIN_ADDRESS` — Auckland Council property ID (numeric — keeps the street address out of git and the spoken reply)
- `BUS_STOPS` / `TIDE_PORT` — optional overrides; default `3881,4010` (Glenfield Mall, both directions) / `auckland`
- `ASR_MODEL` / `ASR_LANGUAGE` / `ASR_PORT` — default `medium` / `yue`(→`zh`) / `9000`
- `OLLAMA_URL` / `OLLAMA_MODEL` — chat fallback, default `http://localhost:11434` / `gemma3:4b`
- `DEPLOY_HOOK_URL` — arms `TRIGGER_DEPLOY` (not yet configured)

### Security model

- **Gateway key** — only clients with `VOICE_GATEWAY_KEY` can hit `/api/voice` (timing-safe compare; requests also need a fresh `X-Timestamp` header, ISO 8601 within ±5 min, to bound replay).
- **Tunnel** — only Cloudflare → the ASR service; no direct public access to the Win11 machine.
- **Local ASR + agents** — never exposed directly; only reachable via the tunnel.

#### Rotating `VOICE_GATEWAY_KEY`

Rotate immediately if the phone is lost or the key may have leaked; otherwise yearly.

1. Generate: `node -e "console.log(crypto.randomBytes(32).toString('base64url'))"`
2. Update `VOICE_GATEWAY_KEY` in the Vercel project env (Production) and in `gateway/.env`.
3. Redeploy the gateway (env changes don't apply until redeploy).
4. Paste the new key into the iPhone Shortcut's `Authorization` header (`Bearer <key>`).
5. Verify: run a voice command; a request with the old key must get 401.

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
- [ ] **Keep secrets and audio out of logs.** *(Mostly done 2026-07-16: benchmark audio capture removed and deleted; transcript retention decided and enforced — chat 30 d, rotated logs 90 d, commands forever, via "VoiceOS Log Prune" daily task + `ops/prune_logs.py`; see SECURITY.md. Remaining: audit Vercel/Cloudflare logs for Authorization headers.)* Authorization headers and audio bodies must not appear in Vercel, Cloudflare, or local logs; decide deliberately where transcripts are stored and for how long.

#### Reliability / usability

- [x] **Run cloudflared and the ASR server as auto-restarting Windows services**; disable sleep/hibernate on the Win11 box. *(Done 2026-07-11: services `Cloudflared` and `VoiceASR` (NSSM), both auto-start; AC sleep/hibernate disabled.)*
- [x] **Health check + external uptime ping** so silent failure (sleep, Windows Update reboot, dead tunnel) gets noticed. *(Done 2026-07-12: `ops/heartbeat.ps1` via "VoiceOS Heartbeat" scheduled task every 10 min → healthchecks.io, 30-min period; /fail ping with reason on detected failure.)*
- [x] **User feedback channel.** Push or spoken confirmation of success/failure — never silent execution. *(Done 2026-07-12: shortcut speaks the router's `reply`, with an error branch for failures.)*
- [x] **Idempotency keys at the gateway** so double-taps/retries don't run a command twice. *(Done 2026-07-15: SHA-256 body dedupe in `gateway/api/voice.ts` — identical bytes within 60 s get 409; per-instance best-effort like the rate limit. Catches network retries; two separate recordings are two commands by design.)*
- [ ] **Benchmark latency on real hardware before locking model size.** Target <3 s end-to-end; `large` on CPU is unusable. Watch the gateway function timeout on long transcriptions.
- [x] **Validate Cantonese accuracy early.** *(Done 2026-07-15: capture mode + `ops/asr_bench.py` benchmark on 12 real phone clips; switched production to the `JackyHoCL/whisper-small-cantonese-yue-english-ct2` fine-tune — 100% vs 58% command routing, 3× faster. faster-whisper decodes iOS `audio/mp4` directly, no conversion needed. SenseVoice not needed unless the fine-tune regresses in daily use.)*

---

## 11. Lessons Learned

Generalized patterns from past mistakes — apply these proactively.

| Lesson | Pattern | How to avoid |
|--------|---------|--------------|
| Memory before files | Inspected source before checking memory; found contradictory state | Always read memory file first for context on established architecture |
| Stop hooks don't write entries | Assumed automation handled session log; log went stale | Manually add log entries before running any regeneration script |
| Confirmation scope | User approved an action once; assumed blanket approval | Re-confirm destructive or shared-state actions each session unless pre-authorized in this file |
| Speculative error handling | Added validation for states that can't occur internally | Only validate at true system boundaries; trust internal invariants |
| Silent interpretation | Picked one of two interpretations and implemented without asking | Surface ambiguity before touching code |

---

**These guidelines are working if:** diffs contain fewer unnecessary changes, rewrites due to overcomplication decrease, and clarifying questions arrive before mistakes rather than after.
