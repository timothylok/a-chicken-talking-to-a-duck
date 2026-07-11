import { createHash, timingSafeEqual } from "node:crypto";

// Vercel's own body limit is ~4.5 MB; reject earlier with a clear error.
const MAX_BODY_BYTES = 4 * 1024 * 1024;
const UPSTREAM_TIMEOUT_MS = 55_000;

function keyMatches(header: string | null): boolean {
  const expected = process.env.VOICE_GATEWAY_KEY;
  if (!expected || !header?.startsWith("Bearer ")) return false;
  // Hash both sides to equal length so timingSafeEqual never throws.
  const a = createHash("sha256").update(header.slice(7)).digest();
  const b = createHash("sha256").update(expected).digest();
  return timingSafeEqual(a, b);
}

export async function POST(request: Request): Promise<Response> {
  if (!keyMatches(request.headers.get("authorization"))) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }

  const asrUrl = process.env.ASR_URL;
  if (!asrUrl) {
    return Response.json({ error: "gateway misconfigured: ASR_URL not set" }, { status: 500 });
  }
  const isCommand = new URL(request.url).searchParams.get("mode") === "command";
  const targetUrl = isCommand ? asrUrl.replace(/\/inference$/, "/command") : asrUrl;

  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.startsWith("multipart/form-data") && !contentType.startsWith("audio/")) {
    return Response.json(
      { error: "expected multipart/form-data or audio/* body" },
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
