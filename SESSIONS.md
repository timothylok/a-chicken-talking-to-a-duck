# Session log

## 2026-07-11
- Filled CLAUDE.md section 10 with the voice-OS architecture blueprint; added memory/session-log paths.
- Design review: added prioritized hardening checklist to CLAUDE.md (tunnel auth bypass, command-router injection, upload limits, reliability).
- Built the Vercel gateway in `gateway/`: `api/voice.ts` with timing-safe bearer auth, content-type + 4 MB size validation, Cloudflare Access service-token headers on the upstream call, response normalization, 55 s upstream timeout. Typechecked and smoke-tested (8/8 pass) against a stub ASR server.
- Gateway deployed to https://a-chicken-talking-to-a-duck.vercel.app (repo: https://github.com/timothylok/a-chicken-talking-to-a-duck, root dir `gateway/`, preset "Other").
- Built the local ASR service in `asr/`: FastAPI + faster-whisper on 127.0.0.1:9000, `POST /inference` (multipart or raw audio), `/health`, in-memory only (no disk writes, no client filenames), 16 MB / 120 s caps, single-inference lock. GPU (GTX 1650) works via nvidia-cublas-cu12/nvidia-cudnn-cu12 pip wheels; startup warmup forces cuBLAS load so CPU fallback actually triggers. Verified end-to-end with Windows TTS speech on CUDA.
- Model note: 4 GB VRAM fits `medium` (default) but not `large-v3` float16; `yue` language token requires large-v3, so non-large-v3 models auto-downgrade to `zh`. Benchmark before locking model choice.
- ASR service committed and pushed (cbebb68).
- Cloudflare Tunnel set up: cloudflared 2026.7.1 installed via winget, tunnel `voice-asr` (77c3012c-98ad-4e5d-83c0-80e3423fcc40), DNS voice.fittertrack.com → localhost:9000, config at C:\Users\timlo\.cloudflared\config.yml. Domain is fittertrack.com (blueprint's tt-tunnel.com was a placeholder). Verified end-to-end: /health and /inference through the public URL, ~1.9 s for 2 s of audio on medium/CUDA.
- Vercel env vars set (VOICE_GATEWAY_KEY, ASR_URL); local copy in gateway/.env (gitignored). Fixed gateway crash on Vercel: web-handler export needs "type":"module" in package.json (cd10845).
- FULL CHAIN VERIFIED: POST /api/voice with bearer key → Vercel → tunnel → Win11 GPU ASR → transcription, 3.6 s for 2 s audio; 401 without key.
- Cloudflare Access set up via API (Zero Trust org onboarded first): app `voice-asr` on voice.fittertrack.com, Service Auth policy, service token `voice-gateway` (1 yr). Anonymous → 401 at edge; CF_ACCESS_* creds in gateway/.env and Vercel env. Full chain retested with Access enforced: 200 in 3.9 s. Checklist item 1 closed.
- User advised to roll the Cloudflare API token pasted in chat.
- Windows services installed (checklist item ticked): `Cloudflared` (explicit --config/--logfile in binPath; the systemprofile config-discovery route crash-looped) and `VoiceASR` (NSSM). Two porting bugs fixed: (1) Store Python unusable by services (MSIX container) → installed python.org 3.12 machine-wide, rebuilt venv; (2) ctranslate2 finds cuBLAS via PATH search, not add_dll_directory → server.py now prepends nvidia bin dirs to PATH. AC sleep/hibernate disabled. Verified: both services auto-start + running, health on CUDA, full chain 200 in 4.6 s. server.py PATH fix NOT yet committed.
- Next: build the command router, iPhone client (iOS Shortcut), external uptime ping.
