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
- **Slack bridge** — `gateway/api/slack.ts`: @mention the bot → Slack-signature-verified event → text forwarded to `/api/voice?mode=command` (same auth/rate-limit/dedupe path as the phone) → router's Cantonese reply posted back to the channel. Mentions only (scopes `app_mentions:read` + `chat:write` + `files:write` for GENERATE_IMAGE uploads; deliberately not `message.channels`); Slack retries (`X-Slack-Retry-Num`) are acked and ignored to avoid double-execution; slow commands run post-ack via `waitUntil` (`@vercel/functions`, `maxDuration` 300). Per-channel throttle: max 3 command executions/min, over-limit mentions get an immediate 指令太密 reply; `chat.postMessage` retries once (429 `Retry-After` honoured) — a 2026-07-18 burst test showed replies dropping silently when briefings queued behind Ollama past the old 60 s function lifetime. Same allowlist router — no LLM action routing. `GENERATE_IMAGE` (畫/draw/image prefix, Slack-source only) comes back as base64 in `data` and the bridge uploads the PNG (external upload flow, `files.upload` is sunset); generation is local CPU diffusion (`asr/image_gen.py`, `Lykon/dreamshaper-8-lcm` at 4 steps, `guidance_scale=1.0`, **`LCMScheduler` set explicitly** — the repo loads as a plain SD pipeline on PNDM, which turns LCM weights into etched noise; NSFW safety checker deliberately off, with an all-black-frame guard so a failed generation errors instead of posting a black square) so the box never holds a Slack token.
- **Cloudflare Tunnel** — Windows service `Cloudflared`, tunnel `voice-asr`, hostname voice.fittertrack.com, locked by Cloudflare Access service token (only the gateway can pass).
- **Local ASR service** — Windows service `VoiceASR` (NSSM): FastAPI + faster-whisper on CUDA, `language=zh`, `initial_prompt` built from command vocabulary for phrase accuracy. Model: `JackyHoCL/whisper-small-cantonese-yue-english-ct2` (Cantonese+English fine-tune; beat stock `medium` 100% vs 58% command routing on real captured clips, 2026-07-15 — see `ops/asr_bench.py`). Python 3.12 at `C:\Program Files\Python312` (Store Python unusable by services).
- **Command router** — `asr/router.py`: exact normalized-phrase allowlist; destructive commands need 確認 within 60 s. Unmatched speech goes to the Ollama chat fallback (`gemma3:4b`, replies in spoken Cantonese, reply-only — chat can never trigger commands). One deliberate exception to exact matching: utterances starting with 提我/提醒我/remind me route to `CREATE_REMINDER` — Ollama extracts `{title, due}` as data only (validated in code: schema, date parse, future-bounded), the router returns a `reminder` payload, and the **iPhone Shortcut** creates the iOS Reminder locally (Apple has no server-side Reminders API). That reminder carries no alert (iOS Shortcuts rejects dynamic alert times), so scheduled task "VoiceOS Reminder Alerts" (`ops/reminder_alerts.py`, every minute, runs as the user — same credential isolation as Notion sync) watches `history.jsonl` and pushes an ntfy notification when each reminder falls due (>24 h overdue = dropped, not alerted late).
- **Monitoring** — scheduled task "VoiceOS Heartbeat" (`ops/heartbeat.ps1`, every 10 min) → healthchecks.io; logs at `asr/logs/service.log` and `logs/cloudflared.log`.
- **Conversation history** — every `/command` interaction (chat included) appends a JSON line to `asr/logs/history.jsonl` (NZ timestamp, transcript, channel `source` — voice/text/slack, command, Cantonese reply, structured `data`); `service.log` command lines carry the same tag (`command [slack]: ...`). Command runners may return `(reply, data)` — the dict is stored for later comparisons (e.g. today-vs-yesterday temperature), never spoken. Scheduled task "VoiceOS Notion Sync" (`ops/notion_sync.py`, every 5 min, runs as the user so the Notion key stays out of the VoiceASR service env) mirrors **command entries only** to a Notion database — chat transcripts never leave the machine. Config `ops/notion.json` (gitignored — holds `api_key`, `database_id`); `--setup <parent_page_id>` creates the database. Unconfigured runs are no-ops.
- **Calendar agenda** — `SCHEDULE_TODAY` (今日行程) speaks today's Google Calendar events. Scheduled task "VoiceOS Calendar Sync" (`ops/calendar_sync.py`, every 15 min, runs as the user) fetches the calendar's **secret iCal URL** (`GOOGLE_ICAL_URL` in the gitignored repo-root `.env`), expands recurrences for today+tomorrow with `icalendar` + `recurring-ical-events`, and writes `asr/cache/calendar.json` (event titles + times only) — the command only reads that file, so the ASR service never holds the feed URL. iCal, not OAuth: sidesteps the Google-Workspace-license blocker that killed the original `TODAY_AGENDA`. Reply is stale-guarded at >30 min; not in `WEB_COMMANDS` (personal). Register the task with `ops/register_calendar_sync.ps1` (elevated). Fetch failure keeps the last good cache.
- **Stock automation** — six report tiers (Category 1 fundamentals → 6 Mag 7 risk dashboard), the `dashboard/` Next.js frontend, and the `STOCK_ANALYSIS`/`PINE_INDICATOR`/`PINE_STRATEGY` commands. Architecture, model choices and gotchas: skill `stock-reports` (`.claude/skills/stock-reports/SKILL.md`).
- **Public home page** — https://a-chicken-talking-to-a-duck.vercel.app/ (`gateway/public/index.html`, Cantonese: purpose + command table). Generated by `ops/generate_homepage.py`; the pre-commit hook (`git config core.hooksPath ops/githooks` — rerun on a fresh clone) regenerates it and this file's command list whenever `asr/router.py` is committed. New commands need a description line in the script's `DESCRIPTIONS` dict.
- **NZ data skills** — 102 vetted TheColab connectors (see memory: thecolab-nz-skills) power FUEL_PRICES, BUS_TIMES, TIDE_TIMES, BIN_DAY via subprocess.
- **PriceSpy product watch** (`ops/pricewatch.py`, scheduled task "VoiceOS Price Watch", 09:05 NZT — 5 min after Milk Watch) — watches PriceSpy NZ product prices (`PRICEWATCH_PRODUCTS` env var, comma-separated product ids, default `13101596,5848136,5241360,15436948,14576211` = "Nintendo Switch 2" + "Kingston Fury Beast Black DDR4 3200MHz 2x16GB" + "G.Skill Ripjaws V Black DDR4 3600MHz 2x16GB" + "Pokemon Pokopia (Switch 2)" + "The Legend of Zelda: Tears of the Kingdom (Switch 2)") via the thecolab-ai `nz-pricewatch` skill CLI (no login, public product pages only). Tracks the cheapest **not-OutOfStock** offer, not the skill's own `current_lowest_price` — that counts dead listings, and a $299 out-of-stock Kingston offer flickering off the page for a day and back fired a phantom "$470.35 → $299 drop" 2026-09-06. Pushes one ntfy alert only when today's price is at least `PRICEWATCH_MIN_DROP_PCT` (default 1%) below the last day it was checked; otherwise silent — any strictly-lower price used to page, so a $470.35 → $470.01 move was an alert. Also watches Trade Me searches (`PRICEWATCH_TRADEME_SEARCHES`, `"term|min_price"`); a candidate must clear the price floor **and** contain every word of the search term in its title, since the floor alone had it tracking a Pokemon Eevee Switch 1 console for a "Nintendo Switch 2" watch (2026-09-08). Deliberately does **not** use PriceSpy's own embedded price-history data for the day-over-day baseline — records its own daily observation to `asr/logs/pricewatch_state.json` instead, so it can't repeat the exact class of bug found in `ops/milk_watch.py` 2026-07-29 (an upstream history dataset that silently stalled for days, causing the same stale "drop" to be re-reported every morning). Same-day reruns don't overwrite the recorded baseline, so a manual test run can't corrupt tomorrow's comparison. A third source added 2026-09-09 watches plain retailer product URLs (`PRICEWATCH_URLS`, default the same Aberlour 12YO at The Bottle-O Glenfield and Super Liquor) — shops PriceSpy doesn't index, so the page is read directly, but only its schema.org product data: JSON-LD for The Bottle-O, `<meta itemprop>` microdata for Super Liquor. Both carry name/price/availability, and an OutOfStock page is skipped without moving the baseline (same rule as the PriceSpy offers). A page that stops publishing either encoding raises rather than guessing a price off visible text.
- **Workflow rules** — `ops/workflows.json` (IF→THEN, owner-edited, committed — no secrets) evaluated every minute by scheduled task "VoiceOS Workflows" (`ops/workflows.py`, runs as the user). Triggers: `schedule` (once/day at HH:MM with late catch-up) and `history` (new history.jsonl entries, byte cursor). Optional `if` runs a command for its reply/`data` (workflow-sourced `/command` responses keep `data`) and tests `reply_contains`/`data_contains`/`data_gte`/`data_lte` (`{today}`/`{tomorrow}` render as "17 July"). Actions: ntfy / non-destructive allowlisted commands (destructive refused — a workflow must never arm the 確認 window) / webhook. Built 2026-07-17 as the free from-scratch IFTTT alternative; phone-context triggers (location etc.) belong to iOS Shortcuts automations calling the gateway text path instead.
- **Voice macros** — `ops/macros.json` (owner-edited, committed — no secrets, same trust model as `ops/workflows.json` above rather than a dynamic plugin system) loaded once at `asr/router.py` import time by `_load_macros()`: each entry chains several existing non-destructive `COMMANDS` into one new trigger phrase, registered as a synthetic `MACRO_<ID>` entry in `COMMANDS` itself, so it gets the confirm/logging/Whisper-vocab/`LIST_COMMANDS` pipeline for free — a JSON-configurable generalization of how `MORNING_BRIEFING` hand-chains its four sections. Per-macro validation at load time skips (logs, never crashes) a macro whose `commands` reference a destructive command, one of the 5 documentation-only stub IDs (`CREATE_REMINDER`/`GENERATE_IMAGE`/`STOCK_ANALYSIS`/`PINE_INDICATOR`/`PINE_STRATEGY` — their real dispatch is intercepted by regex before the `COMMANDS` loop, so their registered `run` is just a usage-hint string), `RESTART_ASR` (not `destructive`, but its `os._exit(0)` timer assumes "reply first, then exit" is atomic — chaining slower steps after it risks killing the process mid-macro), another macro, or a phrase colliding with an existing command/macro. A macro's `pause_english` flag is inherited (`all(...)`) from its chained commands rather than left at the default, so chaining `EARTHQUAKES`/`NEWS_HEADLINES` doesn't reintroduce the English-place-name-mangling bug those flags exist to prevent. Never added to `WEB_COMMANDS` — voice/Slack/text only. Ships with an empty scaffold (`{"macros": []}`); building the mechanism was scoped ahead of any real macro so it could be verified end-to-end with a throwaway example first.
- **External integrations (OAuth)** — follow the pattern in SECURITY.md § External integrations: reads = user-context sync task → sanitized cache under `asr/cache` → command reads the file; writes = marker-file hand-off + 確認. The ASR service never holds provider tokens. (Google Calendar OAuth was the reference implementation; removed 2026-07-29 — no paid Google Workspace license, so OAuth setup was never completable. No token-based integration is currently live; the Calendar agenda read below uses a secret iCal URL, not OAuth, and follows the same sync-task → sanitized-cache → command-reads-file shape.)

