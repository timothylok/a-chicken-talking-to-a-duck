import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { waitUntil } from "@vercel/functions";

// Slack retries an event unless it gets a 200 within 3 s, but commands can
// take ~20 s (morning briefing) — so the handler acks immediately and the
// command runs post-response via waitUntil.
const COMMAND_TIMEOUT_MS = 58_000;

// Same replay bound as /api/voice; Slack sends its timestamp in unix seconds.
const REPLAY_WINDOW_MS = 5 * 60_000;

function signatureValid(request: Request, rawBody: string): boolean {
  const secret = process.env.SLACK_SIGNING_SECRET;
  const ts = request.headers.get("x-slack-request-timestamp");
  const sig = request.headers.get("x-slack-signature");
  if (!secret || !ts || !sig) return false;
  if (Math.abs(Date.now() - Number(ts) * 1000) > REPLAY_WINDOW_MS) return false;
  const expected =
    "v0=" + createHmac("sha256", secret).update(`v0:${ts}:${rawBody}`).digest("hex");
  // Hash both sides to equal length so timingSafeEqual never throws.
  const a = createHash("sha256").update(expected).digest();
  const b = createHash("sha256").update(sig).digest();
  return timingSafeEqual(a, b);
}

async function postToSlack(channel: string, text: string): Promise<void> {
  const token = process.env.SLACK_BOT_TOKEN;
  if (!token) {
    console.error("SLACK_BOT_TOKEN not set; dropping reply");
    return;
  }
  try {
    const resp = await fetch("https://slack.com/api/chat.postMessage", {
      method: "POST",
      headers: {
        "content-type": "application/json; charset=utf-8",
        authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ channel, text }),
    });
    const result = await resp.json().catch(() => null);
    if (!result?.ok) console.error("chat.postMessage failed:", result?.error);
  } catch (err) {
    console.error("chat.postMessage unreachable:", err);
  }
}

// Forward through our own /api/voice text path so Slack traffic gets the same
// auth, rate limit, idempotency, and Cloudflare Access handling as the phone.
async function runCommand(
  text: string,
  channel: string,
  origin: string,
  eventTs: string,
): Promise<void> {
  let reply: string;
  try {
    const resp = await fetch(`${origin}/api/voice?mode=command`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${process.env.VOICE_GATEWAY_KEY}`,
        "x-timestamp": new Date().toISOString(),
        "content-type": "application/json",
      },
      // slack_event_ts (ignored by the ASR server) makes re-asking the same
      // phrase a distinct body — otherwise the gateway's byte-dedupe 409s a
      // repeat within 60 s. A true Slack retry reuses the event ts, so
      // network-level duplicates still dedupe.
      body: JSON.stringify({ text, source: "slack", slack_event_ts: eventTs }),
      signal: AbortSignal.timeout(COMMAND_TIMEOUT_MS),
    });
    const result = await resp.json().catch(() => null);
    reply =
      typeof result?.reply === "string" && result.reply
        ? result.reply
        : `指令出錯：${typeof result?.error === "string" ? result.error : "無回應"}`;
  } catch {
    reply = "指令出錯：語音系統無回應";
  }
  await postToSlack(channel, reply);
}

export async function POST(request: Request): Promise<Response> {
  const rawBody = await request.text();
  if (!signatureValid(request, rawBody)) {
    return Response.json({ error: "invalid slack signature" }, { status: 401 });
  }

  let body: any;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return Response.json({ error: "invalid json" }, { status: 400 });
  }

  // Slack app setup handshake.
  if (body.type === "url_verification") {
    return Response.json({ challenge: body.challenge });
  }

  // A retry means our ack was late, not that delivery failed — the original
  // event is already being processed; running it again would double-execute.
  if (request.headers.get("x-slack-retry-num")) {
    return new Response("ok");
  }

  const event = body.event;
  // Mentions only; bot_id guards against replying to our own (or any bot's)
  // messages.
  if (body.type !== "event_callback" || event?.type !== "app_mention" || event.bot_id) {
    return new Response("ignored");
  }

  const channel = String(event.channel ?? "");
  const text = String(event.text ?? "").replace(/<@[^>]+>/g, " ").trim();
  if (!channel) {
    return new Response("ignored");
  }
  if (!text) {
    waitUntil(postToSlack(channel, "講個指令俾我，例如：系統狀態、今日天氣、早晨"));
    return new Response("ok");
  }

  waitUntil(
    runCommand(text, channel, new URL(request.url).origin, String(event.event_ts ?? Date.now())),
  );
  return new Response("ok");
}
