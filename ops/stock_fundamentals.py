"""Category 1 Fundamental Snapshot reports (SEC EDGAR-sourced), one HTML
file per ticker in content/stock-fundamentals/.

Run on demand: python ops/stock_fundamentals.py [--tickers AAPL,MSFT,...]
Defaults to STOCK_WATCHLIST (same env var as stock_daily.py) minus SPCX,
which is excluded here: it's the SpaceX-linked ticker, not a traditional
SEC reporting company, so it has no 10-K/proxy filings to pull from.

15-section report (Timeliness Flag through Red/Yellow/Green Flags), all
anchored to one company's latest 10-K accession:
  1. Timeliness Flag        9. Insider Ownership & Share Structure
  2. Business in Plain English   10. Risk Factor Highlights
  3. Key Financials          11. Durability & Moat Notes
  4. Segment Revenue Breakdown   12. Forward Watchlist
  5. Margin Trajectory       13. Quality of Earnings
  6. Balance Sheet Health    14. Valuation Snapshot
  7. Capital Allocation      15. Red/Yellow/Green Flags
  8. Share Count Trend

10 of 15 sections are fully deterministic Python (XBRL numbers + a little
live market data) -- no LLM, no hallucination risk on the figures
themselves. Only 5 sections need local-LLM synthesis grounded in filing
text: Business (2), Risk Factor Highlights (10), Durability & Moat (11),
Forward Watchlist (12), Insider Ownership (9).

Pipeline per ticker:
  1. SEC EDGAR company_tickers.json -> CIK
  2. data.sec.gov submissions API -> latest 10-K + DEF 14A accession/doc
  3. data.sec.gov companyfacts API -> XBRL numeric data, filtered to the
     exact periods reported in that one latest 10-K filing
  4. Fetch + strip the 10-K/proxy HTML, extract relevant sections by
     heading/keyword, and have local Ollama synthesize the 5 LLM sections
     grounded only in those excerpts
  5. A small amount of live market data (current price via Yahoo's chart
     endpoint) for Capital Allocation's return yield and the Valuation
     Snapshot section

Not scheduled: 10-K/proxy data changes ~annually (unlike stock_daily.py's
daily prices), so this is meant to be rerun manually after a new filing.

Known limitations: XBRL tag names vary by company -- a metric reports
"N/A" if none of the candidate tags are found, rather than guessing.
Section extraction (both LLM excerpts and the segment-revenue regex
parser) is heading/keyword based and best-effort; when a section can't be
located, the report says so instead of guessing. Segment Revenue Breakdown
in particular is currently only confirmed working for AAPL's table shapes
(Products/Services + geographic) -- other tickers honestly report N/A
rather than force-fitting a parser tuned for a different company's table.
"""

import argparse
import datetime as dt
import html as html_lib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT, "content", "stock-fundamentals")
LOG_PATH = os.path.join(ROOT, "asr", "logs", "stock_fundamentals.log")
NZ_TZ = ZoneInfo("Pacific/Auckland")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Deliberately not the shared OLLAMA_MODEL (gemma3:4b, tuned for spoken
# Cantonese naturalness on the voice path). This report is a written
# document, never read aloud, and needs faithful text extraction instead:
# tested on a real AAPL 10-K excerpt with no disclosed customer/geographic
# concentration, gemma3:4b fabricated a "20%+ concentrated in Apple Inc.
# itself" flag on two separate attempts (2026-07-28). qwen3:8b (think:false)
# fixed that, then lfm2.5 was benchmarked against it on the same real AAPL
# excerpts (2026-07-28): matched qwen3:8b's quality (no false-flag repeat)
# at roughly half the wall-clock time -- swapped in as the default. See the
# num_predict note on _ollama_generate for the one config gotcha it needs.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "lfm2.5")
SEC_UA = "voice-ecosystem stock-fundamentals research (timlok@gmail.com)"
SEC_DELAY = 0.2  # SEC asks for <=10 req/sec; well under that.

PALETTE_ENV = os.path.join(ROOT, "ops", "palette.env")
# Fallback only -- ops/palette.env is the source of truth. Kept in sync so a
# missing/corrupt file degrades gracefully instead of crashing report
# generation, same "never guess, but never crash either" posture as the rest
# of this module's SEC-gap handling.
_DEFAULT_PALETTE = {
    "LEAN_BULLISH": "#15803d", "LEAN_BULLISH_PULLBACK": "#d97706",
    "LEAN_NEUTRAL": "#64748b", "LEAN_BEARISH": "#dc2626", "LEAN_INSUFFICIENT": "#94a3b8",
    "RSI_OVERSOLD": "#2563eb", "RSI_NEUTRAL": "#6b7280",
    "RSI_STRONG": "#16a34a", "RSI_OVERBOUGHT": "#f59e0b",
}


def load_palette() -> dict:
    palette = dict(_DEFAULT_PALETTE)
    try:
        with open(PALETTE_ENV, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                palette[key.strip()] = value.strip()
    except OSError:
        pass
    return palette


PALETTE = load_palette()

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("stock_fundamentals")

DEFAULT_WATCHLIST = os.environ.get(
    "STOCK_WATCHLIST", "AAPL,MSFT,NVDA,TSLA,GOOGL,AMZN,META,AON,SPCX"
)
EXCLUDE_NO_SEC_FILINGS = {"SPCX"}

# Candidate XBRL us-gaap tags, tried in order, per concept -- companies use
# different (sometimes custom) tags for the same line item. All queried via
# _xbrl_series(facts, tags, accn, unit=...), scoped to one 10-K accession --
# Category 1 never needs the TTM/point-in-time machinery ops/stock_valuation.py
# has, since everything here is anchored to a single annual filing.
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
]
NET_INCOME_TAGS = ["NetIncomeLoss"]
GROSS_PROFIT_TAGS = ["GrossProfit"]
OPERATING_INCOME_TAGS = ["OperatingIncomeLoss"]
OCF_TAGS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
]
CAPEX_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsForCapitalImprovements",
    "PaymentsToAcquireProductiveAssets",
]
REPURCHASE_TAGS = [
    "PaymentsForRepurchaseOfCommonStock",
    "PaymentsForRepurchaseOfCommonStockAndPreferredStock",
]
DIVIDEND_TAGS = ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"]
ACQUISITION_TAGS = ["PaymentsToAcquireBusinessesNetOfCashAcquired"]
SHARES_TAGS = ["CommonStockSharesOutstanding"]

