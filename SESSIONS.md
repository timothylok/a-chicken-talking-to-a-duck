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
- Next: set Vercel env vars (ASR_URL=https://voice.fittertrack.com/inference, VOICE_GATEWAY_KEY), add Cloudflare Access service token on the hostname (endpoint currently UNAUTHENTICATED), install cloudflared + ASR as Windows services, build the command router. Doc updates not yet committed.
