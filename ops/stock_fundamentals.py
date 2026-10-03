"""Category 1 Fundamental Snapshot reports (SEC EDGAR-sourced), one HTML
file per ticker in content/stock-fundamentals/.

Run on demand: python ops/stock_fundamentals.py [--tickers AAPL,MSFT,...]
Defaults to STOCK_WATCHLIST (same env var as stock_daily.py) minus SPCX,
which is excluded here: it's the SpaceX-linked ticker, not a traditional
SEC reporting company, so it has no 10-K/proxy filings to pull from.

17-section report (Timeliness Flag through Red/Yellow/Green Flags), all
anchored to one company's latest 10-K accession except Insider Activity,
which reads the last 6 months of Form 4s:
  1. Timeliness Flag         10. Insider Activity (6 months)
  2. Business in Plain English  11. Management Incentives
  3. Key Financials            12. Risk Factor Highlights
  4. Segment Revenue Breakdown 13. Durability & Moat Notes
  5. Margin Trajectory         14. Forward Watchlist
  6. Balance Sheet Health      15. Quality of Earnings
  7. Capital Allocation        16. Valuation Snapshot
  8. Share Count Trend         17. Red/Yellow/Green Flags
  9. Insider Ownership & Share Structure

10 of 17 sections are fully deterministic Python (XBRL numbers + a little
live market data) -- no LLM, no hallucination risk on the figures
themselves. Insider Activity (10) is deterministic too, with a short LLM
narration of its finished table. 6 sections need LLM synthesis grounded in
filing text: Business (2), Insider Ownership (9), Management Incentives
(11), Risk Factor Highlights (12), Durability & Moat (13), Forward
Watchlist (14).

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
import atexit
import collections
import datetime as dt
import html as html_lib
import json
import logging
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT, "content", "stock-fundamentals")
LOG_PATH = os.path.join(ROOT, "asr", "logs", f"stock_fundamentals-{dt.date.today():%Y-%m-%d}.log")
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
# num_predict note on _ollama_local for the one config gotcha it needs.
# Since 2026-10-02 this is the local FALLBACK only: narration goes to Workers
# AI first (sf.workers_ai_or_local) and drops back here if that call fails.
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
# A dedicated non-propagating logger, not logging.basicConfig() -- basicConfig
# attaches its handler to the ROOT logger, so every propagating logger in any
# module that imports this one (asr/router.py's "router" logger, pulled in via
# the Category 4/6 scripts) ended up writing into stock_fundamentals.log too.
# Same pattern every other Category script already uses.
log = logging.getLogger("stock_fundamentals")
log.propagate = False
log.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)

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
    "PaymentsForRepurchaseOfEquity",
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
DILUTED_SHARES_TAGS = ["WeightedAverageNumberOfDilutedSharesOutstanding"]
REPURCHASE_VALUE_TAGS = [
    "StockRepurchasedAndRetiredDuringPeriodValue",
    "TreasuryStockValueAcquiredCostMethod",
    "StockRepurchasedDuringPeriodValue",
]
REPURCHASE_SHARES_TAGS = [
    "StockRepurchasedAndRetiredDuringPeriodShares",
    "TreasuryStockSharesAcquired",
    "StockRepurchasedDuringPeriodShares",
]
RD_TAGS = ["ResearchAndDevelopmentExpense"]
DA_TAGS = [
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAndAmortization",
    "Depreciation",
]
TAX_EXPENSE_TAGS = ["IncomeTaxExpenseBenefit"]
PRETAX_INCOME_TAGS = [
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
]


# ---------------------------------------------------------------------------
# SEC EDGAR fetch helpers
# ---------------------------------------------------------------------------

RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2.0  # seconds before the first retry, doubled each time


def urlopen_retry(req: urllib.request.Request, timeout: int,
                  attempts: int = RETRY_ATTEMPTS) -> bytes:
    """urlopen + read, retrying transient network/TLS/5xx failures.

    Shared by every ops/stock_*.py external fetch. Single-shot urlopen lost
    whole reports in practice: connection timeouts (WinError 10060) dropped
    tickers from the published dashboard on 4 of 7 days, and one TLS
    interception blip (CERTIFICATE_VERIFY_FAILED) zeroed an entire Category 4
    run. A 4xx is a real answer from the server, so it is raised immediately
    rather than hammered; only 429/5xx and socket/TLS faults are retried.
    """
    delay = RETRY_BACKOFF
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts:
                raise
            reason = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == attempts:
                raise
            reason = str(exc)
        log.warning("fetch failed (%s), retrying in %.0fs [%d/%d]: %s",
                    reason, delay, attempt, attempts - 1, req.full_url)
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")  # loop always returns or raises


def _sec_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
    data = urlopen_retry(req, timeout=30)
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
        payload = json.loads(urlopen_retry(req, timeout=20))
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


def _xbrl_annual_history(facts: dict, tags: list, unit: str = "USD", n: int = 5) -> "tuple[str | None, list]":
    # _xbrl_series above is scoped to ONE accession, which only yields the
    # fiscal years that filing happens to present. For multi-year trends
    # (capital allocation, ROIC) pull the tag's full fact array across all
    # filings instead: form=="10-K" only (10-Q quarter-ends are not fiscal
    # years), deduped by "end" keeping the most-recently-filed value so a
    # restatement wins, ascending, last n.
    # Tag preference is by RECENCY, not by position in `tags`. Filers
    # abandon tags: TSLA and AON both still carry
    # DepreciationDepletionAndAmortization, but stopped populating it after
    # 2017 in favour of DepreciationAndAmortization. Taking the first tag
    # with any data at all silently paired 2017 D&A with 2025 capex and
    # produced an 11x "reinvestment ratio" -- found live, 2026-09-19.
    best = (None, [])
    for tag in tags:
        node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
        if not node:
            continue
        arr = node.get("units", {}).get(unit)
        if not arr:
            continue
        by_end = {}
        for item in arr:
            if item.get("form") != "10-K":
                continue
            # For DURATION facts, "end" alone does not identify a period: AON
            # tags a single since-2012 cumulative buyback ($26.2bn over
            # 174.9m shares) carrying the same end date as its CY2025 annual
            # fact ($1.0bn over 2.7m shares). Deduping on end alone mixed the
            # cumulative value with the annual share count and implied an
            # $810/share repurchase price against a ~$300 stock. Keep only
            # ~one-fiscal-year durations; instants (no start) pass through.
            if item.get("start"):
                span = (dt.date.fromisoformat(item["end"]) - dt.date.fromisoformat(item["start"])).days
                if not 300 <= span <= 400:
                    continue
            existing = by_end.get(item["end"])
            if not existing or item.get("filed", "") >= existing.get("filed", ""):
                by_end[item["end"]] = item
        if not by_end:
            continue
        series = sorted(by_end.values(), key=lambda i: i["end"])
        if not best[1] or series[-1]["end"] > best[1][-1]["end"]:
            best = (tag, series)
    return best[0], best[1][-n:]


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
                                 market_cap: "float | None", accn: str, filed: str,
                                 facts: dict, current_price: "float | None",
                                 shares_out: "float | None") -> str:
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
    html += _capital_allocation_scorecard(facts, current_price, shares_out)
    html += _cite(f"SEC EDGAR XBRL company facts, 10-K accession {accn}, filed {filed}")
    return html


# --- Capital allocation scorecard (prompt 48) -------------------------------
# Five graded components, each 0-2, averaged into an A-F letter. Every
# component is deterministic XBRL arithmetic; the LLM never sees this.
# Components that can't be computed are skipped, not scored 0 -- a filer
# that simply doesn't tag R&D must not be marked down for it. Fewer than
# 3 available components means no grade at all rather than a grade resting
# on one number.

_GRADE_BANDS = [(1.7, "A"), (1.3, "B"), (0.9, "C"), (0.5, "D")]


def _aligned(*series: list) -> bool:
    # Every series must end in the same fiscal year before their values may
    # be divided by one another. Guards the cross-era pairing described in
    # _xbrl_annual_history: a filer that stopped tagging one input years ago
    # must yield N/A, never a ratio spanning two different decades.
    ends = [dt.date.fromisoformat(x[-1]["end"]) for x in series if x]
    if len(ends) != len(series):
        return False
    return (max(ends) - min(ends)).days <= 120


def _grade_letter(avg: float) -> str:
    for cutoff, letter in _GRADE_BANDS:
        if avg >= cutoff:
            return letter
    return "F"


def _pct_change(series: list) -> "float | None":
    if len(series) < 2 or not series[0]["val"]:
        return None
    return (series[-1]["val"] - series[0]["val"]) / abs(series[0]["val"])


def _capital_allocation_scorecard(facts: dict, current_price: "float | None",
                                  shares_out: "float | None") -> str:
    rows, scores = [], []

    def add(name, detail, score):
        rows.append([name, detail, "N/A" if score is None else f"{score}/2"])
        if score is not None:
            scores.append(score)

    _, capex_h = _xbrl_annual_history(facts, CAPEX_TAGS)
    _, da_h = _xbrl_annual_history(facts, DA_TAGS)
    if capex_h and da_h and da_h[-1]["val"] and _aligned(capex_h, da_h):
        ratio = abs(capex_h[-1]["val"]) / abs(da_h[-1]["val"])
        add("Organic reinvestment (capex / D&A)", f"{ratio:.2f}x",
            2 if ratio >= 1.2 else 1 if ratio >= 0.8 else 0)
    else:
        add("Organic reinvestment (capex / D&A)",
            "N/A -- capex or D&A not tagged, or their latest tagged fiscal years differ", None)

    _, rd_h = _xbrl_annual_history(facts, RD_TAGS)
    _, rev_h = _xbrl_annual_history(facts, REVENUE_TAGS)
    rd_growth, rev_growth = _pct_change(rd_h), _pct_change(rev_h)
    if rd_growth is not None and rev_growth is not None and _aligned(rd_h, rev_h):
        gap = rd_growth - rev_growth
        add("R&D trend vs revenue trend",
            f"R&D {rd_growth * 100:+.0f}% vs revenue {rev_growth * 100:+.0f}% over {len(rd_h)} FYs",
            2 if gap >= 0 else 1 if gap >= -0.10 else 0)
    else:
        add("R&D trend vs revenue trend", "N/A -- R&D not tagged (common outside tech/pharma)", None)

    # Average repurchase price, two ways. Preferred: the filer's own paired
    # value/share repurchase tags, which give the price exactly. Fallback:
    # cumulative spend divided by the fall in diluted share count -- that
    # denominator is NET of issuance, so it understates shares bought and
    # therefore overstates the price. The fallback is conservative by
    # construction: it can make good buybacks look mediocre, never the
    # reverse. Both paths are labelled so the two are never confused.
    _, rep_val_h = _xbrl_annual_history(facts, REPURCHASE_VALUE_TAGS)
    _, rep_sh_h = _xbrl_annual_history(facts, REPURCHASE_SHARES_TAGS, unit="shares")
    _, rep_h = _xbrl_annual_history(facts, REPURCHASE_TAGS)
    _, sh_h = _xbrl_annual_history(facts, DILUTED_SHARES_TAGS, unit="shares")

    avg_price, basis = None, None
    if rep_val_h and rep_sh_h:
        # Sum only the fiscal years BOTH tags cover. AON tags a 2023
        # repurchase value but no 2023 share count, so summing each series
        # whole divided 3 years of spend by 2 years of shares and implied
        # $810/share against a ~$300 stock.
        val_by_end = {i["end"]: abs(i["val"]) for i in rep_val_h}
        sh_by_end = {i["end"]: abs(i["val"]) for i in rep_sh_h}
        common = sorted(set(val_by_end) & set(sh_by_end))
        shares_bought = sum(sh_by_end[e] for e in common)
        if common and shares_bought > 0:
            avg_price = sum(val_by_end[e] for e in common) / shares_bought
            basis = "reported repurchase value/shares over %d FY%s" % (len(common), "" if len(common) == 1 else "s")
    if avg_price is None and rep_h and len(sh_h) >= 2 and _aligned(rep_h, sh_h):
        retired = sh_h[0]["val"] - sh_h[-1]["val"]
        if retired > 0:
            avg_price = sum(abs(i["val"]) for i in rep_h) / retired
            basis = "implied from spend / fall in diluted shares, an upper bound (net of issuance)"

    if avg_price is None:
        add("Buyback execution (avg repurchase price vs today)",
            "N/A -- no aligned repurchase tags, or diluted share count rose over the "
            "period so issuance outpaced buybacks", None)
    elif current_price and avg_price > current_price * 2.5 and basis and basis.startswith("implied"):
        # Not a verdict on execution: it means issuance (stock comp, or shares
        # issued for an acquisition) swamped the retirement, leaving a
        # denominator too small to divide by. Seen live on MSFT.
        add("Buyback execution (avg repurchase price vs today)",
            f"N/A -- implied {_fmt_usd(avg_price)}/sh exceeds 2.5x the "
            f"{_fmt_usd(current_price)} price, so issuance offset most of the buyback "
            f"and no meaningful average can be implied", None)
    elif current_price:
        disc = (current_price - avg_price) / avg_price
        add("Buyback execution (avg repurchase price vs today)",
            f"{_fmt_usd(avg_price)}/sh vs {_fmt_usd(current_price)} today "
            f"({disc * 100:+.0f}%) -- {basis}",
            2 if disc >= 0.15 else 1 if disc >= -0.05 else 0)
    else:
        add("Buyback execution (avg repurchase price vs today)",
            "N/A -- no current price to compare against", None)

    _, div_h = _xbrl_annual_history(facts, DIVIDEND_TAGS)
    _, ocf_h = _xbrl_annual_history(facts, OCF_TAGS)
    if div_h and ocf_h and capex_h and _aligned(div_h, ocf_h, capex_h):
        fcf = ocf_h[-1]["val"] - abs(capex_h[-1]["val"])
        if fcf > 0:
            payout = abs(div_h[-1]["val"]) / fcf
            add("Dividend sustainability (dividends / FCF)", f"{payout * 100:.0f}% of FCF",
                2 if payout <= 0.50 else 1 if payout <= 0.80 else 0)
        else:
            add("Dividend sustainability (dividends / FCF)",
                "dividends paid against negative free cash flow", 0)
    else:
        add("Dividend sustainability (dividends / FCF)",
            "N/A -- no dividends tagged (may mean none paid), or the inputs' latest "
            "tagged fiscal years differ", None)

    # ROIC = NOPAT / (total debt + equity - cash), latest FY. The effective
    # tax rate is taken from the same year rather than assumed.
    _, oi_h = _xbrl_annual_history(facts, OPERATING_INCOME_TAGS)
    _, tax_h = _xbrl_annual_history(facts, TAX_EXPENSE_TAGS)
    _, pre_h = _xbrl_annual_history(facts, PRETAX_INCOME_TAGS)
    _, eq_h = _xbrl_annual_history(facts, STOCKHOLDERS_EQUITY_TAGS)
    _, cash_h = _xbrl_annual_history(facts, CASH_TAGS)
    _, debt_h = _xbrl_annual_history(facts, LONGTERM_DEBT_COMBINED_TAGS)
    roic = None
    if oi_h and eq_h and pre_h and tax_h and pre_h[-1]["val"] and _aligned(oi_h, eq_h, pre_h, tax_h):
        tax_rate = min(max(tax_h[-1]["val"] / pre_h[-1]["val"], 0.0), 0.50)
        nopat = oi_h[-1]["val"] * (1 - tax_rate)
        invested = (eq_h[-1]["val"] + (debt_h[-1]["val"] if debt_h else 0)
                    - (cash_h[-1]["val"] if cash_h else 0))
        if invested > 0:
            roic = nopat / invested
    if roic is not None:
        add("Return on invested capital (latest FY)", f"{roic * 100:.1f}%",
            2 if roic >= 0.15 else 1 if roic >= 0.08 else 0)
    else:
        add("Return on invested capital (latest FY)",
            "N/A -- needs operating income, tax rate and positive invested capital", None)

    html = "<h4>Capital Allocation Scorecard</h4>"
    html += _table(["Component", "Measure", "Score"], rows)
    if len(scores) >= 3:
        avg = sum(scores) / len(scores)
        html += (f"<p><strong>Overall grade: {_grade_letter(avg)}</strong> "
                 f"({avg:.2f}/2 across {len(scores)} of 5 components; "
                 f"unavailable components are skipped, not scored zero).</p>")
    else:
        html += (f"<p><strong>Overall grade: not assigned</strong> -- only {len(scores)} "
                 f"of 5 components could be computed, too few to grade on.</p>")
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

INCENTIVES_PROMPT = (
    "You are reading the Compensation Discussion and Analysis from {ticker}'s "
    "proxy statement (accession {accn}, filed {filed}).\n\n{excerpt}\n\n"
    "Using ONLY this text, answer in four short bullets:\n"
    "1. Which metrics management is actually paid on (name them).\n"
    "2. Roughly how much of total pay is performance-based versus time-based, "
    "if the text says.\n"
    "3. Whether the metrics reward long-term shareholder value or short-term "
    "results, and why.\n"
    "4. Anything the text shows about how demanding the targets are.\n"
    "If the text does not support a point, write 'not disclosed in this excerpt' "
    "for that bullet rather than guessing. Do not invent figures. Under 220 words."
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


# ---------------------------------------------------------------------------
# Narration health -- a wedged Ollama is invisible one call at a time: every
# narration failure degrades a single section and the run still reports
# success. It stayed wedged 2026-09-02..07 (llama-server evicted a model and
# never recovered) and five days of Category 4/6 and day-range reports
# published with no narrative at all, unalerted. The daily jobs record each
# call here and alert once at the end of a run if most of them failed.
# ---------------------------------------------------------------------------

_llm_attempts = 0
_llm_failures = 0


def llm_record(ok: bool) -> None:
    global _llm_attempts, _llm_failures
    _llm_attempts += 1
    if not ok:
        _llm_failures += 1


def alert_if_narration_dead(job: str, log_name: str) -> None:
    """Push one ntfy alert if at least half this run's narration calls failed."""
    if _llm_attempts and _llm_failures * 2 >= _llm_attempts:
        from notify import notify
        notify("股票報告降級",
               f"{job}: {_llm_failures}/{_llm_attempts} narration calls failed -- "
               f"check Ollama / Workers AI and asr/logs/{log_name}", priority=4)


