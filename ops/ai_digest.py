"""Daily AI digest -- top AI news and hiccups -- published as a page
of the dashboard app at /dashboard/ai.

Scheduled task "VoiceOS AI Digest" (07:30 NZT, register with
ops/register_ai_digest.ps1, elevated). Same shape as the risk dashboard:
fetch locally, write dashboard/data/ai-digest.json (gitignored), then
publish with deploy_dashboard.publish(). Nothing on Vercel holds a secret.

Sources are free and every item links to its real origin:
  news    -- Google News RSS, last 24 h
  hiccups -- OpenAI and Anthropic status-page incidents (last 72 h) plus
             Google News outage/glitch headlines

Selection and ordering are deterministic. Workers AI only writes a
one-sentence summary for items that carry text beyond the headline (status
incident details), from that text alone; if it fails the
summaries are left empty rather than invented. A section that fetches
nothing keeps the previous run's items, marked stale, instead of going
blank.

Run manually: python ops/ai_digest.py [--no-deploy]
"""

import datetime as dt
import email.utils
import html
import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_fundamentals as sf  # noqa: E402  (workers_ai_generate)

OUT_PATH = os.path.join(ROOT, "dashboard", "data", "ai-digest.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", f"ai_digest-{dt.date.today():%Y-%m-%d}.log")
NZ_TZ = ZoneInfo("Pacific/Auckland")
PER_SECTION = 5
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

NEWS_QUERY = '"artificial intelligence" OR "AI model" OR OpenAI OR Anthropic OR "Google Gemini"'
HICCUP_QUERY = '(AI OR chatbot OR ChatGPT OR Gemini OR Claude) (outage OR glitch OR "went down" OR blunder OR hallucination)'
STATUS_FEEDS = {
    "OpenAI status": "https://status.openai.com/history.rss",
    "Anthropic status": "https://status.claude.com/history.rss",
}
STATUS_WINDOW = dt.timedelta(hours=72)

log = logging.getLogger("ai_digest")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)


def _get(url: str, ua: str = BROWSER_UA) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def _iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _google_news(query: str) -> list:
    params = {"q": f"{query} when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    root = ET.fromstring(_get(f"https://news.google.com/rss/search?{urllib.parse.urlencode(params)}"))
    items, seen = [], set()
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        # Google News titles end in " - Source"; the same story recurs per outlet.
        headline = title[: -len(source) - 3] if source and title.endswith(f" - {source}") else title
        if not headline or headline.lower() in seen:
            continue
        seen.add(headline.lower())
        items.append({
            "title": headline,
            "source": source or "Google News",
            "url": (item.findtext("link") or "").strip(),
            "context": "",
            "timestamp": _iso(email.utils.parsedate_to_datetime(item.findtext("pubDate"))),
        })
    return items


def fetch_news() -> list:
    return _google_news(NEWS_QUERY)[:PER_SECTION]


def fetch_hiccups() -> list:
    cutoff = dt.datetime.now(dt.timezone.utc) - STATUS_WINDOW
    items = []
    for name, url in STATUS_FEEDS.items():
        try:
            root = ET.fromstring(_get(url))
        except Exception as exc:
            log.warning("status feed %s failed: %s", name, exc)
            continue
        for item in root.findall(".//item"):
            published = email.utils.parsedate_to_datetime(item.findtext("pubDate"))
            if published < cutoff:
                continue
            items.append({
                "title": (item.findtext("title") or "").strip(),
                "source": name,
                "url": (item.findtext("link") or "").strip().replace(".com//", ".com/"),
                "context": _strip_html(item.findtext("description"))[:300],
                "timestamp": _iso(published),
            })
    items.sort(key=lambda i: i["timestamp"], reverse=True)
    # Up to 3 status incidents, news fills the rest -- a busy status week
    # otherwise crowds out every outage story.
    items = items[:3]
    try:
        items += _google_news(HICCUP_QUERY)
    except Exception as exc:
        log.warning("hiccup news fetch failed: %s", exc)
    return items[:PER_SECTION]


SUMMARY_PROMPT = """You write one-sentence summaries for an AI digest page.
For each numbered item below, write ONE plain-English sentence (max 25 words)
saying what the item is about. Use ONLY the title and context given -- do not
add facts, names, numbers or dates that are not in them. If the title is all
there is, restate it more plainly.

Reply with a JSON array of exactly {n} strings, in order, and nothing else.

{items}"""


def summarize(items: list) -> None:
    # Only items with text beyond the headline: Google News RSS gives the
    # headline alone, and a "summary" of that was just a shorter headline.
    for item in items:
        item["summary"] = ""
    items = [i for i in items if i["context"]]
    if not items:
        return
    block = "\n".join(
        f"{n}. Title: {i['title']}\n   Source: {i['source']}" + (f"\n   Context: {i['context']}" if i["context"] else "")
        for n, i in enumerate(items, 1)
    )
    try:
        reply = sf.workers_ai_generate(SUMMARY_PROMPT.format(n=len(items), items=block), max_tokens=900)
        summaries = json.loads(reply[reply.index("["): reply.rindex("]") + 1])
        if len(summaries) != len(items):
            raise ValueError(f"expected {len(items)} summaries, got {len(summaries)}")
    except Exception as exc:
        log.warning("summaries skipped: %s", exc)
        summaries = [""] * len(items)
    for item, summary in zip(items, summaries):
        item["summary"] = str(summary).strip()


def _previous() -> dict:
    try:
        with open(OUT_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def build() -> "dict | None":
    previous = _previous()
    digest = {"updated_at": dt.datetime.now(NZ_TZ).isoformat(timespec="seconds"), "stale": []}
    fetched = 0
    for key, fetch in (("top_ai_news", fetch_news), ("top_ai_hiccups", fetch_hiccups)):
        try:
            items = fetch()
        except Exception as exc:
            log.warning("%s fetch failed: %s", key, exc)
            items = []
        if items:
            fetched += 1
            summarize(items)
            for i in items:
                i.pop("context", None)
            digest[key] = items
        else:
            digest[key] = previous.get(key, [])
            digest["stale"].append(key)
        log.info("%s: %d items%s", key, len(digest[key]), " (kept from last run)" if not items else "")
    if fetched == 0:
        log.warning("nothing fetched for any section -- leaving the last digest in place")
        return None
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(digest, fh, ensure_ascii=False, indent=2)
    return digest


def main() -> None:
    digest = build()
    if digest is None or "--no-deploy" in sys.argv:
        return
    import deploy_dashboard
    deploy_dashboard.publish()


if __name__ == "__main__":
    main()