### Components

#### Command router + agents

- **Command router** — `asr/router.py`, `POST /command` on the ASR service. Adding a command = one entry in `COMMANDS`: phrases (include traditional + simplified + English variants; mine `asr/logs/service.log` for real misheard forms), `destructive` flag, `run` callable. The Whisper `initial_prompt` rebuilds from `COMMANDS` at startup, so new phrases automatically improve recognition.
- **Current commands** (auto-synced from `COMMANDS` by the pre-commit hook — do not edit between the markers) — <!-- COMMANDS:BEGIN -->`SYSTEM_STATUS` (系統狀態)、`LIST_COMMANDS` (有咩指令)、`WEATHER_TODAY` (今日天氣)、`WEATHER_COMPARE` (同琴日比)、`FUEL_PRICES` (油價)、`BUS_TIMES` (巴士)、`TIDE_TIMES` (潮汐)、`BIN_DAY` (幾時收垃圾)、`MILK_PRICES` (牛奶價錢)、`MORTGAGE_RATES` (按揭利率)、`EARTHQUAKES` (地震)、`NEWS_HEADLINES` (新聞)、`JACKET_CHECK` (帶唔帶遮)、`MORNING_BRIEFING` (早晨)、`QUOTE_OF_DAY` (今日金句)、`MOVIE_QUOTE` (電影金句)、`SCHEDULE_TODAY` (今日行程)、`CREATE_REMINDER` (提我)、`GENERATE_IMAGE` (畫)、`STOCK_ANALYSIS` (分析股票)、`PINE_INDICATOR` (pine indicator)、`PINE_STRATEGY` (pine strategy)、`RESTART_ASR` (重啟語音系統)、`TRIGGER_DEPLOY` (重新部署，destructive)、`MACRO_LEAVING_HOME` (出門模式)、`MACRO_HOUSE_CHECK` (屋企資訊)、`MACRO_WIND_DOWN` (瞓覺前)<!-- COMMANDS:END -->
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

All 17 findings from the 2026-07-11 design review are closed — the full checklist
with its dated implementation notes lives in `SECURITY.md` § Hardening checklist.

Measured latency (2026-09-09, `SECURITY.md` § Latency benchmark): a warm matched
voice command is ~2.1 s end-to-end (~1.9 s ASR + ~0.2 s router), inside the <3 s
target. The Cloudflare tunnel adds nothing measurable. Cold Vercel starts (~3.5 s)
and 7 s utterances (~3.3 s) exceed it; the chat fallback is ~15 s worst case
because it is local LLM generation, which the <3 s target never covered — it was
17-32 s *and intermittently timing out* until the 2026-09-09 reply cap.

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
