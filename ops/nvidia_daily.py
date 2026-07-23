"""Daily NVIDIA / AI-ecosystem strategic intelligence report.

Runs at 09:00 via the "VoiceOS NVIDIA Daily" scheduled task (runs as the
user, same isolation as workflows/notion_sync — not the VoiceASR service).

Pipeline: free Google News RSS search (no API key, no scraping of
paywalled article bodies — headline/snippet/link only) for the topic and
company list below, last 24h, then local Ollama writes the strategic
report from those headlines. Output is a local markdown file for manual
review — this script never posts anything anywhere.

Known limitation: the model sees headlines/snippets only, not full
article text, and is a local 4B model, not a frontier one — treat the
analysis as a first draft. The report ends with a self-rated confidence
score; anything under 90% is flagged for extra scrutiny before you post.
"""

import datetime as dt
import json
import logging
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT, "content", "nvidia-daily")
LOG_PATH = os.path.join(ROOT, "asr", "logs", "nvidia_daily.log")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")
NZ_TZ = ZoneInfo("Pacific/Auckland")

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("nvidia_daily")

KEYWORDS = [
    "NVIDIA", "Jensen Huang", "NVIDIA AI strategy", "NVIDIA products",
    "NVIDIA ecosystem", "NVIDIA partners", "NVIDIA customers",
    "AI infrastructure", "AI chips", "AI factories", "Sovereign AI",
    "robotics AI", "autonomous driving AI", "digital twins",
    "CUDA ecosystem", "NVIDIA DGX", "GB200", "Blackwell GPU",
    "Rubin NVIDIA", "Vera Rubin NVIDIA", "NVLink", "Omniverse",
    "NVIDIA Cosmos", "NVIDIA NeMo", "NVIDIA AI Enterprise", "CUDA-X",
    "NVSwitch", "Spectrum-X networking", "InfiniBand", "Grace CPU NVIDIA",
    "AI datacentres", "agentic AI", "physical AI", "humanoid robotics",
]

COMPANIES = [
    "OpenAI", "Microsoft AI", "Google AI", "Alphabet AI", "Meta AI",
    "Amazon AI", "AWS AI", "Oracle AI", "Tesla AI", "xAI", "Anthropic",
    "AMD", "Intel AI chip", "TSMC", "Foxconn AI", "SoftBank AI",
    "CoreWeave", "Dell AI", "HPE AI", "Cisco AI", "ServiceNow AI",
    "SAP AI", "Snowflake AI",
]

RSS_ENDPOINT = "https://news.google.com/rss/search"
PER_QUERY_LIMIT = 3
TOTAL_LIMIT = 90
REQUEST_DELAY = 0.4

# Two-stage generation: a bounded "digest" pass (select + summarize the top
# stories only — feeding all ~90 headlines through a full per-item strategic
# breakdown ate the entire output budget and never reached the actual
# deliverable), then a "content" pass that turns the digest into the social
# posts. This guarantees the LinkedIn/X/confidence-score sections always get
# generated instead of being cut off by earlier verbose sections.

DIGEST_PROMPT = """\
You are an elite AI Strategy Intelligence Analyst. Below is a \
pre-collected list of {count} news headlines published in the last 24 \
hours ({date}), gathered from Google News, relating to NVIDIA, Jensen \
Huang, the NVIDIA ecosystem, AI infrastructure, and its major partners/\
customers/competitors (OpenAI, Microsoft, Google, Meta, Amazon, Oracle, \
Tesla, xAI, Anthropic, AMD, Intel, TSMC, and others).

You have NO internet access. Use only the headline text, source, and date \
given — never invent article content, quotes, statistics, or details not \
present in the text given.

HEADLINES:
{headline_block}

---

TASK: From these headlines, select only the 8 most strategically \
important stories (ignore the rest entirely — do not list or analyze \
unselected headlines). For each selected story, copy its real source, \
date, and URL from the HEADLINES list above — never write the words \
"Source", "Date", or "URL" as literal placeholder text. Format each \
story exactly like this filled-in example:

1. **Alphabet Quadruples Profit to $112 Billion, Fueled by A.I. \
Investments** — The New York Times — Wed, 22 Jul 2026 20:42:37 GMT — \
https://news.google.com/rss/articles/CBMi...
   Summary: Alphabet reported a large profit increase attributed to AI \
investment.
   Why it matters: Validates the ROI case for hyperscaler AI capex.
   Category: Investment

Include the full URL exactly as it appears in the HEADLINES list — it's \
how the report gets verified later.

After the 8 stories, write one short paragraph (4-6 sentences) titled \
"## Connect the Dots" describing what patterns, if any, appear across \
today's stories (e.g. sovereign AI expansion, AI factories, CUDA \
lock-in, robotics acceleration, vertical integration). If there's too \
little material for a real pattern, say so rather than forcing one.

If (and only if) one of the 8 selected headlines is a Jensen Huang \
statement, interview, or speech, add a short "## Jensen Huang Analysis" \
paragraph (vision, strategic message, leadership/business lesson). \
Otherwise omit that section entirely.

Keep the whole response under 700 words. Rank the 8 stories by \
strategic importance. Do not write a conclusion or sign-off — stop after \
the last section above.
"""

