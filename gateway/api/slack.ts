import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { waitUntil } from "@vercel/functions";

// Slack retries an event unless it gets a 200 within 3 s, but commands can
// take ~20 s (morning briefing) or ~60-100 s (GENERATE_IMAGE, CPU diffusion)
// — so the handler acks immediately and the command runs post-response via
// waitUntil, inside the 300 s maxDuration.
const COMMAND_TIMEOUT_MS = 250_000;

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
  // One retry (honoring 429 Retry-After) — a burst of replies can trip
  // Slack's per-channel posting limit, and a dropped reply is silent failure.
  for (let attempt = 0; attempt < 2; attempt++) {
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
      if (result?.ok) return;
      console.error("chat.postMessage failed:", result?.error ?? resp.status);
      if (attempt === 0) {
        const waitS = resp.status === 429 ? Number(resp.headers.get("retry-after")) || 1 : 1;
        await new Promise((r) => setTimeout(r, Math.min(waitS, 10) * 1000));
      }
    } catch (err) {
      console.error("chat.postMessage unreachable:", err);
      if (attempt === 0) await new Promise((r) => setTimeout(r, 1000));
    }
  }
}

// GENERATE_IMAGE returns its PNG as base64 in the command response — the
// local box holds no Slack token by design (credential isolation), so the
// bridge does the upload. Two-step external flow: files.upload is sunset.
// Needs the files:write bot scope.
async function uploadToSlack(channel: string, png: Uint8Array<ArrayBuffer>, title: string): Promise<boolean> {
  const token = process.env.SLACK_BOT_TOKEN;
  if (!token) {
    console.error("SLACK_BOT_TOKEN not set; dropping image");
    return false;
  }
  try {
    const urlResp = await fetch("https://slack.com/api/files.getUploadURLExternal", {
      method: "POST",
      headers: {
        "content-type": "application/x-www-form-urlencoded",
        authorization: `Bearer ${token}`,
      },
      body: new URLSearchParams({ filename: "generated.png", length: String(png.byteLength) }),
    });
    const urlResult = await urlResp.json().catch(() => null);
    if (!urlResult?.ok) {
      console.error("files.getUploadURLExternal failed:", urlResult?.error ?? urlResp.status);
      return false;
    }
    const putResp = await fetch(urlResult.upload_url, { method: "POST", body: png });
    if (!putResp.ok) {
      console.error("image bytes upload failed:", putResp.status);
      return false;
    }
    const doneResp = await fetch("https://slack.com/api/files.completeUploadExternal", {
      method: "POST",
      headers: {
        "content-type": "application/json; charset=utf-8",
        authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ files: [{ id: urlResult.file_id, title }], channel_id: channel }),
    });
    const doneResult = await doneResp.json().catch(() => null);
    if (!doneResult?.ok) {
      console.error("files.completeUploadExternal failed:", doneResult?.error ?? doneResp.status);
      return false;
    }
    return true;
  } catch (err) {
    console.error("image upload unreachable:", err);
    return false;
  }
}

// Slow commands serialize behind Ollama on the ASR box (~20 s each), so a
// burst of mentions queues later ones past every timeout and their replies
// drop (observed 2026-07-18: 6 briefings in a minute, 3 replies lost). Cap
// executions per channel and say so, instead of failing silently. Per
// instance and best-effort, same tradeoff as the /api/voice rate limit.
const CHANNEL_LIMIT = 3;
const CHANNEL_WINDOW_MS = 60_000;
const channelHits = new Map<string, number[]>();

function channelLimited(channel: string): boolean {
  const now = Date.now();
  const hits = (channelHits.get(channel) ?? []).filter((t) => now - t < CHANNEL_WINDOW_MS);
  const limited = hits.length >= CHANNEL_LIMIT;
  if (!limited) hits.push(now);
  channelHits.set(channel, hits);
  return limited;
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
  let imageB64: string | null = null;
  let imageTitle = "image";
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
    if (typeof result?.data?.image_b64 === "string") {
      imageB64 = result.data.image_b64;
      if (typeof result.data.prompt === "string") imageTitle = result.data.prompt;
    }
  } catch {
    reply = "指令出錯：語音系統無回應";
  }
  await postToSlack(channel, reply);
  if (imageB64) {
    // new Uint8Array(...) re-views the bytes over a plain ArrayBuffer — the
    // DOM fetch BodyInit type rejects Node's Buffer directly.
    const uploaded = await uploadToSlack(channel, new Uint8Array(Buffer.from(imageB64, "base64")), imageTitle);
    // Never fail silently: the reply already said the image was drawn.
    if (!uploaded) await postToSlack(channel, "張圖整好咗但上載唔到Slack，遲啲再試");
  }
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
  if (channelLimited(channel)) {
    waitUntil(postToSlack(channel, "指令太密，一分鐘最多三個，唞一唞再試"));
    return new Response("ok");
  }

  waitUntil(
    runCommand(text, channel, new URL(request.url).origin, String(event.event_ts ?? Date.now())),
  );
  return new Response("ok");
}
