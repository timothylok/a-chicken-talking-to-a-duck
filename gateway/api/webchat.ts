// Public, unauthenticated entry point for the web chat widget
// (gateway/public/chat.html). Unlike voice.ts/slack.ts there is no shared
// secret a visitor can present, so IP-based rate limiting is the only
// defense — same best-effort, per-instance caveat those limiters already
// carry (no shared store on Vercel functions).
//
// Deliberately does NOT proxy through /api/voice?mode=command the way
// slack.ts does: that would share voice.ts's single global rate-limit
// counter across channels, so a webchat burst could false-limit legitimate
// Slack/phone traffic landing on the same warm instance (and vice versa).
// This route talks to the ASR service directly instead, same as voice.ts.

const MAX_BODY_BYTES = 2 * 1024; // chat text, not audio
const COMMAND_TIMEOUT_MS = 240_000; // commands can shell out to CLIs

const RATE_LIMIT_MAX = 10;
const RATE_LIMIT_WINDOW_MS = 60_000;
const hitsByIp = new Map<string, number[]>();

function rateLimited(ip: string): boolean {
  const now = Date.now();
  // Sweep every call (mirrors voice.ts's duplicateBody pattern) so IPs with
  // no recent hits don't accumulate forever on a long-lived instance.
  for (const [key, hits] of hitsByIp) {
    const fresh = hits.filter((ts) => now - ts <= RATE_LIMIT_WINDOW_MS);
    if (fresh.length === 0) hitsByIp.delete(key);
    else hitsByIp.set(key, fresh);
  }
  const hits = hitsByIp.get(ip) ?? [];
  if (hits.length >= RATE_LIMIT_MAX) return true;
  hits.push(now);
  hitsByIp.set(ip, hits);
  return false;
}

function clientIp(request: Request): string {
  const forwarded = request.headers.get("x-forwarded-for");
  return forwarded ? forwarded.split(",")[0].trim() : "unknown";
}

export async function POST(request: Request): Promise<Response> {
  if (rateLimited(clientIp(request))) {
    return Response.json({ error: "too many requests, wait a minute" }, { status: 429 });
  }

  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.startsWith("application/json")) {
    return Response.json({ error: "expected a json body" }, { status: 415 });
  }

  const body = await request.arrayBuffer();
  if (body.byteLength === 0) {
    return Response.json({ error: "empty body" }, { status: 400 });
  }
  if (body.byteLength > MAX_BODY_BYTES) {
    return Response.json({ error: `body exceeds ${MAX_BODY_BYTES} bytes` }, { status: 413 });
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder().decode(body));
  } catch {
    return Response.json({ error: "invalid json body" }, { status: 400 });
  }
  const record = typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : {};
  const text = record.text;
  const lang = record.lang;
  if (typeof text !== "string" || !text.trim()) {
    return Response.json({ error: "json body must include a non-empty 'text'" }, { status: 400 });
  }
  if (lang !== "yue" && lang !== "en") {
    return Response.json({ error: "json body must include lang: 'yue' or 'en'" }, { status: 400 });
  }

  const asrUrl = process.env.ASR_URL;
  if (!asrUrl) {
    return Response.json({ error: "gateway misconfigured: ASR_URL not set" }, { status: 500 });
  }
  const targetUrl = asrUrl.replace(/\/inference$/, "/command");

  const headers: Record<string, string> = { "content-type": "application/json" };
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
      body: JSON.stringify({ text: text.trim(), source: "web", lang }),
      signal: AbortSignal.timeout(COMMAND_TIMEOUT_MS),
    });
  } catch {
    return Response.json({ error: "asr service unreachable" }, { status: 502 });
  }

  if (!upstream.ok) {
    const detail = await upstream
      .json()
      .then((b) => b?.detail)
      .catch(() => undefined);
    return Response.json(
      { error: typeof detail === "string" ? detail : "asr service error", upstreamStatus: upstream.status },
      { status: 502 },
    );
  }

  const result = await upstream.json().catch(() => null);
  if (!result || typeof result.reply !== "string") {
    return Response.json({ error: "invalid asr response" }, { status: 502 });
  }

  // Public surface: pass through only what the chat UI needs, never `data`.
  return Response.json({
    reply: result.reply,
    command: typeof result.command === "string" ? result.command : null,
    status: typeof result.status === "string" ? result.status : "unknown",
  });
}
