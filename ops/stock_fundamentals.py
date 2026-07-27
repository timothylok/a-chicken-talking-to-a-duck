"""Category 1 Fundamental Snapshot reports (SEC EDGAR-sourced), one markdown
file per ticker in content/stock-fundamentals/.

Run on demand: python ops/stock_fundamentals.py [--tickers AAPL,MSFT,...]
Defaults to STOCK_WATCHLIST (same env var as stock_daily.py) minus SPCX,
which is excluded here: it's the SpaceX-linked ticker, not a traditional
SEC reporting company, so it has no 10-K/proxy filings to pull from.

Pipeline per ticker:
  1. SEC EDGAR company_tickers.json -> CIK
  2. data.sec.gov submissions API -> latest 10-K + DEF 14A accession/doc
  3. data.sec.gov companyfacts API -> XBRL numeric data, filtered to the
     exact periods reported in that one latest 10-K filing (prompts 2, 4,
     5, 7 are fully computed from these numbers -- no LLM, no hallucination
     risk on the figures themselves)
  4. Fetch + strip the 10-K/proxy HTML, extract the relevant section by
     heading, and have local Ollama synthesize prompts 1, 3, 6 (business
     description, revenue concentration, insider ownership/dual-class)
     grounded only in that excerpt

Not scheduled: 10-K/proxy data changes ~annually (unlike stock_daily.py's
daily prices), so this is meant to be rerun manually after a new filing.

Known limitations: XBRL tag names vary by company -- a metric reports
"N/A" if none of the candidate tags are found, rather than guessing.
Section extraction is heading-regex/keyword based and best-effort; when a
section can't be located, the report says so instead of asking the LLM to
fill the gap.
"""

import argparse
import html as html_lib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT, "content", "stock-fundamentals")
LOG_PATH = os.path.join(ROOT, "asr", "logs", "stock_fundamentals.log")
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

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("stock_fundamentals")

DEFAULT_WATCHLIST = os.environ.get(
    "STOCK_WATCHLIST", "AAPL,MSFT,NVDA,TSLA,GOOGL,AMZN,META,SPCX"
)
EXCLUDE_NO_SEC_FILINGS = {"SPCX"}

# Candidate XBRL us-gaap tags, tried in order, per concept -- companies use
# different (sometimes custom) tags for the same line item.
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


# ---------------------------------------------------------------------------
# XBRL numeric extraction (deterministic -- prompts 2, 4, 5, 7)
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


def _section2_key_financials(rev, ni, fcf, accn: str, filed: str) -> str:
    lines = [f"**Latest fiscal year** (10-K, accession {accn}, filed {filed}):", ""]
    for label, series in (("Revenue", rev), ("Net income", ni), ("Free cash flow", fcf)):
        if not series:
            lines.append(f"- {label}: N/A (no matching XBRL tag found in this filing)")
            continue
        chg = _yoy(series)
        chg_str = f"{chg:+.1f}% YoY" if chg is not None else "YoY n/a (prior year not in this filing)"
        lines.append(f"- {label}: {_fmt_usd(series[-1]['val'])} ({chg_str})")
    lines.append("")
    lines.append(f"*Source: SEC EDGAR XBRL company facts, 10-K accession {accn}*")
    return "\n".join(lines)


def _section4_margins(rev, gp, oi, ni) -> str:
    if not rev:
        return "N/A -- revenue tag not found, cannot compute margins."
    ends = sorted({i["end"] for i in rev})[-3:]
    rev_by, gp_by, oi_by, ni_by = (
        {i["end"]: i["val"] for i in s} for s in (rev, gp, oi, ni)
    )
    rows = []
    for end in ends:
        r = rev_by.get(end)
        if not r:
            continue
        rows.append({
            "end": end,
            "gross": (gp_by[end] / r * 100) if end in gp_by else None,
            "operating": (oi_by[end] / r * 100) if end in oi_by else None,
            "net": (ni_by[end] / r * 100) if end in ni_by else None,
        })

    def f(x):
        return f"{x:.1f}%" if x is not None else "N/A"

    lines = [
        "| Fiscal Year End | Gross Margin | Operating Margin | Net Margin |",
        "|---|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| {row['end']} | {f(row['gross'])} | {f(row['operating'])} | {f(row['net'])} |")
    if len(rows) >= 2:
        first, last = rows[0], rows[-1]
        deltas = {
            k: (last[k] - first[k]) * 100
            for k in ("gross", "operating", "net")
            if first[k] is not None and last[k] is not None
        }
        if deltas:
            biggest = max(deltas, key=lambda k: abs(deltas[k]))
            direction = "expanded" if deltas[biggest] > 0 else "compressed"
            lines.append(
                f"\nBiggest driver: {biggest} margin {direction} "
                f"{abs(deltas[biggest]):.0f} bps from {first['end']} to {last['end']} "
                "-- the largest swing of the three."
            )
    return "\n".join(lines)


