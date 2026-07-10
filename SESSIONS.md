# Session log

## 2026-07-11
- Filled CLAUDE.md section 10 with the voice-OS architecture blueprint; added memory/session-log paths.
- Design review: added prioritized hardening checklist to CLAUDE.md (tunnel auth bypass, command-router injection, upload limits, reliability).
- Built the Vercel gateway in `gateway/`: `api/voice.ts` with timing-safe bearer auth, content-type + 4 MB size validation, Cloudflare Access service-token headers on the upstream call, response normalization, 55 s upstream timeout. Typechecked and smoke-tested (8/8 pass) against a stub ASR server.
- Next: create Vercel project + set env vars (`VOICE_GATEWAY_KEY`, `ASR_URL`, CF Access pair), configure Cloudflare Access on the tunnel hostname, then build the local ASR service.