def fail_on_report_errors(job: str, failed: list, log_name: str) -> None:
    """Push one ntfy and raise if any ticker's report failed this run.

    The reactive jobs catch per-ticker errors so one bad filer can't block the
    rest, which also meant the task exited 0x0 on a failure: TSLA's valuation
    report died on a Notion schema mismatch 2026-10-03 and only a log read
    found it. Raising lets main() exit 1 so Task Scheduler shows it too.
    Failed tickers keep their old accession cursor and retry next run.
    """
    if not failed:
        return
    from notify import notify
    notify("股票報告失敗", f"{job}: {', '.join(failed)} failed -- check asr/logs/{log_name}", priority=4)
    raise RuntimeError(f"{len(failed)} report(s) failed: {', '.join(failed)}")


# ---------------------------------------------------------------------------
# Cloudflare Workers AI -- the daily jobs (Category 4 technicals, Category 6
# dashboard, day range) narrate here instead of local qwen3:8b: public market
# data only, ~7x faster than the 4 GB GTX 1650 (benchmarked 2026-10-01), and it
# keeps qwen3:8b off the GPU in the 10:00 window it shares with the voice
# briefing. Credentials live in the gitignored repo-root .env. Budget: these
# jobs use ~3,300 of the 10,000 free neurons/day (resets 00:00 UTC). The
# reactive SEC-filing reports (Category 1/2/3/5, ~4,400 neurons per filing)
# use it too since 2026-10-02, but through workers_ai_or_local(): a
# multi-filing earnings night can exhaust the free tier, and those runs land
# at 02:00-03:00 when the GPU is idle, so they fall back to local Ollama
# rather than writing blank sections.
# ---------------------------------------------------------------------------