CONTENT_PROMPT = """\
You are an elite AI Strategy Intelligence Analyst turning today's NVIDIA/\
AI-ecosystem digest into review-ready draft content. Ground everything \
below strictly in the digest given — do not invent facts, quotes, or \
statistics that aren't in it.

TODAY'S DIGEST ({date}):
{digest}

---

Produce ONLY the following sections, in markdown, in this exact order:

## Executive Summary
Exactly 50 words, suitable for busy executives.

## Three Key Takeaways
Bullet points only.

## LinkedIn Post
One post, 300-500 words. Strong hook, strategic insight, explain WHY it \
matters, plain English, end with a thought-provoking question. No \
clickbait. Position the author as someone who understands AI strategy, \
not someone just sharing news.

## X / Twitter Thread
5 tweets, each under 280 characters, numbered "1/" through "5/". \
Educational, actionable, no hype.

## Future Watch
Five specific developments worth monitoring over the next 30 days, based \
on today's digest.

## Confidence Score
A single overall self-rated confidence percentage (0-100%) in the \
factual accuracy of today's report, given it was built from headlines \
only with no full-article verification and no ability to cross-check \
sources. Explain briefly what limits the score. If the score is below \
90%, add this exact line on its own: \
"⚠️ Below 90% confidence — verify every fact against the source URL \
before posting anything from this report."
"""


def _fetch_rss(query: str) -> list:
    params = {"q": f"{query} when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    url = f"{RSS_ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            root = ET.fromstring(resp.read())
    except Exception as exc:
        log.warning("rss fetch failed for %r: %s", query, exc)
        return []
    items = []
    for item in root.findall(".//item")[:PER_QUERY_LIMIT]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        if not title or not link:
            continue
        items.append({"title": title, "link": link, "pub_date": pub_date, "source": source})
    return items


def collect_headlines() -> list:
    seen_links = set()
    collected = []
    for query in KEYWORDS + COMPANIES:
        for item in _fetch_rss(query):
            if item["link"] in seen_links:
                continue
            seen_links.add(item["link"])
            collected.append(item)
        time.sleep(REQUEST_DELAY)
    return collected[:TOTAL_LIMIT]


def build_digest_prompt(headlines: list, report_date: str) -> str:
    lines = [
        f"- {h['title']} | {h['source'] or 'unknown source'} | {h['pub_date']} | {h['link']}"
        for h in headlines
    ]
    headline_block = "\n".join(lines) if lines else "(no headlines collected in the last 24h)"
    return DIGEST_PROMPT.format(date=report_date, headline_block=headline_block, count=len(headlines))


def _ollama_generate(prompt: str, num_predict: int) -> str:
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_ctx": 8192, "num_predict": num_predict},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read()).get("message", {}).get("content", "")


def main() -> None:
    now = dt.datetime.now(NZ_TZ)
    report_date = now.strftime("%Y-%m-%d")
    out_path = os.path.join(OUTPUT_DIR, f"{report_date}.md")

    try:
        headlines = collect_headlines()
        log.info("collected %d headlines", len(headlines))
    except Exception:
        log.exception("headline collection failed")
        return

    date_label = now.strftime("%d %B %Y")
    digest_prompt = build_digest_prompt(headlines, date_label)
    try:
        digest = _ollama_generate(digest_prompt, num_predict=2200)
    except Exception:
        log.exception("ollama digest generation failed")
        return
    if not digest.strip():
        log.error("ollama returned an empty digest")
        return

    content_prompt = CONTENT_PROMPT.format(date=date_label, digest=digest)
    try:
        content = _ollama_generate(content_prompt, num_predict=2200)
    except Exception:
        log.exception("ollama content generation failed")
        content = "(content generation failed — see nvidia_daily.log; digest above is still usable)"

    header = f"<!-- generated {now.isoformat()} | {len(headlines)} headlines | model {OLLAMA_MODEL} -->\n\n"
    report = (
        f"# NVIDIA Daily Intelligence Report — {date_label}\n\n"
        f"## Today's Digest\n\n{digest.strip()}\n\n---\n\n{content.strip()}\n"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + report)
    log.info("wrote %s (%d chars)", out_path, len(report))


if __name__ == "__main__":
    main()