# New tags for the Balance Sheet Health / Valuation Snapshot sections --
# same names ops/stock_valuation.py already uses/verified for debt, cash,
# equity, diluted EPS. Re-verified live here 2026-07-28 against AAPL,
# MSFT, GOOGL, TSLA (all resolved for every tag except TSLA, which lacks
# split noncurrent/current debt tags and falls back to the combined one --
# handled by _total_debt below). CURRENT_ASSETS_TAGS/CURRENT_LIABILITIES_TAGS
# are new to this project; also verified live for all four.
LONGTERM_DEBT_NONCURRENT_TAGS = ["LongTermDebtNoncurrent"]
LONGTERM_DEBT_CURRENT_TAGS = ["LongTermDebtCurrent"]
LONGTERM_DEBT_COMBINED_TAGS = ["LongTermDebt"]
CASH_TAGS = ["CashAndCashEquivalentsAtCarryingValue"]
SHORT_TERM_INVESTMENTS_TAGS = ["ShortTermInvestments", "MarketableSecuritiesCurrent"]
STOCKHOLDERS_EQUITY_TAGS = ["StockholdersEquity"]
CURRENT_ASSETS_TAGS = ["AssetsCurrent"]
CURRENT_LIABILITIES_TAGS = ["LiabilitiesCurrent"]
DILUTED_EPS_TAGS = ["EarningsPerShareDiluted"]  # unit "USD/shares", not "USD"


# ---------------------------------------------------------------------------
# SEC EDGAR fetch helpers
# ---------------------------------------------------------------------------

def _sec_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    time.sleep(SEC_DELAY)
    return data


def _sec_get_json(url: str) -> dict:
    return json.loads(_sec_get(url))


_CIK_MAP = None


def _cik_for_ticker(ticker: str) -> "str | None":
    global _CIK_MAP
    if _CIK_MAP is None:
        raw = _sec_get_json("https://www.sec.gov/files/company_tickers.json")
        _CIK_MAP = {v["ticker"].upper(): str(v["cik_str"]) for v in raw.values()}
    return _CIK_MAP.get(ticker.upper())


def _submissions(cik: str) -> dict:
    return _sec_get_json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")


def _latest_filing(subs: dict, form: str) -> "dict | None":
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    for i, f in enumerate(forms):
        if f == form:
            return {
                "accn": recent["accessionNumber"][i],
                "doc": recent["primaryDocument"][i],
                "filed": recent["filingDate"][i],
            }
    return None