WORKERS_AI_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"


def _repo_env() -> dict:
    env = {}
    try:
        with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    env[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return env


def workers_ai_generate(prompt: str, max_tokens: int) -> str:
    env = _repo_env()
    account = env.get("CF_ACCOUNT_ID") or os.environ.get("CF_ACCOUNT_ID")
    token = env.get("CF_AI_TOKEN") or os.environ.get("CF_AI_TOKEN")
    if not (account and token):
        raise RuntimeError("CF_ACCOUNT_ID / CF_AI_TOKEN not set in .env")
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1/chat/completions",
        data=json.dumps({
            "model": WORKERS_AI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        reply = json.loads(resp.read())["choices"][0]["message"]["content"] or ""
    return reply.strip()


def workers_ai_or_local(prompt: str, max_tokens: int, local, logger) -> str:
    """Workers AI first; on any failure or empty reply, call local() instead."""
    try:
        reply = workers_ai_generate(prompt, max_tokens)
        if reply:
            return reply
        logger.warning("Workers AI returned empty content -- falling back to local Ollama")
    except Exception as exc:
        logger.warning("Workers AI failed (%s) -- falling back to local Ollama", exc)
    return local()


_evicted: set = set()


def _ollama_post(path: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        f"{OLLAMA_URL}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def ollama_make_room(model: str) -> None:
    """Unload every other resident model before a local call to `model`.

    asr/router.py pins gemma3:4b with keep_alive 24h and the Ollama service
    runs OLLAMA_MAX_LOADED_MODELS=1, so a qwen3:8b/lfm2.5 call otherwise has
    to win an implicit eviction race -- the one that wedged the runner on
    2026-09-19. An explicit keep_alive 0 unload (what `ollama stop` does) was
    the fix that worked by hand. Evicted models are reloaded at exit with the
    router's 24h pin so voice replies don't pay a cold load afterwards.
    """
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/ps", timeout=10) as resp:
            loaded = [m["name"] for m in json.loads(resp.read()).get("models", [])]
    except Exception as exc:
        log.warning("ollama ps failed (%s); calling %s without unloading", exc, model)
        return
    for name in loaded:
        if name.split(":latest")[0] == model.split(":latest")[0]:
            continue
        try:
            _ollama_post("/api/chat", {"model": name, "messages": [], "keep_alive": 0}, 30)
            if not _evicted:
                atexit.register(_restore_evicted)
            _evicted.add(name)
            log.info("unloaded %s to make room for %s", name, model)
        except Exception as exc:
            log.warning("could not unload %s (%s)", name, exc)


def _restore_evicted() -> None:
    for name in _evicted:
        try:
            _ollama_post("/api/chat", {"model": name, "messages": [], "keep_alive": "24h"}, 180)
            log.info("reloaded %s", name)
        except Exception as exc:
            log.warning("could not reload %s (%s)", name, exc)


def _ollama_generate(prompt: str, num_predict: int = 1500) -> str:
    return workers_ai_or_local(prompt, num_predict, lambda: _ollama_local(prompt, num_predict), log)


def _ollama_local(prompt: str, num_predict: int) -> str:
    # lfm2.5 is also a hybrid reasoning model, but unlike qwen3:8b its
    # "think": false doesn't suppress reasoning -- it just dumps <think>
    # into the visible content instead. Left unset, Ollama correctly
    # separates reasoning into message.thinking and keeps content clean,
    # but the reasoning still consumes num_predict: the 400 budget tuned
    # for qwen3:8b's think:false path left content empty here (observed
    # 2026-07-28); 1500 was enough for all prompts on a real AAPL filing
    # (1.3-2.8k reasoning chars, still ~2x faster than the old
    # qwen3:8b/400 baseline).
    ollama_make_room(OLLAMA_MODEL)
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


INSIDER_WINDOW_DAYS = 183
CLUSTER_DAYS = 30
CLUSTER_MIN_INSIDERS = 3


def _xml_text(node, path: str) -> "str | None":
    el = node.find(path)
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


def _xml_float(node, path: str) -> "float | None":
    val = _xml_text(node, path)
    try:
        return float(val) if val is not None else None
    except ValueError:
        return None


def _parse_form4(xml_bytes: bytes) -> "tuple[str | None, list]":
    """(issuer CIK, rows) from one Form 4.

    Rows are non-derivative transactions plus holdings-only lines (code None,
    shares 0), each tagged with its ownership line so an insider's whole
    stake can be summed across direct and indirect vehicles.
    """
    root = ET.fromstring(xml_bytes)
    owners = []
    for ro in root.findall("reportingOwner"):
        name = _xml_text(ro, "reportingOwnerId/rptOwnerName") or "unknown"
        rel = ro.find("reportingOwnerRelationship")
        role = []
        if rel is not None:
            if _xml_text(rel, "isDirector") in ("true", "1"):
                role.append("Director")
            if _xml_text(rel, "isOfficer") in ("true", "1"):
                role.append(_xml_text(rel, "officerTitle") or "Officer")
            if _xml_text(rel, "isTenPercentOwner") in ("true", "1"):
                role.append("10% owner")
        owners.append((name, ", ".join(role) or "Other"))
    owner = "; ".join(n for n, _ in owners) or "unknown"
    role = "; ".join(r for _, r in owners) or "Other"
    footnotes = {fn.get("id"): "".join(fn.itertext()) for fn in root.findall("footnotes/footnote")}
    plan_footnotes = {k for k, v in footnotes.items() if re.search(r"10b5-?1", v, re.I)}
    filing_planned = _xml_text(root, "aff10b5One") in ("true", "1")
    period = _xml_text(root, "periodOfReport")

    def line(node):
        nature = node.find("ownershipNature/natureOfOwnership")
        nature_ids = sorted(f.get("id") for f in nature.iter("footnoteId")) if nature is not None else []
        return (_xml_text(node, "securityTitle/value"),
                _xml_text(node, "ownershipNature/directOrIndirectOwnership/value"),
                _xml_text(node, "ownershipNature/natureOfOwnership/value")
                or " ".join(footnotes.get(i, "") for i in nature_ids))

    rows = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        shares = _xml_float(tx, "transactionAmounts/transactionShares/value")
        if not shares:
            continue
        fn_ids = {f.get("id") for f in tx.iter("footnoteId")}
        rows.append({
            "owner": owner, "role": role, "line": line(tx),
            "date": _xml_text(tx, "transactionDate/value"),
            "code": _xml_text(tx, "transactionCoding/transactionCode"),
            "shares": shares,
            "price": _xml_float(tx, "transactionAmounts/transactionPricePerShare/value"),
            "after": _xml_float(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
            "planned": filing_planned or bool(fn_ids & plan_footnotes),
        })
    for h in root.findall("nonDerivativeTable/nonDerivativeHolding"):
        rows.append({
            "owner": owner, "role": role, "line": line(h), "date": period, "code": None,
            "shares": 0.0, "price": None, "planned": False,
            "after": _xml_float(h, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
        })
    return _xml_text(root, "issuer/issuerCik"), rows


def _insider_transactions(cik: str, subs: dict, since: dt.date) -> "tuple[list, int]":
    """Every Form 4 row dated on/after `since` where this company is the issuer.

    Uses the raw form4.xml, not the xslF345X06/ rendered page that
    primaryDocument points at. A company's submissions also list Form 4s it
    files as a *holder* of another issuer -- GOOGL's carried ~400 rows of GV
    selling a ~$19 portfolio stock -- so the issuer CIK must match. 4/A
    amendments are skipped: they restate an original that is already
    counted. Returns (rows oldest-filing-first, filings_read).
    """
    recent = subs.get("filings", {}).get("recent", {})
    rows, read = [], 0
    for i, form in enumerate(recent.get("form", [])):
        if form != "4" or recent["filingDate"][i] < since.isoformat():
            continue
        doc = recent["primaryDocument"][i].split("/")[-1]
        try:
            issuer, parsed = _parse_form4(_sec_get(_filing_url(cik, recent["accessionNumber"][i], doc)))
        except Exception as exc:
            log.warning("form 4 %s unreadable: %s", recent["accessionNumber"][i], exc)
            continue
        if not issuer or int(issuer) != int(cik):
            continue
        read += 1
        rows[:0] = [r for r in parsed if r["date"] and r["date"] >= since.isoformat()]
    return rows, read


def _clusters(rows: list) -> list:
    """Windows of CLUSTER_DAYS in which CLUSTER_MIN_INSIDERS+ distinct people traded."""
    rows = sorted(rows, key=lambda r: r["date"])
    found = []
    for i, start in enumerate(rows):
        end = dt.date.fromisoformat(start["date"]) + dt.timedelta(days=CLUSTER_DAYS)
        names = {r["owner"] for r in rows[i:] if dt.date.fromisoformat(r["date"]) <= end}
        if len(names) >= CLUSTER_MIN_INSIDERS:
            if not found or start["date"] > found[-1][1]:
                found.append((start["date"], end.isoformat(), len(names)))
    return found


def _insiders(rows: list) -> list:
    """Per-insider totals, with trades sized against the whole reported stake.

    Stake = sum of each ownership line's latest reported balance in the
    window. Per-line sizing read Zuckerberg emptying one holding entity as
    "sold 100%" while he held far more through others. Lines never traded or
    restated in the window are invisible, so the stake can still be
    understated (and % sold overstated) for insiders with many vehicles.
    """
    people = {}
    for r in sorted(rows, key=lambda r: r["date"]):
        p = people.setdefault(r["owner"], {"owner": r["owner"], "role": r["role"], "lines": {},
                                           "bought": 0.0, "sold": 0.0, "disc_sold": 0.0,
                                           "buy_value": 0.0, "sell_value": 0.0, "planned_value": 0.0})
        if r["after"] is not None:
            p["lines"][r["line"]] = r["after"]
        value = r["shares"] * r["price"] if r["price"] else 0.0
        if r["code"] == "P":
            p["bought"] += r["shares"]
            p["buy_value"] += value
        elif r["code"] == "S":
            p["sold"] += r["shares"]
            p["sell_value"] += value
            if r["planned"]:
                p["planned_value"] += value
            else:
                p["disc_sold"] += r["shares"]
    out = []
    for p in people.values():
        if not (p["bought"] or p["sold"]):
            continue
        start = sum(p["lines"].values()) + p["sold"] - p["bought"]
        p["pct_sold"] = p["sold"] / start * 100 if p["sold"] and start > 0 else None
        p["pct_disc_sold"] = p["disc_sold"] / start * 100 if p["disc_sold"] and start > 0 else None
        p["pct_bought"] = p["bought"] / start * 100 if p["bought"] and start > 0 else None
        out.append(p)
    return sorted(out, key=lambda p: -(p["buy_value"] + p["sell_value"]))


def _insider_summary(rows: list) -> dict:
    buys = [r for r in rows if r["code"] == "P"]
    sells = [r for r in rows if r["code"] == "S"]
    planned = [r for r in sells if r["planned"]]
    discretionary = [r for r in sells if not r["planned"]]
    people = _insiders(rows)

    def value(rs):
        return sum(r["shares"] * r["price"] for r in rs if r["price"])

    buy_clusters = _clusters(buys)
    disc_sellers = {r["owner"] for r in discretionary}
    big_disc = [p for p in people if (p["pct_disc_sold"] or 0) >= 25]
    if buy_clusters:
        verdict = ("Bullish", f"cluster buying: {buy_clusters[0][2]} insiders bought within "
                              f"{CLUSTER_DAYS} days of {buy_clusters[0][0]}")
    elif buys:
        verdict = ("Mildly bullish", f"{len({r['owner'] for r in buys})} insider(s) bought on the open market, "
                                     "but not as a cluster")
    elif big_disc:
        verdict = ("Bearish-leaning", f"{len(big_disc)} insider(s) sold 25%+ of their stake outside a 10b5-1 plan")
    elif len(disc_sellers) >= CLUSTER_MIN_INSIDERS:
        verdict = ("Bearish-leaning", f"{len(disc_sellers)} insiders sold outside a 10b5-1 plan")
    elif discretionary:
        verdict = ("Neutral", f"{_fmt_usd(value(discretionary))} sold outside 10b5-1 plans, but by "
                              f"{len(disc_sellers)} insider(s), none of them 25%+ of their stake; "
                              "no open-market buying")
    elif sells:
        verdict = ("Neutral", "all selling was pre-scheduled under 10b5-1 plans; no open-market buying")
    else:
        verdict = ("Neutral", "no open-market purchases or sales in the window")
    return {
        "buys": buys, "sells": sells, "planned": planned, "discretionary": discretionary,
        "people": people,
        "buy_value": value(buys), "sell_value": value(sells),
        "planned_value": value(planned), "discretionary_value": value(discretionary),
        "buy_sizes": [p["pct_bought"] for p in people if p["pct_bought"] is not None],
        "sell_sizes": [p["pct_sold"] for p in people if p["pct_sold"] is not None],
        "buy_clusters": buy_clusters, "sell_clusters": _clusters(sells),
        "other_codes": collections.Counter(r["code"] for r in rows if r["code"] not in ("P", "S", None)),
        "verdict": verdict,
    }


INSIDER_PROMPT = (
    "Act as an equity analyst. Below is a computed summary of {ticker}'s SEC "
    "Form 4 insider transactions over the past 6 months. The numbers and the "
    "verdict are final -- do not recompute, contradict or add to them. In 3-4 "
    "plain-English sentences, explain what the pattern suggests: whether "
    "insiders are net buyers or sellers, whether activity is clustered, how "
    "large it is against their stakes, and how much of the selling is "
    "pre-scheduled 10b5-1. Pre-scheduled 10b5-1 sales by executives are "
    "common and often mean little; sales outside a plan are the more "
    "informative kind, so never call those routine.\n\n{summary}"
)


def _section_insider_activity(ticker: str, cik: str, subs: dict) -> str:
    # "Insider Signal Reader" prompt. Every number here is parsed from Form 4
    # XML; the LLM only narrates the finished summary. Codes P and S are the
    # only open-market trades -- grants (A), tax withholding (F), option
    # exercises (M), conversions (C) and gifts (G) are counted but carry no
    # buy/sell signal.
    since = dt.date.today() - dt.timedelta(days=INSIDER_WINDOW_DAYS)
    rows, read = _insider_transactions(cik, subs, since)
    if not read:
        return f"<p>N/A -- no Form 4 filings for this issuer since {since.isoformat()}.</p>"
    s = _insider_summary(rows)

    def pct(x):
        return f"{x:.1f}%" if x is not None else "N/A"

    def med(xs):
        return pct(statistics.median(xs)) if xs else "N/A"

    table = _table(["Measure", "Open-market buys (P)", "Open-market sales (S)"], [
        ["Transactions", str(len(s["buys"])), str(len(s["sells"]))],
        ["Distinct insiders", str(len({r["owner"] for r in s["buys"]})), str(len({r["owner"] for r in s["sells"]}))],
        ["Value", _fmt_usd(s["buy_value"]), _fmt_usd(s["sell_value"])],
        ["Of which 10b5-1 planned", "--", f"{len(s['planned'])} trades, {_fmt_usd(s['planned_value'])}"],
        ["Discretionary", f"{len(s['buys'])} trades", f"{len(s['discretionary'])} trades, {_fmt_usd(s['discretionary_value'])}"],
        ["Median insider's trades vs. stake", med(s["buy_sizes"]), med(s["sell_sizes"])],
        ["Largest insider's trades vs. stake", pct(max(s["buy_sizes"])) if s["buy_sizes"] else "N/A",
         pct(max(s["sell_sizes"])) if s["sell_sizes"] else "N/A"],
        [f"Clusters ({CLUSTER_MIN_INSIDERS}+ insiders in {CLUSTER_DAYS} days)",
         str(len(s["buy_clusters"])), str(len(s["sell_clusters"]))],
    ])
    net = s["buy_value"] - s["sell_value"]
    other = ", ".join(f"{c} x{n}" for c, n in sorted(s["other_codes"].items())) or "none"
    label, reason = s["verdict"]
    color = {"Bullish": PALETTE["LEAN_BULLISH"], "Mildly bullish": PALETTE["LEAN_BULLISH"],
             "Bearish-leaning": PALETTE["LEAN_BEARISH"]}.get(label, PALETTE["LEAN_NEUTRAL"])
    people_rows = [[p["owner"], p["role"], _fmt_usd(p["buy_value"]) if p["bought"] else "--",
                    _fmt_usd(p["sell_value"]) if p["sold"] else "--",
                    _fmt_usd(p["planned_value"]) if p["sold"] else "--",
                    pct(p["pct_bought"]) if p["bought"] else "--", pct(p["pct_sold"]) if p["sold"] else "--"]
                   for p in s["people"][:8]]
    largest = "; ".join(
        f"{p['owner']} ({p['role']}) sold {_fmt_usd(p['sell_value'])} = {pct(p['pct_sold'])} of stake, "
        f"{_fmt_usd(p['planned_value'])} of it 10b5-1" if p["sold"] else
        f"{p['owner']} ({p['role']}) bought {_fmt_usd(p['buy_value'])} = +{pct(p['pct_bought'])}"
        for p in s["people"][:3]) or "none"

    summary = (
        f"Window: {since.isoformat()} to today, {read} Form 4 filings.\n"
        f"Open-market buys: {len(s['buys'])} by {len({r['owner'] for r in s['buys']})} insiders, {_fmt_usd(s['buy_value'])}.\n"
        f"Open-market sales: {len(s['sells'])} by {len({r['owner'] for r in s['sells']})} insiders, {_fmt_usd(s['sell_value'])} "
        f"({len(s['planned'])} under 10b5-1 plans, {_fmt_usd(s['planned_value'])}).\n"
        f"Net open-market flow: {_fmt_usd(net)}.\n"
        f"Median seller sold {med(s['sell_sizes'])} of their stake; median buyer added {med(s['buy_sizes'])}.\n"
        f"Buy clusters: {len(s['buy_clusters'])}; sell clusters: {len(s['sell_clusters'])}.\n"
        f"Largest insiders by value: {largest}.\n"
        f"Non-trade entries (grants/withholding/exercises/conversions/gifts): {other}.\n"
        f"Verdict: {label} -- {reason}."
    )
    try:
        narrative, _ = _llm_html(_ollama_generate(INSIDER_PROMPT.format(ticker=ticker, summary=summary), 400))
    except Exception as exc:
        log.warning("%s: insider narration failed: %s", ticker, exc)
        narrative = f"<p>N/A -- narration failed ({_esc(exc)}); the tables above are complete.</p>"

    return (
        f"<p>{_pill(label, color)} {_esc(reason)}. Net open-market flow {_esc(_fmt_usd(net))}.</p>"
        + table
        + (("<p class=\"mt-3 text-sm font-semibold\">By insider (largest first)</p>"
            + _table(["Insider", "Role", "Bought", "Sold", "Sold under 10b5-1", "Added to stake", "Sold of stake"],
                     people_rows))
           if people_rows else "")
        + f"<p class=\"text-sm text-slate-600\">Non-trade entries: {_esc(other)} (A grant, F tax withholding, "
          "M option exercise, C conversion, G gift). Stake is the sum of each ownership line the insider reported "
          "in the window, so vehicles never reported here are missing and % of stake can read high.</p>"
        + narrative
        + _cite(f"SEC Form 4, {read} filings since {since.isoformat()}")
    )


_COMP_TERMS = re.compile(
    r"base salary|annual (cash )?incentive|long[- ]term incentive|equity award|"
    r"restricted stock|performance[- ]based|RSU|target bonus|payout|vesting|"
    r"total shareholder return",
    re.I,
)


def _extract_compensation_text(proxy_text: str, window: int = 6000) -> "str | None":
    # _extract_section's largest-gap rule de-TOCs a 10-K well, but not a
    # proxy: the CD&A heading also appears in the table of contents and in
    # cross-references ("see Compensation Discussion and Analysis beginning
    # on page 38"), and on AAPL/AON the gap rule picked those over the real
    # section. Score each candidate window by how densely it uses actual
    # compensation vocabulary instead -- a TOC line and a cross-reference
    # both score near zero. The window is not guaranteed to be the CD&A
    # narrative proper (on some filers it lands on the award tables), which
    # is why INCENTIVES_PROMPT is written to answer only from what the
    # excerpt supports and to say so when it does not.
    best_score, best_text = 0, None
    for m in re.finditer(r"Compensation\s*Discussion\s*and\s*Analysis", proxy_text, re.I):
        chunk = proxy_text[m.start():m.start() + window]
        score = len(_COMP_TERMS.findall(chunk))
        if score > best_score:
            best_score, best_text = score, chunk
    if best_score >= 3:
        return best_text
    # No usable CD&A heading -- fall back to the densest incentive language
    # anywhere in the proxy.
    return _keyword_window(proxy_text, r"performance[- ]based|annual\s*incentive|long[- ]term\s*incentive")


def _section_management_incentives(ticker: str, proxy_text: "str | None",
                                   proxy_accn: "str | None", proxy_filed: "str | None") -> str:
    # Prompt 47. Grounded entirely in the DEF 14A that _section_ownership
    # already fetches -- no extra network call. Proxy-only by design: the
    # 10-K does not carry a Compensation Discussion & Analysis, and guessing
    # a pay structure from anywhere else would be inventing it.
    if not proxy_text:
        return ("<p>N/A -- no DEF 14A proxy statement available for this filer, and "
                "executive compensation structure is only disclosed there.</p>")
    excerpt = _extract_compensation_text(proxy_text)
    if not excerpt:
        return ("<p>N/A -- could not locate a Compensation Discussion &amp; Analysis section "
                "in the proxy text.</p>" + _cite(f"proxy (DEF 14A), accession {proxy_accn}, filed {proxy_filed}"))
    try:
        reply = _ollama_generate(INCENTIVES_PROMPT.format(
            ticker=ticker, accn=proxy_accn, filed=proxy_filed, excerpt=excerpt[:6000]))
    except Exception as exc:
        log.warning("%s: incentives analysis failed: %s", ticker, exc)
        return f"<p>N/A -- local LLM analysis failed ({_esc(exc)}).</p>"
    html, _ = _llm_html(reply)
    return html + _cite(f"proxy (DEF 14A), accession {proxy_accn}, filed {proxy_filed}")


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
    "Insider Activity (6 months)",
    "Management Incentives",
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
    sec7 = _section_capital_allocation(ocf, capex, repurchases, dividends, acquisitions,
                                       market_cap, accn, filed, facts, current_price, shares_out)
    sec8 = _section_share_count_trend(shares)
    sec9, ownership_flag = _section_ownership(ticker, tenk_text, accn, filed, proxy_text, proxy_accn, proxy_filed)
    sec9a = _section_insider_activity(ticker, cik, subs)
    sec9b = _section_management_incentives(ticker, proxy_text, proxy_accn, proxy_filed)
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
        sec1, sec2, sec3, sec4, sec5, sec6, sec7, sec8, sec9, sec9a, sec9b,
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