def _section5_capital_allocation(ocf, capex, repurchases, dividends, acquisitions) -> str:
    def latest(series):
        return series[-1]["val"] if series else None

    o, c, r, d, a = (latest(s) for s in (ocf, capex, repurchases, dividends, acquisitions))
    lines = ["**Latest fiscal year capital allocation** (source: SEC EDGAR 10-K XBRL):", ""]
    lines.append(f"- Operating cash flow: {_fmt_usd(o)}")
    lines.append(f"- Capex: {_fmt_usd(c)}")
    lines.append(f"- Share repurchases: {_fmt_usd(r)}" + ("" if r is not None else " (tag absent -- may mean none, or a different tag)"))
    lines.append(f"- Dividends paid: {_fmt_usd(d)}" + ("" if d is not None else " (tag absent -- may mean none paid)"))
    lines.append(f"- Acquisitions, net of cash acquired: {_fmt_usd(a)}")
    returned = sum(x for x in (r, d) if x)
    reinvested = sum(x for x in (c, a) if x)
    if returned or reinvested:
        verdict = (
            "returning more capital to shareholders than it is reinvesting"
            if returned > reinvested
            else "reinvesting more into the business than it is returning to shareholders"
        )
        lines.append(f"\nOn these figures, the company is {verdict}: "
                      f"{_fmt_usd(returned)} returned vs {_fmt_usd(reinvested)} reinvested.")
    return "\n".join(lines)


def _section7_table(rev, gp, oi, ni, fcf, shares) -> str:
    if not rev:
        return "N/A -- revenue tag not found, cannot build comparable table."
    ends = sorted({i["end"] for i in rev})[-3:]
    maps = {
        "Revenue": {i["end"]: i["val"] for i in rev},
        "Gross Profit": {i["end"]: i["val"] for i in gp},
        "Operating Income": {i["end"]: i["val"] for i in oi},
        "Net Income": {i["end"]: i["val"] for i in ni},
        "Free Cash Flow": {i["end"]: i["val"] for i in fcf},
        "Shares Outstanding": {i["end"]: i["val"] for i in shares},
    }
    lines = ["| Metric | " + " | ".join(ends) + " |", "|---|" + "---|" * len(ends)]
    for label, m in maps.items():
        cells = []
        for e in ends:
            if e not in m:
                cells.append("N/A")
            elif label == "Shares Outstanding":
                cells.append(f"{m[e]:,.0f}")
            else:
                cells.append(_fmt_usd(m[e]))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM-grounded narrative sections (prompts 1, 3, 6)
# ---------------------------------------------------------------------------

BUSINESS_PROMPT = (
    "Act as a senior equity analyst. Below is the 'Item 1. Business' "
    "section from {ticker}'s most recent 10-K filed with the SEC (EDGAR "
    "accession {accn}, filed {filed}). Using ONLY the text given, "
    "summarize in exactly 5 sentences what the company sells, to whom, "
    "and how it makes money. Do not invent facts not present in the "
    "excerpt. Under 150 words.\n\n---\n{excerpt}\n---"
)

CONCENTRATION_PROMPT = (
    "Below is an excerpt from {ticker}'s most recent 10-K (EDGAR accession "
    "{accn}, filed {filed}). Using ONLY this text, identify any disclosed "
    "revenue concentration: a single customer's percentage of revenue, "
    "top-3 customers combined, or geographic concentration. If a single "
    "customer is over 20% of revenue, start your answer with 'FLAG:'. If "
    "no concentration is disclosed in this excerpt, say so plainly -- do "
    "not guess or infer beyond the text. Under 150 words.\n\n---\n{excerpt}\n---"
)

OWNERSHIP_PROMPT = (
    "Below is an excerpt from {ticker}'s SEC filings (EDGAR accession "
    "{accn}, filed {filed}). Using ONLY this text, report: (1) whether the "
    "company has a dual-class or multi-class share structure with unequal "
    "voting power, and (2) any insider/officer/director ownership "
    "percentage disclosed. If public shareholders hold under 50% of "
    "voting power, start with 'FLAG:'. If the excerpt doesn't disclose "
    "this, say so plainly -- do not guess. Under 150 words.\n\n---\n{excerpt}\n---"
)