def _filing_url(cik: str, accn: str, doc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/{doc}"


def _company_facts(cik: str) -> dict:
    return _sec_get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json")


def _fetch_text(url: str) -> str:
    raw = _sec_get(url).decode("utf-8", "replace")
    raw = re.sub(r"<ix:header.*?</ix:header>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_section(text: str, start_pat: str, end_pat: str, max_len: int = 8000) -> "str | None":
    # A heading pattern matches both the table-of-contents entry and the
    # real section; the real one is the match with the largest gap to the
    # next section's heading.
    starts = [m.start() for m in re.finditer(start_pat, text, re.I)]
    ends = [m.start() for m in re.finditer(end_pat, text, re.I)]
    best = None
    for s in starts:
        later_ends = [e for e in ends if e > s]
        if not later_ends:
            continue
        e = min(later_ends)
        if best is None or (e - s) > (best[1] - best[0]):
            best = (s, e)
    if not best:
        return None
    return text[best[0]:best[0] + max_len]


def _keyword_window(text: str, keyword_pat: str, before: int = 500, after: int = 6000) -> "str | None":
    m = re.search(keyword_pat, text, re.I)
    if not m:
        return None
    start = max(0, m.start() - before)
    return text[start:start + before + after]


def _current_price(ticker: str) -> "float | None":
    # Duplicated from ops/stock_valuation.py's _current_price -- same
    # per-script duplication norm this project already uses for the Notion
    # helpers across ops/stock_*.py.
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=5d&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
    except Exception as exc:
        log.warning("%s: current price fetch failed: %s", ticker, exc)
        return None
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        return None
    return result.get("meta", {}).get("regularMarketPrice")


# ---------------------------------------------------------------------------
# XBRL numeric extraction (deterministic sections)
# ---------------------------------------------------------------------------

def _xbrl_series(facts: dict, tags: list, accn: str, unit: str = "USD") -> "tuple[str | None, list]":
    for tag in tags:
        node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
        if not node:
            continue
        arr = node.get("units", {}).get(unit)
        if not arr:
            continue
        items = [i for i in arr if i.get("accn") == accn]
        if not items:
            continue
        by_end = {}
        for i in items:
            by_end[i["end"]] = i
        return tag, sorted(by_end.values(), key=lambda i: i["end"])
    return None, []


def _fcf_series(ocf: list, capex: list) -> list:
    capex_by_end = {i["end"]: i["val"] for i in capex}
    out = []
    for i in ocf:
        if i["end"] in capex_by_end:
            out.append({**i, "val": i["val"] - capex_by_end[i["end"]]})
    return out


def _fmt_usd(val: "float | None") -> str:
    if val is None:
        return "N/A"
    a = abs(val)
    if a >= 1e9:
        s = f"${a / 1e9:.1f}B"
    elif a >= 1e6:
        s = f"${a / 1e6:.1f}M"
    else:
        s = f"${a:,.0f}"
    return ("-" + s) if val < 0 else s


def _yoy(series: list) -> "float | None":
    if len(series) < 2 or series[-2]["val"] == 0:
        return None
    prev, cur = series[-2]["val"], series[-1]["val"]
    return (cur - prev) / abs(prev) * 100


def _total_debt(facts: dict, accn: str) -> "float | None":
    _, noncurrent = _xbrl_series(facts, LONGTERM_DEBT_NONCURRENT_TAGS, accn)
    _, current = _xbrl_series(facts, LONGTERM_DEBT_CURRENT_TAGS, accn)
    nc_val = noncurrent[-1]["val"] if noncurrent else None
    c_val = current[-1]["val"] if current else None
    if nc_val is not None or c_val is not None:
        return (nc_val or 0) + (c_val or 0)
    _, combined = _xbrl_series(facts, LONGTERM_DEBT_COMBINED_TAGS, accn)
    return combined[-1]["val"] if combined else None


def _cash_and_sti(facts: dict, accn: str) -> "tuple[float | None, float | None]":
    _, cash = _xbrl_series(facts, CASH_TAGS, accn)
    _, sti = _xbrl_series(facts, SHORT_TERM_INVESTMENTS_TAGS, accn)
    return (cash[-1]["val"] if cash else None), (sti[-1]["val"] if sti else None)


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------

def _esc(val) -> str:
    return html_lib.escape(str(val))


def _ul(items: list) -> str:
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"


def _table(headers: list, rows: list) -> str:
    thead = "<tr>" + "".join(f"<th>{_esc(h)}</th>" for h in headers) + "</tr>"
    trows = "".join(
        "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return f"<table>{thead}{trows}</table>"


def _rsi_color(rsi: "float | None") -> "str | None":
    # Conventional 30/50/70 RSI bands -- oversold/neutral/strong/overbought,
    # matching the 4 RSI_* colors in ops/palette.env. Shared by
    # stock_technicals.py and stock_day_range.py so both reports band RSI
    # identically.
    if rsi is None:
        return None
    if rsi < 30:
        return PALETTE["RSI_OVERSOLD"]
    if rsi < 50:
        return PALETTE["RSI_NEUTRAL"]
    if rsi < 70:
        return PALETTE["RSI_STRONG"]
    return PALETTE["RSI_OVERBOUGHT"]


def _soft_bg(hex_color: str, alpha: float = 0.15) -> str:
    # Blends a palette color with white so a badge can show colored text on
    # a light tinted background (the watchlist-mockup pill look) without
    # ops/palette.env needing a second "soft" color per state.
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    mix = lambda c: round(c * alpha + 255 * (1 - alpha))
    return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"


def _colored_span(text, color: "str | None") -> str:
    # Plain _esc(text) if color is None (e.g. no data to color) -- callers
    # never need a separate branch for the missing-color case. Rendered as a
    # small soft-background chip (mockup's RSI-column look) rather than bare
    # colored text.
    if color is None:
        return _esc(text)
    return (
        f'<span class="rounded-md px-2 py-1 text-xs font-semibold tabular-nums" '
        f'style="background:{_soft_bg(color)}; color:{color}">{_esc(text)}</span>'
    )


def _pill(text: str, color: str, icon: str = "") -> str:
    # Status pill (lean/setup badges): icon + label on a soft-background
    # rounded-full chip, colored from ops/palette.env via _soft_bg() --
    # matches the mockup's "Lean" column treatment.
    icon_html = f"<span>{icon}</span>" if icon else ""
    return (
        f'<span class="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs '
        f'font-semibold" style="background:{_soft_bg(color)}; color:{color}">'
        f'{icon_html}<span>{_esc(text)}</span></span>'
    )


def _table2(headers: list, rows: list) -> str:
    # Like _table(), but a cell may be a (text, html) tuple to carry
    # pre-built markup (e.g. _colored_span output) verbatim -- every other
    # plain-value cell still goes through _esc() as normal. Avoids the
    # double-escaping bug a raw _table() call would hit if fed markup
    # directly (hit once already in stock_day_range.py's overview table).
    def render(cell):
        return cell[1] if isinstance(cell, tuple) else _esc(cell)
    thead = "<tr>" + "".join(f"<th>{_esc(h)}</th>" for h in headers) + "</tr>"
    trows = "".join(
        "<tr>" + "".join(f"<td>{render(c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return f"<table>{thead}{trows}</table>"


def _card(title_html: str, body_html: str) -> str:
    # Section wrapper matching the watchlist-mockup's white rounded-2xl
    # panel. title_html is inserted verbatim (callers already _esc() the
    # parts they build from raw data); body_html renders inside
    # .prose-report so the many bare <p>/<h3>/<ul>/<table> tags the
    # _section_* functions already emit pick up the report's typography
    # without every call site needing its own Tailwind classes.
    return (
        f'<section class="rounded-2xl border border-slate-200 bg-white shadow-panel">'
        f'<div class="border-b border-slate-200 px-6 py-4">'
        f'<h2 class="text-sm font-semibold text-slate-900">{title_html}</h2></div>'
        f'<div class="prose-report px-6 py-5">{body_html}</div>'
        f'</section>'
    )


# ---------------------------------------------------------------------------
# Shared Tailwind CDN <head> boilerplate + page chrome, used by
# stock_fundamentals.py/stock_technicals.py/stock_day_range.py's page
# templates (imported as sf.TAILWIND_HEAD / sf.PAGE_HEADER / sf.PAGE_FOOTER).
# Light-only, matching the watchlist-mockup design -- no CSS custom
# properties, no @media(prefers-color-scheme) branch.
# ---------------------------------------------------------------------------

TAILWIND_HEAD = """<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script>
  tailwind.config = {
    theme: {
      extend: {
        fontFamily: { sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'] },
        colors: { ink: '#0f172a', muted: '#475569', line: '#e2e8f0' },
        boxShadow: { panel: '0 1px 2px rgba(15, 23, 42, 0.04), 0 8px 24px rgba(15, 23, 42, 0.06)' }
      }
    }
  }
</script>
<style>
  body { font-feature-settings: 'tnum' 1, 'ss01' 1; }
  .prose-report { color: #334155; font-size: 0.875rem; line-height: 1.6; }
  .prose-report > p, .prose-report > ul { margin: 0.6rem 0; }
  .prose-report > :first-child { margin-top: 0; }
  .prose-report > :last-child { margin-bottom: 0; }
  .prose-report h3 { font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.06em; color: #64748b; margin: 1.25rem 0 0.5rem; }
  .prose-report ul { list-style: disc; padding-left: 1.25rem; }
  .prose-report li { margin: 0.25rem 0; }
  .prose-report em { color: #64748b; font-style: normal; }
  .prose-report a { color: #0f172a; text-decoration: underline; text-underline-offset: 2px; }
  .prose-report p.cite { color: #94a3b8; font-size: 0.75rem; margin-top: 0.75rem; }
  .prose-report p.flag { color: #dc2626; font-weight: 700; }
  .prose-report table { width: 100%; border-collapse: collapse; margin: 0.75rem 0; font-size: 0.8125rem; }
  .prose-report th { text-align: left; padding: 0.5rem 0.75rem; font-size: 0.7rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.05em; color: #64748b; border-bottom: 1px solid #e2e8f0; }
  .prose-report td { padding: 0.5rem 0.75rem; border-bottom: 1px solid #f1f5f9; vertical-align: top; }
  .prose-report tbody tr:hover { background: #f8fafc; }
</style>"""

PAGE_HEADER = """<body class="min-h-full bg-slate-50 text-ink antialiased">
<main class="min-h-full px-4 py-8 sm:px-6 lg:px-8">
<div class="mx-auto max-w-4xl space-y-6">
<header class="space-y-1.5">
  <p class="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">{eyebrow}</p>
  <h1 class="text-2xl font-semibold tracking-tight text-slate-900">{heading}</h1>
  {meta}
</header>
<div class="space-y-6">
"""

PAGE_FOOTER = """</div>
</div>
</main>
</body>
</html>
"""


def _cite(text: str) -> str:
    return f'<p class="cite">Source: {_esc(text)}</p>'


def _llm_html(text: str) -> "tuple[str, bool]":
    # Converts a plain-text LLM reply into HTML: a leading "FLAG:" becomes a
    # styled marker, "-"/"*"-prefixed lines become a real <ul>, remaining
    # lines become <p> paragraphs. Returns (html, flagged).
    text = text.strip()
    flagged = text.startswith("FLAG:")
    if flagged:
        text = text[len("FLAG:"):].strip()
    text = text.replace("**", "")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    bullets = [l.lstrip("-* ").strip() for l in lines if l.startswith(("-", "*"))]
    prose = [l for l in lines if not l.startswith(("-", "*"))]
    parts = []
    if flagged:
        parts.append('<p class="flag">&#9888; FLAG</p>')
    parts.extend(f"<p>{_esc(l)}</p>" for l in prose)
    if bullets:
        parts.append(_ul(_esc(b) for b in bullets))
    if not parts:
        parts.append(f"<p>{_esc(text)}</p>")
    return "".join(parts), flagged


# ---------------------------------------------------------------------------
# Section 1: Timeliness Flag (deterministic)
# ---------------------------------------------------------------------------

def _section_timeliness(subs: dict, tenk_accn: str, tenk_filed: str) -> str:
    lines = [f"Latest 10-K: filed {tenk_filed} (accession {tenk_accn})."]
    newer = []
    latest_10q = _latest_filing(subs, "10-Q")
    latest_8k = _latest_filing(subs, "8-K")
    if latest_10q and latest_10q["filed"] > tenk_filed:
        newer.append(f"a 10-Q filed {latest_10q['filed']}")
    if latest_8k and latest_8k["filed"] > tenk_filed:
        newer.append(f"an 8-K filed {latest_8k['filed']}")
    if newer:
        lines.append(
            "Newer data exists (" + " and ".join(newer) + ") -- this report "
            "uses only the annual 10-K figures below, not the newer filing(s). "
            "See ops/stock_earnings.py (Category 2) for quarterly-reactive "
            "updates and ops/stock_valuation.py (Category 3) for a TTM-based "
            "valuation."
        )
    else:
        lines.append("No newer 10-Q or 8-K found on file -- the 10-K above is the most recent SEC filing.")
    try:
        age_days = (dt.date.today() - dt.date.fromisoformat(tenk_filed)).days
        lines.append(f"This 10-K is {age_days} days old as of this report's generation date.")
    except ValueError:
        pass
    return "".join(f"<p>{_esc(l)}</p>" for l in lines)


# ---------------------------------------------------------------------------
# Section 4: Segment Revenue Breakdown -- best-effort text extraction.
# XBRL's companyfacts API has no dimensional segment breakdown at all
# (confirmed live in ops/stock_valuation.py's Category 3 build), so this
# regex-parses the 10-K's own MD&A/notes text instead. Confirmed working
# live for AAPL's two table shapes (2026-07-28); other tickers use
# differently-named/shaped segments and honestly return {} rather than
# force-fitting AAPL's parser onto them -- same "N/A over guessing" ethic
# as the rest of this project's extraction code.
# ---------------------------------------------------------------------------

_GEO_SEGMENT_NAMES = ["Americas", "Europe", "Greater China", "Japan", "Rest of Asia Pacific"]


def _extract_product_segments(text: str) -> dict:
    m = re.search(r"[Nn]et sales by category for \d{4}.*?\(dollars in millions\):", text)
    if not m:
        m = re.search(r"[Nn]et sales by (?:reportable )?segment for \d{4}.*?\(in millions\):", text)
    if not m:
        return {}
    window = text[m.end():m.end() + 1500]
    end_m = re.search(r"Total net sales", window)
    window = window[:end_m.start()] if end_m else window
    # Strip the "2025 Change 2024 Change 2023" column-header boilerplate so
    # it doesn't get swallowed into the first row's label.
    window = re.sub(r"^\s*\d{4}(?:\s+Change\s+\d{4}){1,2}\s+", "", window)
    segments = {}
    for row_m in re.finditer(r"([A-Za-z][A-Za-z ,()0-9]{1,40}?)\s+\$?\s*([\d]{1,3}(?:,\d{3})+)", window):
        name = re.sub(r"\s*\(\d+\)\s*$", "", row_m.group(1).strip())
        val = float(row_m.group(2).replace(",", ""))
        if val > 0:
            segments[name] = val
    return segments


def _extract_geo_segments(text: str) -> dict:
    m = re.search(
        r"Americas\s+Europe\s+Greater China\s+Japan\s+Rest of Asia Pacific\s+Corporate\s+Total\s+Net sales\s+",
        text,
    )
    if not m:
        return {}
    window = text[m.end():m.end() + 300]
    toks = re.findall(r"\$\s*([\d,]+|\W)", window)
    segments = {}
    for name, tok in zip(_GEO_SEGMENT_NAMES, toks):
        try:
            val = float(tok.replace(",", ""))
        except ValueError:
            continue
        if val > 0:
            segments[name] = val
    return segments


def _section_segment_breakdown(product_segments: dict, geo_segments: dict, accn: str, filed: str) -> str:
    parts = []
    if product_segments:
        rows = [[name, _fmt_usd(val * 1_000_000)] for name, val in product_segments.items()]
        parts.append("<h3>Products / Categories (latest FY)</h3>" + _table(["Segment", "Net Sales"], rows))
    else:
        parts.append("<p>Products/category breakdown: N/A -- no recognizable segment revenue table found in this filing's text.</p>")
    if geo_segments:
        rows = [[name, _fmt_usd(val * 1_000_000)] for name, val in geo_segments.items()]
        parts.append("<h3>Geographic (latest FY)</h3>" + _table(["Region", "Net Sales"], rows))
    else:
        parts.append("<p>Geographic breakdown: N/A -- no recognizable segment revenue table found in this filing's text.</p>")
    parts.append(_cite(
        f"10-K text, accession {accn}, filed {filed} -- best-effort extraction from filing "
        "prose/tables, not from XBRL (which has no dimensional segment data)"
    ))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Sections 3, 5, 6, 7, 8, 13, 14, 15 -- deterministic (no LLM)
# ---------------------------------------------------------------------------

def _section_key_financials(rev, ni, fcf, accn: str, filed: str) -> "tuple[str, float | None]":
    rows = []
    for label, series in (("Revenue", rev), ("Net income", ni), ("Free cash flow", fcf)):
        if not series:
            rows.append([label, "N/A", "N/A (no matching XBRL tag found)"])
            continue
        chg = _yoy(series)
        chg_str = f"{chg:+.1f}%" if chg is not None else "N/A (prior year not in this filing)"
        rows.append([label, _fmt_usd(series[-1]["val"]), chg_str])
    revenue_yoy = _yoy(rev) if rev else None
    conversion = None
    if ni and fcf and ni[-1]["val"]:
        conversion = fcf[-1]["val"] / ni[-1]["val"] * 100
    rows.append(["Cash conversion (FCF / Net income)", f"{conversion:.0f}%" if conversion is not None else "N/A", ""])
    html = _table(["Metric", "Latest FY", "YoY"], rows)
    html += _cite(f"SEC EDGAR XBRL company facts, 10-K accession {accn}, filed {filed}")
    return html, revenue_yoy


def _section_margins(rev, gp, oi, ni) -> "tuple[str, float | None]":
    if not rev:
        return "<p>N/A -- revenue tag not found, cannot compute margins.</p>", None
    ends = sorted({i["end"] for i in rev})[-3:]
    rev_by, gp_by, oi_by, ni_by = (
        {i["end"]: i["val"] for i in s} for s in (rev, gp, oi, ni)
    )
    rows_data = []
    for end in ends:
        r = rev_by.get(end)
        if not r:
            continue
        rows_data.append({
            "end": end,
            "gross": (gp_by[end] / r * 100) if end in gp_by else None,
            "operating": (oi_by[end] / r * 100) if end in oi_by else None,
            "net": (ni_by[end] / r * 100) if end in ni_by else None,
        })

    def f(x):
        return f"{x:.1f}%" if x is not None else "N/A"

    rows = [[row["end"], f(row["gross"]), f(row["operating"]), f(row["net"])] for row in rows_data]
    html = _table(["Fiscal Year End", "Gross Margin", "Operating Margin", "Net Margin"], rows)

    biggest_bps = None
    if len(rows_data) >= 2:
        first, last = rows_data[0], rows_data[-1]
        deltas = {
            k: (last[k] - first[k]) * 100
            for k in ("gross", "operating", "net")
            if first[k] is not None and last[k] is not None
        }
        if deltas:
            biggest = max(deltas, key=lambda k: abs(deltas[k]))
            biggest_bps = deltas[biggest]
            direction = "expanded" if biggest_bps > 0 else "compressed"
            html += (
                f"<p>Biggest driver: {biggest} margin {direction} "
                f"{abs(biggest_bps):.0f} bps from {first['end']} to {last['end']} "
                "-- the largest swing of the three.</p>"
            )
    return html, biggest_bps


def _section_balance_sheet(cash, sti, debt, equity, cur_assets, cur_liab, accn: str, filed: str) -> "tuple[str, float | None, float | None]":
    cash_sti = None
    if cash is not None:
        cash_sti = cash + (sti or 0)
    net_cash = None
    if cash_sti is not None and debt is not None:
        net_cash = cash_sti - debt
    leverage = (debt / equity) if (debt is not None and equity) else None
    current_ratio = (cur_assets / cur_liab) if (cur_assets is not None and cur_liab) else None

    def money(v):
        return _fmt_usd(v) if v is not None else "N/A"

    net_cash_label = "N/A"
    if net_cash is not None:
        net_cash_label = f"{_fmt_usd(net_cash)} net {'cash' if net_cash >= 0 else 'debt'}"

    rows = [
        ["Cash & short-term investments", money(cash_sti)],
        ["Total debt", money(debt)],
        ["Net cash / (net debt)", net_cash_label],
        ["Stockholders' equity", money(equity)],
        ["Debt / Equity", f"{leverage:.2f}" if leverage is not None else "N/A"],
        ["Current ratio (current assets / current liabilities)", f"{current_ratio:.2f}" if current_ratio is not None else "N/A"],
    ]
    html = _table(["Metric", "Value"], rows)
    html += _cite(f"SEC EDGAR XBRL company facts, 10-K accession {accn}, filed {filed}")
    return html, leverage, net_cash


def _section_capital_allocation(ocf, capex, repurchases, dividends, acquisitions,
                                 market_cap: "float | None", accn: str, filed: str) -> str:
    def latest(series):
        return series[-1]["val"] if series else None

    o, c, r, d, a = (latest(s) for s in (ocf, capex, repurchases, dividends, acquisitions))
    rows = [
        ["Operating cash flow", _fmt_usd(o)],
        ["Capex", _fmt_usd(c)],
        ["Share repurchases", _fmt_usd(r) if r is not None else "N/A (tag absent -- may mean none, or a different tag)"],
        ["Dividends paid", _fmt_usd(d) if d is not None else "N/A (tag absent -- may mean none paid)"],
        ["Acquisitions, net of cash acquired", _fmt_usd(a)],
    ]
    html = _table(["Metric", "Latest FY"], rows)
    returned = sum(x for x in (r, d) if x)
    reinvested = sum(x for x in (c, a) if x)
    if returned or reinvested:
        verdict = (
            "returning more capital to shareholders than it is reinvesting"
            if returned > reinvested
            else "reinvesting more into the business than it is returning to shareholders"
        )
        html += f"<p>On these figures, the company is {verdict}: {_fmt_usd(returned)} returned vs {_fmt_usd(reinvested)} reinvested.</p>"
    if returned and market_cap:
        yield_pct = returned / market_cap * 100
        html += f"<p>Return yield (buybacks + dividends / market cap): {yield_pct:.1f}%.</p>"
    else:
        html += "<p>Return yield: N/A (needs both buybacks/dividends and a current market cap).</p>"
    html += _cite(f"SEC EDGAR XBRL company facts, 10-K accession {accn}, filed {filed}")
    return html


def _section_share_count_trend(shares: list) -> str:
    if not shares or len(shares) < 2:
        return "<p>N/A -- insufficient multi-year share-count history in this filing.</p>"
    ends = sorted({i["end"] for i in shares})[-3:]
    by_end = {i["end"]: i["val"] for i in shares}
    rows = [[e, f"{by_end[e]:,.0f}"] for e in ends if e in by_end]
    html = _table(["Fiscal Year End", "Shares Outstanding"], rows)
    usable_ends = [e for e in ends if e in by_end]
    if len(usable_ends) >= 2:
        first_val, last_val = by_end[usable_ends[0]], by_end[usable_ends[-1]]
        if first_val:
            pct = (last_val - first_val) / first_val * 100
            direction = "shrunk, consistent with net buybacks outpacing issuance" if pct < 0 else "grown, meaning issuance (incl. stock comp) outpaced buybacks"
            html += f"<p>Shares outstanding {direction}: {pct:+.1f}% from {usable_ends[0]} to {usable_ends[-1]}.</p>"
    return html


def _section_quality_of_earnings(ni, fcf) -> "tuple[str, float | None]":
    if not ni or not fcf or not ni[-1]["val"]:
        return "<p>N/A -- net income or free cash flow not available for this filing.</p>", None
    ni_val, fcf_val = ni[-1]["val"], fcf[-1]["val"]
    ratio_pct = fcf_val / ni_val * 100
    rows = [
        ["Net income", _fmt_usd(ni_val)],
        ["Free cash flow", _fmt_usd(fcf_val)],
        ["FCF / Net income", f"{ratio_pct:.0f}%"],
    ]
    html = _table(["Metric", "Value"], rows)
    if ratio_pct >= 110:
        note = "FCF meaningfully exceeds net income -- often a sign of high-quality earnings (non-cash charges such as D&amp;A outweighing working-capital drag), though it can also reflect deferred-revenue growth."
    elif ratio_pct >= 80:
        note = "FCF and net income are broadly aligned -- no major red flag on earnings quality from this ratio alone."
    else:
        note = "FCF trails net income noticeably -- worth checking working-capital swings, capex intensity, or one-off items before taking reported net income at face value."
    html += f"<p>{note}</p>"
    return html, ratio_pct


def _section_valuation(current_price, shares_out, diluted_eps, fcf, debt, cash, sti, accn: str, filed: str) -> str:
    if current_price is None or not shares_out:
        return "<p>N/A -- could not fetch a current market price or shares-outstanding figure.</p>"
    market_cap = current_price * shares_out
    net_debt = None
    if debt is not None or cash is not None:
        net_debt = (debt or 0) - ((cash or 0) + (sti or 0))
    ev = market_cap + net_debt if net_debt is not None else None
    pe = (current_price / diluted_eps) if diluted_eps else None
    fcf_val = fcf[-1]["val"] if fcf else None
    fcf_yield = (fcf_val / market_cap * 100) if fcf_val and market_cap else None
    rows = [
        ["Current price", f"${current_price:,.2f}"],
        ["Market cap", _fmt_usd(market_cap)],
        ["Enterprise value", _fmt_usd(ev) if ev is not None else "N/A"],
        ["Trailing P/E (latest FY diluted EPS)", f"{pe:.1f}x" if pe else "N/A"],
        ["FCF yield (latest FY)", f"{fcf_yield:.1f}%" if fcf_yield is not None else "N/A"],
    ]
    html = _table(["Metric", "Value"], rows)
    html += (
        "<p>Based on latest-FY reported figures (not TTM) against today's market price -- "
        "see Category 3 (ops/stock_valuation.py) for a full TTM-based DCF/multiples valuation.</p>"
    )
    html += _cite(f"SEC EDGAR XBRL, 10-K accession {accn}, filed {filed}; current price from Yahoo Finance, fetched at report generation time")
    return html


def _section_flags(signals: dict) -> str:
    rows = []

    bps = signals.get("margin_bps")
    if bps is None:
        rows.append(["Margin trend", "N/A", "insufficient multi-year data"])
    elif bps >= 100:
        rows.append(["Margin trend", "Green", f"biggest margin expanded {bps:.0f} bps over the window"])
    elif bps <= -100:
        rows.append(["Margin trend", "Red", f"biggest margin compressed {abs(bps):.0f} bps over the window"])
    else:
        rows.append(["Margin trend", "Yellow", f"margins roughly flat ({bps:+.0f} bps)"])

    lev = signals.get("leverage")
    net_cash = signals.get("net_cash")
    if lev is None and net_cash is None:
        rows.append(["Leverage", "N/A", "debt/equity data unavailable"])
    elif (net_cash is not None and net_cash >= 0) or (lev is not None and lev < 0.5):
        rows.append(["Leverage", "Green", "net cash positive or debt/equity below 0.5"])
    elif lev is not None and lev <= 1.5:
        rows.append(["Leverage", "Yellow", f"debt/equity {lev:.2f}"])
    else:
        rows.append(["Leverage", "Red", "high leverage (debt/equity above 1.5) or negative equity"])

    cc = signals.get("cash_conversion_pct")
    if cc is None:
        rows.append(["Cash conversion (FCF/NI)", "N/A", "net income or FCF unavailable"])
    elif cc >= 90:
        rows.append(["Cash conversion (FCF/NI)", "Green", f"{cc:.0f}%"])
    elif cc >= 60:
        rows.append(["Cash conversion (FCF/NI)", "Yellow", f"{cc:.0f}%"])
    else:
        rows.append(["Cash conversion (FCF/NI)", "Red", f"{cc:.0f}%"])

    g = signals.get("revenue_yoy")
    if g is None:
        rows.append(["Revenue growth YoY", "N/A", "prior-year figure unavailable"])
    elif g > 5:
        rows.append(["Revenue growth YoY", "Green", f"{g:+.1f}%"])
    elif g >= 0:
        rows.append(["Revenue growth YoY", "Yellow", f"{g:+.1f}%"])
    else:
        rows.append(["Revenue growth YoY", "Red", f"{g:+.1f}%"])

    disclosure_flag = signals.get("concentration_flag") or signals.get("ownership_flag")
    if disclosure_flag:
        rows.append(["Disclosure flags", "Red", "Risk Factor Highlights or Insider Ownership raised a FLAG"])
    else:
        rows.append(["Disclosure flags", "Green", "no FLAG raised in Risk Factor Highlights or Insider Ownership"])

    flag_icons = {"Green": "\U0001F7E2", "Yellow": "\U0001F7E1", "Red": "\U0001F534", "N/A": "⚪"}
    for row in rows:
        row[1] = f"{flag_icons.get(row[1], '')} {row[1]}".strip()

    html = _table(["Category", "Flag", "Basis"], rows)
    html += "<p><em>Heuristic screen based on figures already computed above in this report -- not a rating or recommendation.</em></p>"
    return html


# ---------------------------------------------------------------------------
# LLM-grounded narrative sections (2, 10, 11, 12, 9)
# ---------------------------------------------------------------------------

BUSINESS_PROMPT = (
    "Act as a senior equity analyst. Below is the 'Item 1. Business' "
    "section from {ticker}'s most recent 10-K filed with the SEC (EDGAR "
    "accession {accn}, filed {filed}). Using ONLY the text given, "
    "summarize in exactly 5-6 sentences what the company sells, to whom, "
    "how it makes money, and how it organizes its reportable segments (if "
    "mentioned in this excerpt). Do not invent facts not present in the "
    "excerpt. Under 180 words.\n\n---\n{excerpt}\n---"
)

RISK_HIGHLIGHTS_PROMPT = (
    "Below is an excerpt from {ticker}'s most recent 10-K 'Item 1A. Risk "
    "Factors' section (EDGAR accession {accn}, filed {filed}). Using ONLY "
    "this text, summarize the 5-7 most significant disclosed risks as a "
    "bullet list, one short line each (start each line with '- '). If the "
    "excerpt discloses that a single customer accounts for more than 20% "
    "of revenue, start your entire answer with 'FLAG:' before the bullet "
    "list. Do not invent risks not present in the excerpt. Under 200 "
    "words.\n\n---\n{excerpt}\n---"
)

MOAT_PROMPT = (
    "Below is an excerpt from {ticker}'s most recent 10-K (Item 1 Business "
    "and Item 1A Risk Factors, EDGAR accession {accn}, filed {filed}). "
    "Using ONLY this text, note any disclosed factors relevant to "
    "competitive durability: customer switching costs or ecosystem "
    "lock-in, recurring or services revenue mix, and supply chain "
    "concentration or dependency. If a factor isn't discussed in the "
    "excerpt, say so plainly rather than speculating. Under 200 "
    "words.\n\n---\n{excerpt}\n---"
)

FORWARD_WATCHLIST_PROMPT = (
    "Below is an excerpt from {ticker}'s most recent 10-K 'Item 1A. Risk "
    "Factors' section (EDGAR accession {accn}, filed {filed}). Using ONLY "
    "this text, list forward-looking risk themes worth monitoring under "
    "these four categories, one bullet each (start each line with '- '): "
    "Product/Technology Cycle Risk, Regulatory/Legal Risk, Foreign "
    "Exchange Risk, China/Geographic Concentration Risk. If a category "
    "isn't discussed in the excerpt, write '<Category name>: not "
    "disclosed in this excerpt' for that one rather than guessing. Under "
    "200 words.\n\n---\n{excerpt}\n---"
)

OWNERSHIP_PROMPT = (
    "Below is an excerpt from {ticker}'s SEC filings (EDGAR accession "
    "{accn}, filed {filed}). Using ONLY this text, report: (1) whether the "
    "company has a dual-class or multi-class share structure with unequal "
    "voting power, and (2) any insider/officer/director ownership "
    "percentage disclosed. If public shareholders hold under 50% of "
    "voting power, start your entire answer with 'FLAG:'. If the excerpt "
    "doesn't disclose this, say so plainly -- do not guess. Under 150 "
    "words.\n\n---\n{excerpt}\n---"
)


def _ollama_generate(prompt: str, num_predict: int = 1500) -> str:
    # lfm2.5 is also a hybrid reasoning model, but unlike qwen3:8b its
    # "think": false doesn't suppress reasoning -- it just dumps <think>
    # into the visible content instead. Left unset, Ollama correctly
    # separates reasoning into message.thinking and keeps content clean,
    # but the reasoning still consumes num_predict: the 400 budget tuned
    # for qwen3:8b's think:false path left content empty here (observed
    # 2026-07-28); 1500 was enough for all prompts on a real AAPL filing
    # (1.3-2.8k reasoning chars, still ~2x faster than the old
    # qwen3:8b/400 baseline).
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
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read()).get("message", {}).get("content", "").strip()


def _section_business(ticker: str, tenk_text: str, accn: str, filed: str) -> str:
    excerpt = _extract_section(tenk_text, r"Item\s*1\.?\s*Business\b", r"Item\s*1A\.?\s*Risk\s*Factors\b")
    if not excerpt:
        return "<p>N/A -- could not locate the 'Item 1. Business' section in the filing text.</p>"
    try:
        reply = _ollama_generate(BUSINESS_PROMPT.format(ticker=ticker, accn=accn, filed=filed, excerpt=excerpt[:6000]))
    except Exception as exc:
        log.warning("%s: business summary failed: %s", ticker, exc)
        return f"<p>N/A -- local LLM summary failed ({_esc(exc)}). Raw section available in the 10-K.</p>"
    html, _ = _llm_html(reply)
    return html + _cite(f"10-K Item 1, accession {accn}, filed {filed}")


def _section_risk_highlights(ticker: str, risk_text: str, accn: str, filed: str) -> "tuple[str, bool]":
    if not risk_text:
        return "<p>N/A -- could not locate the 'Item 1A. Risk Factors' section in the filing text.</p>", False
    try:
        reply = _ollama_generate(RISK_HIGHLIGHTS_PROMPT.format(ticker=ticker, accn=accn, filed=filed, excerpt=risk_text[:6000]))
    except Exception as exc:
        log.warning("%s: risk highlights failed: %s", ticker, exc)
        return f"<p>N/A -- local LLM analysis failed ({_esc(exc)}).</p>", False
    html, flagged = _llm_html(reply)
    return html + _cite(f"10-K Item 1A, accession {accn}, filed {filed}"), flagged


def _section_moat(ticker: str, biz_text: str, risk_text: str, accn: str, filed: str) -> str:
    excerpt = (biz_text[:4000] + "\n\n" + risk_text[:4000]).strip()
    if not excerpt.strip():
        return "<p>N/A -- could not locate Business/Risk Factors sections in the filing text.</p>"
    try:
        reply = _ollama_generate(MOAT_PROMPT.format(ticker=ticker, accn=accn, filed=filed, excerpt=excerpt[:6000]))
    except Exception as exc:
        log.warning("%s: moat notes failed: %s", ticker, exc)
        return f"<p>N/A -- local LLM analysis failed ({_esc(exc)}).</p>"
    html, _ = _llm_html(reply)
    return html + _cite(f"10-K Item 1 / Item 1A, accession {accn}, filed {filed}")


def _section_forward_watchlist(ticker: str, risk_text: str, accn: str, filed: str) -> str:
    if not risk_text:
        return "<p>N/A -- could not locate the 'Item 1A. Risk Factors' section in the filing text.</p>"
    try:
        reply = _ollama_generate(FORWARD_WATCHLIST_PROMPT.format(ticker=ticker, accn=accn, filed=filed, excerpt=risk_text[:6000]))
    except Exception as exc:
        log.warning("%s: forward watchlist failed: %s", ticker, exc)
        return f"<p>N/A -- local LLM analysis failed ({_esc(exc)}).</p>"
    html, _ = _llm_html(reply)
    return html + _cite(f"10-K Item 1A, accession {accn}, filed {filed}")


def _section_ownership(ticker: str, tenk_text: str, tenk_accn: str, tenk_filed: str,
                        proxy_text: "str | None", proxy_accn: "str | None", proxy_filed: "str | None") -> "tuple[str, bool]":
    excerpt = None
    cite_accn, cite_filed = tenk_accn, tenk_filed
    if proxy_text:
        excerpt = (
            _extract_section(proxy_text, r"Security\s*Ownership\s*of\s*Certain\s*Beneficial\s*Owners",
                              r"(PROPOSAL|EXECUTIVE\s*COMPENSATION|Item\s*13)", max_len=6000)
            or _keyword_window(proxy_text, r"beneficially\s*own")
        )
        if excerpt:
            cite_accn, cite_filed = proxy_accn, proxy_filed
    if not excerpt:
        # Fall back to the 10-K cover page area, which lists share classes.
        excerpt = tenk_text[:4000]
    try:
        reply = _ollama_generate(OWNERSHIP_PROMPT.format(ticker=ticker, accn=cite_accn, filed=cite_filed, excerpt=excerpt[:6000]))
    except Exception as exc:
        log.warning("%s: ownership check failed: %s", ticker, exc)
        return f"<p>N/A -- local LLM analysis failed ({_esc(exc)}).</p>", False
    html, flagged = _llm_html(reply)
    source = "proxy (DEF 14A)" if proxy_text and cite_accn == proxy_accn else "10-K"
    return html + _cite(f"{source}, accession {cite_accn}, filed {cite_filed}"), flagged


# ---------------------------------------------------------------------------
# HTML page template
# ---------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{ticker} -- Category 1 Fundamental Snapshot</title>
{tailwind_head}
</head>
{page_header}{sections}
{page_footer}"""

SECTION_TITLES = [
    "Timeliness Flag",
    "Business in Plain English",
    "Key Financials",
    "Segment Revenue Breakdown",
    "Margin Trajectory",
    "Balance Sheet Health",
    "Capital Allocation",
    "Share Count Trend",
    "Insider Ownership & Share Structure",
    "Risk Factor Highlights",
    "Durability & Moat Notes",
    "Forward Watchlist",
    "Quality of Earnings",
    "Valuation Snapshot",
    "Red/Yellow/Green Flags",
]


def _html_page(ticker: str, cik: str, accn: str, filed: str, tenk_url: str, section_bodies: list) -> str:
    sections_html = "".join(
        _card(f"{i}. {_esc(title)}", body)
        for i, (title, body) in enumerate(zip(SECTION_TITLES, section_bodies), start=1)
    )
    generated = dt.datetime.now(NZ_TZ).strftime("%Y-%m-%d %H:%M")
    meta = (
        f'<p class="text-sm text-slate-600">SEC EDGAR, CIK {_esc(cik)}. Latest 10-K: accession '
        f'{_esc(accn)}, filed {_esc(filed)} (<a class="underline hover:text-slate-900" '
        f'href="{_esc(tenk_url)}">source filing</a>).</p>'
        f'<p class="text-xs text-slate-500">Report generated {_esc(generated)} NZT.</p>'
    )
    page_header = PAGE_HEADER.format(
        eyebrow="Category 1 Fundamental Snapshot", heading=_esc(ticker), meta=meta,
    )
    return PAGE_TEMPLATE.format(
        ticker=_esc(ticker), tailwind_head=TAILWIND_HEAD,
        page_header=page_header, sections=sections_html, page_footer=PAGE_FOOTER,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def build_report(ticker: str) -> "str | None":
    cik = _cik_for_ticker(ticker)
    if not cik:
        log.error("%s: no CIK found in SEC company_tickers.json", ticker)
        return None

    subs = _submissions(cik)
    tenk = _latest_filing(subs, "10-K")
    if not tenk:
        log.error("%s: no 10-K found in SEC submissions", ticker)
        return None
    proxy = _latest_filing(subs, "DEF 14A")

    facts = _company_facts(cik)
    accn, filed = tenk["accn"], tenk["filed"]
    rev_tag, rev = _xbrl_series(facts, REVENUE_TAGS, accn)
    _, ni = _xbrl_series(facts, NET_INCOME_TAGS, accn)
    _, gp = _xbrl_series(facts, GROSS_PROFIT_TAGS, accn)
    _, oi = _xbrl_series(facts, OPERATING_INCOME_TAGS, accn)
    _, ocf = _xbrl_series(facts, OCF_TAGS, accn)
    _, capex = _xbrl_series(facts, CAPEX_TAGS, accn)
    _, repurchases = _xbrl_series(facts, REPURCHASE_TAGS, accn)
    _, dividends = _xbrl_series(facts, DIVIDEND_TAGS, accn)
    _, acquisitions = _xbrl_series(facts, ACQUISITION_TAGS, accn)
    _, shares = _xbrl_series(facts, SHARES_TAGS, accn, unit="shares")
    _, diluted_eps_series = _xbrl_series(facts, DILUTED_EPS_TAGS, accn, unit="USD/shares")
    _, equity_series = _xbrl_series(facts, STOCKHOLDERS_EQUITY_TAGS, accn)
    _, cur_assets_series = _xbrl_series(facts, CURRENT_ASSETS_TAGS, accn)
    _, cur_liab_series = _xbrl_series(facts, CURRENT_LIABILITIES_TAGS, accn)
    fcf = _fcf_series(ocf, capex)
    debt = _total_debt(facts, accn)
    cash, sti = _cash_and_sti(facts, accn)
    equity = equity_series[-1]["val"] if equity_series else None
    cur_assets = cur_assets_series[-1]["val"] if cur_assets_series else None
    cur_liab = cur_liab_series[-1]["val"] if cur_liab_series else None
    diluted_eps = diluted_eps_series[-1]["val"] if diluted_eps_series else None
    shares_out = shares[-1]["val"] if shares else None

    current_price = _current_price(ticker)
    market_cap = (current_price * shares_out) if (current_price and shares_out) else None

    tenk_url = _filing_url(cik, tenk["accn"], tenk["doc"])
    try:
        tenk_text = _fetch_text(tenk_url)
    except Exception as exc:
        log.error("%s: failed to fetch 10-K text: %s", ticker, exc)
        tenk_text = ""

    proxy_text = proxy_accn = proxy_filed = None
    if proxy:
        try:
            proxy_url = _filing_url(cik, proxy["accn"], proxy["doc"])
            proxy_text = _fetch_text(proxy_url)
            proxy_accn, proxy_filed = proxy["accn"], proxy["filed"]
        except Exception as exc:
            log.warning("%s: failed to fetch proxy text: %s", ticker, exc)

    biz_text = _extract_section(tenk_text, r"Item\s*1\.?\s*Business\b", r"Item\s*1A\.?\s*Risk\s*Factors\b", max_len=4000) or "" if tenk_text else ""
    risk_text = _extract_section(tenk_text, r"Item\s*1A\.?\s*Risk\s*Factors\b", r"Item\s*1B\.?\s*Unresolved", max_len=6000) or "" if tenk_text else ""
    product_segments = _extract_product_segments(tenk_text) if tenk_text else {}
    geo_segments = _extract_geo_segments(tenk_text) if tenk_text else {}

    sec1 = _section_timeliness(subs, accn, filed)
    sec2 = _section_business(ticker, tenk_text, accn, filed) if tenk_text else "<p>N/A -- could not fetch 10-K text.</p>"
    sec3, revenue_yoy = _section_key_financials(rev, ni, fcf, accn, filed)
    sec4 = _section_segment_breakdown(product_segments, geo_segments, accn, filed)
    sec5, margin_bps = _section_margins(rev, gp, oi, ni)
    sec6, leverage, net_cash = _section_balance_sheet(cash, sti, debt, equity, cur_assets, cur_liab, accn, filed)
    sec7 = _section_capital_allocation(ocf, capex, repurchases, dividends, acquisitions, market_cap, accn, filed)
    sec8 = _section_share_count_trend(shares)
    sec9, ownership_flag = _section_ownership(ticker, tenk_text, accn, filed, proxy_text, proxy_accn, proxy_filed)
    sec10, concentration_flag = _section_risk_highlights(ticker, risk_text, accn, filed) if risk_text else (
        "<p>N/A -- could not locate the 'Item 1A. Risk Factors' section in the filing text.</p>", False)
    sec11 = _section_moat(ticker, biz_text, risk_text, accn, filed)
    sec12 = _section_forward_watchlist(ticker, risk_text, accn, filed)
    sec13, cash_conversion_pct = _section_quality_of_earnings(ni, fcf)
    sec14 = _section_valuation(current_price, shares_out, diluted_eps, fcf, debt, cash, sti, accn, filed)
    signals = {
        "margin_bps": margin_bps,
        "leverage": leverage,
        "net_cash": net_cash,
        "cash_conversion_pct": cash_conversion_pct,
        "revenue_yoy": revenue_yoy,
        "concentration_flag": concentration_flag,
        "ownership_flag": ownership_flag,
    }
    sec15 = _section_flags(signals)

    return _html_page(ticker, cik, accn, filed, tenk_url, [
        sec1, sec2, sec3, sec4, sec5, sec6, sec7, sec8, sec9,
        sec10, sec11, sec12, sec13, sec14, sec15,
    ])


def regenerate(ticker: str) -> bool:
    # Shared by main() and by stock_earnings.py/stock_valuation.py, which
    # call this after a successful Cat 2/Cat 3 report so the Cat 1 snapshot
    # (current price, Timeliness Flag) stays fresh whenever a ticker gets
    # new SEC activity -- not on its own schedule, since Cat 1 has none.
    try:
        report = build_report(ticker)
    except Exception:
        log.exception("%s: report generation failed", ticker)
        return False
    if not report:
        return False
    out_path = os.path.join(OUTPUT_DIR, f"{ticker}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    log.info("wrote %s", out_path)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=None, help="comma-separated ticker list")
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = [
            t.strip().upper() for t in DEFAULT_WATCHLIST.split(",")
            if t.strip() and t.strip().upper() not in EXCLUDE_NO_SEC_FILINGS
        ]

    written = 0
    for ticker in tickers:
        if regenerate(ticker):
            written += 1
    log.info("done: %d/%d tickers", written, len(tickers))
    print(f"wrote {written}/{len(tickers)} reports to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
