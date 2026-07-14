# 🐔🦆 A Chicken Talking to a Duck

A private, Cantonese-capable voice OS: speak Cantonese into an iPhone, and a home Windows PC transcribes it, routes it through a command allowlist, and speaks the answer back — weather, buses, tides, fuel, bins, news, a morning briefing, and free-form chat, all without sending a word to a cloud AI service.

> The name comes from the Cantonese idiom 雞同鴨講 ("a chicken talking to a duck") — two parties talking past each other. Fitting for the years voice assistants spent not understanding Cantonese.

## Inspiration

Mainstream voice assistants treat Cantonese as an afterthought — they either don't support it, force Mandarin, or mangle the 口語 (spoken register) that real Cantonese speakers actually use. And the ones that do listen send everything to someone else's cloud.

We wanted a voice assistant that speaks Hong Kong-style Cantonese back, knows about life in Auckland, New Zealand (not a generic "your local weather"), and keeps the speech, the transcripts, and the AI entirely on hardware we own.

## What it does

Say something in Cantonese to an iOS Shortcut and it speaks the answer back:

- **今日天氣** — today's Auckland weather
- **巴士** — live departures from the local bus stops
- **油價** — cheapest 95 petrol nearby
- **潮汐** — next tides, spoken with natural Cantonese time phrasing (朝早7點05分)
- **幾時收垃圾** — Auckland Council rubbish / recycling / food-scraps days for our address
- **新聞** — top NZ headlines, translated into spoken Cantonese on the fly by a local LLM
- **早晨** — a composed morning briefing: weather + buses + bin reminder + news
- **系統狀態 / 重啟語音系統 / 重新部署** — system health, voice-triggered service restart, and a deploy hook (destructive commands demand a spoken 確認 within 60 seconds)

Anything that isn't a command falls through to a local LLM that replies in genuine Hong Kong 口語 — and by design its output can *never* trigger a command.

## How we built it

```
[iPhone mic] → HTTPS → [Vercel gateway] → [Cloudflare Tunnel] → [Win11 GPU ASR] → [Command router / local LLM] → spoken reply
```

- **iPhone client** — a plain iOS Shortcut: record audio, POST it, speak the reply (errors are spoken too, never silent).
- **Gateway** — a stateless Vercel function (`gateway/api/voice.ts`): timing-safe bearer auth, content-type validation, a 4 MB body cap, and per-instance rate limiting. It forwards audio to the tunnel with Cloudflare Access service-token headers.
- **Tunnel** — Cloudflare Tunnel to the home PC, locked with Cloudflare Access so only the gateway can get through; the Windows box is never directly exposed.
- **ASR** — FastAPI + faster-whisper on a 4 GB GTX 1650, running as a Windows service. A Whisper `initial_prompt` built from the command vocabulary biases decoding toward the exact allowlisted phrases. Started on stock `medium`; now a Cantonese+English fine-tune of `small`, chosen by benchmarking candidates on real captured phone recordings.
- **Command router** — an exact normalized-phrase allowlist (`asr/router.py`). Adding a command is one dict entry; the recognition prompt rebuilds itself from it. NZ data commands shell out to vetted open-source NZ data connectors (Auckland Transport, LINZ tides, Gaspy fuel, Auckland Council bins, NZ news RSS).
- **Local LLM** — Ollama running `gemma3:4b`, chosen after a bake-off for producing the most natural spoken Cantonese; it powers both the chat fallback and live headline translation, warmed at service startup.
- **Ops** — everything runs as auto-restarting Windows services, with a scheduled heartbeat pinging healthchecks.io every 10 minutes so silent failure gets noticed.

## Challenges we ran into

- **Whisper's `yue` token only exists in `large-v3`**, which doesn't fit in 4 GB of VRAM. Counter-intuitively, `language=zh` on `medium` transcribes spoken Cantonese well (it produces real 口語 like 講嘢) — accuracy came from the command-vocabulary prompt, not model size.
- **Two-syllable commands are an ASR worst case**: a 1–2 second clip gives Whisper almost no context, so 油價 became 有加, 巴士 became 巴西 or even 發洩, and 潮汐 came back as 超值, 朝夕 or 確認. The strategy that worked was a two-stage loop, not a setting. Stage one: mine every real misheard form out of the logs and add it to the phrase allowlist — harmless under exact matching, and it re-biases the recognition prompt toward the true phrase. Stage two, when mishearings kept coming: an opt-in capture mode saves real phone recordings, and a benchmark harness (`ops/asr_bench.py`) replays them against candidate models measuring the metric that actually matters — did it route to the right command? A community Cantonese+English fine-tune of Whisper `small` scored 100% command routing where stock `medium` managed 58%, with a third of the latency — and swapping it into production was a single env var, because the model was never hard-coded.
- **Windows services are hostile territory for Python**: Microsoft Store Python is unusable inside a service (MSIX container), ctranslate2 finds cuBLAS via `PATH` rather than `add_dll_directory`, and NSSM's stderr defaults to cp1252 — which turns Cantonese logs into escape soup and killed subprocesses with `UnicodeEncodeError` on macrons like *Whangārei*.
- **Fuzzy geocoding across the Tasman**: the fuel API happily resolved bare "glenfield" to Sydney, and the bins API matched our suburb to a street in Papakura. Every location value now has to be disambiguated.
- **iOS TTS quirks**: reading English words embedded in a Chinese sentence, iOS runs them together — we inject Chinese commas between adjacent English words to force pauses. English headlines mid-Cantonese were unlistenable, which is why headlines are LLM-translated.
- **LLM discipline**: qwen2.5 mixed English and emoji into "Cantonese" replies; batch headline translation produced unparseable preambles. One call per headline, strict system prompt, and a hard rule that LLM output is reply-only.

## Accomplishments that we're proud of

- **It's real and it's daily-driven**: full chain live since 2026-07-12, spoken Cantonese in → spoken Cantonese out in a few seconds, on a consumer GPU.
- **Security was designed in, not bolted on**: edge-enforced Cloudflare Access, timing-safe key comparison, an exact-match command allowlist (no fuzzy matching an LLM could be prompt-injected through), spoken confirmation for destructive commands, and rate limiting at the gateway.
- **A one-dict-entry command system**: new commands automatically improve speech recognition (via the rebuilt prompt), appear in the spoken help, and regenerate the public home page through a pre-commit hook.
- **Genuinely local, genuinely Cantonese**: transcription and chat never leave the house, and the assistant answers in the register people actually speak.

## What we learned

- Model size isn't accuracy: a domain vocabulary prompt on a small Whisper model beat chasing a bigger one, and validating against *real misheard transcriptions from the logs* is the highest-leverage tuning loop.
- Treat an LLM in the loop as untrusted input. Reply-only fallback plus an exact allowlist means the worst a prompt injection can do is say something weird out loud.
- Voice UX lives and dies on feedback: every failure path must *say something*. Silent errors in a screen-free interface are indistinguishable from a dead system.
- The unglamorous ops layer — Windows services, encoding, heartbeats, auto-restart — took as much care as the AI, and is the reason the system survives reboots and Windows Updates unattended.