def _ollama_generate(prompt: str, num_predict: int = 1500) -> str:
    # lfm2.5 is also a hybrid reasoning model, but unlike qwen3:8b its
    # "think": false doesn't suppress reasoning -- it just dumps <think>
    # into the visible content instead. Left unset, Ollama correctly
    # separates reasoning into message.thinking and keeps content clean,
    # but the reasoning still consumes num_predict: the 400 budget tuned
    # for qwen3:8b's think:false path left content empty here (observed
    # 2026-07-28); 1500 was enough for all three prompts on a real AAPL
    # filing (1.3-2.8k reasoning chars, still ~2x faster than the old
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


def _section1_business(ticker: str, tenk_text: str, accn: str, filed: str) -> str:
    excerpt = _extract_section(tenk_text, r"Item\s*1\.?\s*Business\b", r"Item\s*1A\.?\s*Risk\s*Factors\b")
    if not excerpt:
        return "N/A -- could not locate the 'Item 1. Business' section in the filing text."
    try:
        reply = _ollama_generate(BUSINESS_PROMPT.format(ticker=ticker, accn=accn, filed=filed, excerpt=excerpt[:6000]))
    except Exception as exc:
        log.warning("%s: business summary failed: %s", ticker, exc)
        return f"N/A -- local LLM summary failed ({exc}). Raw section available in the 10-K."
    return reply + f"\n\n*Source: 10-K Item 1, accession {accn}, filed {filed}*"


def _section3_concentration(ticker: str, tenk_text: str, accn: str, filed: str) -> str:
    biz = _extract_section(tenk_text, r"Item\s*1\.?\s*Business\b", r"Item\s*1A\.?\s*Risk\s*Factors\b", max_len=4000) or ""
    risk = _extract_section(tenk_text, r"Item\s*1A\.?\s*Risk\s*Factors\b", r"Item\s*1B\.?\s*Unresolved", max_len=4000) or ""
    excerpt = (biz + "\n\n" + risk).strip()
    if not excerpt:
        return "N/A -- could not locate Business/Risk Factors sections in the filing text."
    try:
        reply = _ollama_generate(CONCENTRATION_PROMPT.format(ticker=ticker, accn=accn, filed=filed, excerpt=excerpt[:6000]))
    except Exception as exc:
        log.warning("%s: concentration check failed: %s", ticker, exc)
        return f"N/A -- local LLM analysis failed ({exc})."
    return reply + f"\n\n*Source: 10-K Item 1 / Item 1A, accession {accn}, filed {filed}*"


def _section6_ownership(ticker: str, tenk_text: str, tenk_accn: str, tenk_filed: str,
                         proxy_text: "str | None", proxy_accn: "str | None", proxy_filed: "str | None") -> str:
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
        return f"N/A -- local LLM analysis failed ({exc})."
    return reply + f"\n\n*Source: {'proxy (DEF 14A)' if proxy_text and cite_accn == proxy_accn else '10-K'}, accession {cite_accn}, filed {cite_filed}*"


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
    accn = tenk["accn"]
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
    fcf = _fcf_series(ocf, capex)

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

    sec1 = _section1_business(ticker, tenk_text, accn, tenk["filed"]) if tenk_text else "N/A -- could not fetch 10-K text."
    sec2 = _section2_key_financials(rev, ni, fcf, accn, tenk["filed"])
    sec3 = _section3_concentration(ticker, tenk_text, accn, tenk["filed"]) if tenk_text else "N/A -- could not fetch 10-K text."
    sec4 = _section4_margins(rev, gp, oi, ni)
    sec5 = _section5_capital_allocation(ocf, capex, repurchases, dividends, acquisitions)
    sec6 = _section6_ownership(ticker, tenk_text, accn, tenk["filed"], proxy_text, proxy_accn, proxy_filed)
    sec7 = _section7_table(rev, gp, oi, ni, fcf, shares)

    report = f"""# {ticker} -- Category 1 Fundamental Snapshot

*Source: SEC EDGAR, CIK {cik}. Latest 10-K: accession {accn}, filed {tenk['filed']} ({tenk_url}).*

## 1. Business in Plain English

{sec1}

## 2. Three Key Financial Numbers (Latest Fiscal Year)

{sec2}

## 3. Revenue Concentration Check

{sec3}

## 4. Margin Trajectory

{sec4}

## 5. Capital Allocation

{sec5}

## 6. Insider Ownership and Dual-Class Structure

{sec6}

## 7. Three-Year Comparable Financials

{sec7}
"""
    return report


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
        try:
            report = build_report(ticker)
        except Exception:
            log.exception("%s: report generation failed", ticker)
            continue
        if not report:
            continue
        out_path = os.path.join(OUTPUT_DIR, f"{ticker}.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report)
        log.info("wrote %s", out_path)
        written += 1
    log.info("done: %d/%d tickers", written, len(tickers))
    print(f"wrote {written}/{len(tickers)} reports to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
