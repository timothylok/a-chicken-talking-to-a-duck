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

### Stack

- **iPhone client** — captures Cantonese audio and sends it over HTTPS. Options: native iOS app (AVAudioRecorder) or web app in Safari using MediaRecorder (`audio/webm` or `audio/wav`).
- **Vercel API gateway** — Node/TypeScript, stateless secure proxy.
- **Cloudflare Tunnel** — `cloudflared` on Win11, exposes the local ASR service without opening the machine to the public internet.
- **Local ASR service** — Faster-Whisper or whisper.cpp on Win11, Cantonese-capable.
- **Command router + agents** — on the same Win11 box (or nearby server).

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

Cantonese settings:

- `language: "yue"` or `zh` with auto-detect
- Model: `medium` or `large` for better Cantonese accuracy

This is the private speech-to-text engine.

#### 5. Command router + agents

- **Command router** — input: transcription text. Maps phrases → command IDs (e.g. `RUN_DEMARK_SCAN`, `UPDATE_NOTION`). Simple pattern matching + optional LLM router.
- **Agents** — each command triggers: Notion updates, Vercel deploy hooks, MCP pipelines, quant analysis jobs, or any other defined automation.

This layer is the automation brain.

### Environment variables

- `VOICE_GATEWAY_KEY` — stored in Vercel env; shared secret required in the `Authorization` header to hit `/api/voice`

### Security model

- **Gateway key** — only clients with `VOICE_GATEWAY_KEY` can hit `/api/voice`.
- **Tunnel** — only Cloudflare → the ASR service; no direct public access to the Win11 machine.
- **Local ASR + agents** — never exposed directly; only reachable via the tunnel.

### Hardening checklist

Findings from the 2026-07-11 design review, in priority order. Check items off as they are implemented.

#### Critical — do before anything goes live

- [x] **Lock the tunnel with Cloudflare Access service tokens.** *(Done 2026-07-11: app `voice-asr`, service token `voice-gateway`.)* The tunnel hostname is public and bypasses the Vercel gateway key entirely. Gateway sends `CF-Access-Client-Id`/`CF-Access-Client-Secret` headers; Cloudflare rejects all other traffic at the edge so the Win11 box never sees unauthenticated requests.
- [x] **Command allowlist, not fuzzy matching.** *(Done 2026-07-11: `asr/router.py`, exact normalized-phrase match only.)* Destructive commands (deploys, writes) match only exact allowlisted phrases. If an LLM router is added, it may only output a command ID from a fixed enum — never free-form actions (prompt-injection guard).
- [x] **Confirmation step for destructive commands.** *(Done 2026-07-11: destructive commands return `needs_confirmation`; 確認/confirm within 60 s executes, 取消/cancel clears.)* Echo the transcription back and require confirm before executing (also guards against Cantonese misrecognition).
- [ ] **Upload limits at the gateway.** Max file size, max audio duration, content-type validation, rate limiting. ASR inference is compute-heavy — unlimited uploads = trivial DoS of the Win11 box. Note Vercel's ~4.5 MB body limit: enforce compressed audio (AAC/Opus) client-side.

#### High — do before daily use

- [ ] **Key hygiene.** Timing-safe comparison of `VOICE_GATEWAY_KEY`; request timestamps to bound replay; documented rotation procedure. Never share the iOS Shortcut containing the key via iCloud.
- [ ] **Isolate ASR from agent credentials.** ASR service runs under a low-privilege account/container; agent credentials (Notion, Vercel hooks, quant jobs) live in a separate process the ASR service cannot read.
- [ ] **File handling safety.** Never use client-supplied filenames in paths or shell commands (path traversal / command injection via ffmpeg); validate audio before decoding.
- [ ] **Keep secrets and audio out of logs.** Authorization headers and audio bodies must not appear in Vercel, Cloudflare, or local logs; decide deliberately where transcripts are stored and for how long.

#### Reliability / usability

- [x] **Run cloudflared and the ASR server as auto-restarting Windows services**; disable sleep/hibernate on the Win11 box. *(Done 2026-07-11: services `Cloudflared` and `VoiceASR` (NSSM), both auto-start; AC sleep/hibernate disabled.)*
- [x] **Health check + external uptime ping** so silent failure (sleep, Windows Update reboot, dead tunnel) gets noticed. *(Done 2026-07-12: `ops/heartbeat.ps1` via "VoiceOS Heartbeat" scheduled task every 10 min → healthchecks.io, 30-min period; /fail ping with reason on detected failure.)*
- [x] **User feedback channel.** Push or spoken confirmation of success/failure — never silent execution. *(Done 2026-07-12: shortcut speaks the router's `reply`, with an error branch for failures.)*
- [ ] **Idempotency keys at the gateway** so double-taps/retries don't run a command twice.
- [ ] **Benchmark latency on real hardware before locking model size.** Target <3 s end-to-end; `large` on CPU is unusable. Watch the gateway function timeout on long transcriptions.
- [ ] **Validate Cantonese accuracy early.** Whisper `yue` is weak and Canto-English code-switching degrades it; evaluate SenseVoice against real command phrases. iOS Safari records `audio/mp4` (not webm) — plan server-side conversion to 16 kHz WAV.

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
