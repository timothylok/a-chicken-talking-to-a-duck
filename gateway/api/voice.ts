import { createHash, timingSafeEqual } from "node:crypto";

// Vercel's own body limit is ~4.5 MB; reject earlier with a clear error.
const MAX_BODY_BYTES = 4 * 1024 * 1024;
const UPSTREAM_TIMEOUT_MS = 55_000;

// Best-effort per-instance rate limit (no shared store on Vercel functions).
// Protects the Win11 box from a leaked key or a looping Shortcut: ASR
// inference is serialized locally, so anything past this is a DoS, not use.
const RATE_LIMIT_MAX = 10;
const RATE_LIMIT_WINDOW_MS = 60_000;
const recentRequests: number[] = [];

function rateLimited(): boolean {
  const now = Date.now();
  while (recentRequests.length > 0 && now - recentRequests[0] > RATE_LIMIT_WINDOW_MS) {
    recentRequests.shift();
  }
  if (recentRequests.length >= RATE_LIMIT_MAX) return true;
  recentRequests.push(now);
  return false;
}

function keyMatches(header: string | null): boolean {
  const expected = process.env.VOICE_GATEWAY_KEY;
  if (!expected || !header?.startsWith("Bearer ")) return false;
  // Hash both sides to equal length so timingSafeEqual never throws.
  const a = createHash("sha256").update(header.slice(7)).digest();
  const b = createHash("sha256").update(expected).digest();
  return timingSafeEqual(a, b);
}

// Replay bound: the Shortcut sends X-Timestamp (ISO 8601); a captured request
// stops working once it is older than this window.
const REPLAY_WINDOW_MS = 5 * 60_000;

function timestampFresh(header: string | null): boolean {
  if (!header) return false;
  const ts = Date.parse(header);
  if (Number.isNaN(ts)) return false;
  return Math.abs(Date.now() - ts) <= REPLAY_WINDOW_MS;
}

// Idempotency: identical body bytes within the window = a network-level
// retry or double-send, never a new recording (each recording differs).
// Per-instance like the rate limit — best effort, no shared store.
const IDEMPOTENCY_WINDOW_MS = 60_000;
const seenBodies = new Map<string, number>();

function duplicateBody(body: ArrayBuffer): boolean {
  const now = Date.now();
  for (const [hash, ts] of seenBodies) {
    if (now - ts > IDEMPOTENCY_WINDOW_MS) seenBodies.delete(hash);
  }
  const hash = createHash("sha256").update(new Uint8Array(body)).digest("hex");
  if (seenBodies.has(hash)) return true;
  seenBodies.set(hash, now);
  return false;
}

export async function POST(request: Request): Promise<Response> {
  if (!keyMatches(request.headers.get("authorization"))) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }

  if (!timestampFresh(request.headers.get("x-timestamp"))) {
    return Response.json(
      { error: "missing or stale X-Timestamp header (ISO 8601, within 5 minutes)" },
      { status: 401 },
    );
  }

  // After auth so unauthenticated noise can't lock out the real user.
  if (rateLimited()) {
    return Response.json({ error: "too many requests, wait a minute" }, { status: 429 });
  }

  const asrUrl = process.env.ASR_URL;
  if (!asrUrl) {
    return Response.json({ error: "gateway misconfigured: ASR_URL not set" }, { status: 500 });
  }
  const isCommand = new URL(request.url).searchParams.get("mode") === "command";
  const targetUrl = isCommand ? asrUrl.replace(/\/inference$/, "/command") : asrUrl;

  const contentType = request.headers.get("content-type") ?? "";
  // Text path (Shortcut automations): JSON {"text": "早晨"} skips ASR on the
  // command endpoint; the transcription endpoint is audio-only.
  const isText = contentType.startsWith("application/json");
  if (isText && !isCommand) {
    return Response.json(
      { error: "json text body requires ?mode=command" },
      { status: 415 },
    );
  }
  if (!isText && !contentType.startsWith("multipart/form-data") && !contentType.startsWith("audio/")) {
    return Response.json(
      { error: "expected multipart/form-data, audio/*, or json text body" },
      { status: 415 },
    );
  }

  const body = await request.arrayBuffer();
  if (body.byteLength === 0) {
    return Response.json({ error: "empty body" }, { status: 400 });
  }
  if (body.byteLength > MAX_BODY_BYTES) {
    return Response.json(
      { error: `body exceeds ${MAX_BODY_BYTES} bytes; send compressed audio (AAC/Opus)` },
      { status: 413 },
    );
  }
  if (duplicateBody(body)) {
    return Response.json({ error: "duplicate request ignored" }, { status: 409 });
  }

  const headers: Record<string, string> = { "content-type": contentType };
  const cfId = process.env.CF_ACCESS_CLIENT_ID;
  const cfSecret = process.env.CF_ACCESS_CLIENT_SECRET;
  if (cfId && cfSecret) {
    headers["CF-Access-Client-Id"] = cfId;
    headers["CF-Access-Client-Secret"] = cfSecret;
  }

  let upstream: Response;
  try {
    upstream = await fetch(targetUrl, {
      method: "POST",
      headers,
      body,
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch {
    return Response.json({ error: "asr service unreachable" }, { status: 502 });
  }

  if (!upstream.ok) {
    const detail = await upstream
      .json()
      .then((body) => body?.detail)
      .catch(() => undefined);
    return Response.json(
      {
        error: typeof detail === "string" ? detail : "asr service error",
        upstreamStatus: upstream.status,
      },
      { status: 502 },
    );
  }

  const result = await upstream.json().catch(() => null);
  if (!result || typeof result.text !== "string") {
    return Response.json({ error: "invalid asr response" }, { status: 502 });
  }

  if (isCommand) {
    // Command responses carry router fields (command, status, reply) — pass through.
    return Response.json(result);
  }

  return Response.json({
    text: result.text,
    language: typeof result.language === "string" ? result.language : "yue",
    segments: Array.isArray(result.segments) ? result.segments : [],
  });
}
