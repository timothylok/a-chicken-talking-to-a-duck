"""Category 5: risk factors and red flags -- top risks de-jargoned,
off-balance-sheet liabilities, goodwill impairment risk, receivables/
inventory trends vs revenue, debt maturity schedule, auditor changes and
going-concern flags. Generated reactively on each watchlist ticker's
newest 10-K, saved to Notion.

Runs daily via the "VoiceOS Risk Watch" scheduled task, 2:00 AM NZT
(staggered after "VoiceOS Earnings Watch" 1:00 AM / "VoiceOS Valuation
Watch" 1:30 AM). Trigger is a NEW mechanism, not reused from
stock_earnings.py/stock_valuation.py: those react to a quarterly 8-K
(item 2.02); this reacts to a new ANNUAL 10-K filing instead
(sf._latest_filing(subs, "10-K")), since every prompt below reads from
the 10-K, not a quarterly earnings release. Own state file
(asr/logs/risk_watch_state.json), same accession-based-dedupe shape as
the other two watch scripts, but deliberately NO 30-day staleness guard:
that guard exists so a first-ever run doesn't fire on already-old
quarterly "news" -- a 10-K's risk factors aren't time-sensitive news the
same way, so the very first run for any ticker always generates a report
for whatever its latest 10-K currently is.

Minimizes LLM usage on purpose, more than Category 1-4: this category
exists specifically to surface hard facts a press release/summary might
omit, so every prompt expressible as a direct calculation stays 100%
deterministic Python (DSO trend, inventory/revenue trend, debt maturity,
goodwill-to-equity ratio) -- no risk of an LLM softening or missing a red
flag. LLM calls (qwen3:8b, think:false, same reasoning as
stock_earnings.py/stock_valuation.py) are reserved for genuine
translation/synthesis tasks: de-jargoning Item 1A's legal language, and
synthesizing scattered footnote mentions of off-balance-sheet items /
goodwill-impairment assumptions. The going-concern and auditor-change
section shows raw cited filing excerpts rather than an LLM paraphrase --
a going-concern flag must never be softened or missed by a summarizer.

New XBRL tag lists (verified present for AAPL/MSFT/TSLA/GOOGL
2026-07-28 against real SEC companyfacts JSON; not yet verified for
correctness across the full watchlist -- same live-testing discipline
that caught real bugs in stock_earnings.py/stock_valuation.py):
AccountsReceivableNetCurrent, InventoryNet, Goodwill, and the 6-bucket
LongTermDebtMaturitiesRepaymentsOfPrincipal... family. Point-in-time
balance-sheet tags like these only yield ~2 fiscal years out of
sf._xbrl_series (which scopes to one accession's face-of-balance-sheet
columns) -- see _xbrl_multi_year below for how this file gets a real
3-year trend instead.

Setup:
  1. Requires ops/notion.json already configured with api_key +
     database_id (see notion_sync.py's own --setup).
  2. python ops/stock_risk_flags.py --setup
     (creates the "股票風險與紅旗 Risk & Red Flags" database under the
     same parent page as the existing Voice History database, adds
     risk_database_id to the config)

Until risk_database_id exists in the config, scheduled runs are silent
no-ops (--tickers still prints/writes if configured).
"""

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_fundamentals as sf  # noqa: E402

# Not sf.OLLAMA_MODEL (lfm2.5) -- same reasoning as stock_earnings.py/
# stock_valuation.py: structured judgment/translation the user may act on.
RISK_MODEL = "qwen3:8b"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "asr", "logs", "stock_risk_flags.log")
CONFIG = os.path.join(ROOT, "ops", "notion.json")
WATCH_STATE = os.path.join(ROOT, "asr", "logs", "risk_watch_state.json")
NOTION_VERSION = "2022-06-28"

WATCHLIST = [
    t.strip().upper()
    for t in os.environ.get("STOCK_WATCHLIST", "AAPL,MSFT,NVDA,TSLA,GOOGL,AMZN,META,SPCX").split(",")
    if t.strip()
]

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
# logging.basicConfig() would be a silent no-op here -- importing
# stock_fundamentals above already configured the root logger via its own
# basicConfig() call (same gotcha every other Category script hit).
log = logging.getLogger("stock_risk_flags")
log.propagate = False
log.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)


# ---------------------------------------------------------------------------
# New XBRL tag lists + multi-year helper
# ---------------------------------------------------------------------------

ACCOUNTS_RECEIVABLE_TAGS = ["AccountsReceivableNetCurrent"]
INVENTORY_TAGS = ["InventoryNet"]
GOODWILL_TAGS = ["Goodwill"]
DEBT_MATURITY_TAGS = {
    "y1": ["LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths"],
    "y2": ["LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo"],
    "y3": ["LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree"],
    "y4": ["LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour"],
    "y5": ["LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive"],
    "thereafter": ["LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive"],
}


def _xbrl_multi_year(facts: dict, tags: list, unit: str = "USD", n: int = 3) -> "tuple[str | None, list]":
    # Point-in-time balance-sheet facts (AR/Inventory/Goodwill) only give
    # ~2 fiscal years out of sf._xbrl_series (scoped to one accession's
    # face-of-balance-sheet columns). Pulls the tag's full historical fact
    # array across ALL filings instead, restricted to form=="10-K" (skip
    # 10-Q quarter-ends -- these prompts want fiscal years), dedupes by
    # "end" keeping the most-recently-filed value (picks up restatements),
    # sorted ascending, returns the last n.
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
            existing = by_end.get(item["end"])
            if not existing or item.get("filed", "") >= existing.get("filed", ""):
                by_end[item["end"]] = item
        if not by_end:
            continue
        return tag, sorted(by_end.values(), key=lambda i: i["end"])[-n:]
    return None, []


# ---------------------------------------------------------------------------
# LLM narrative -- "compute first, narrate second," minimal LLM footprint
# (see module docstring for why).
# ---------------------------------------------------------------------------

def _generate(prompt: str, num_predict: int = 500) -> str:
    payload = json.dumps({
        "model": RISK_MODEL,
        "think": False,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_ctx": 8192, "num_predict": num_predict},
    }).encode()
    req = urllib.request.Request(
        f"{sf.OLLAMA_URL}/api/chat", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        reply = json.loads(resp.read()).get("message", {}).get("content", "").strip()
    if not reply:
        raise RuntimeError("model returned empty content")
    return reply


RISKS_PROMPT = (
    "Act as a forensic equity analyst. Below is the 'Item 1A. Risk Factors' "
    "section from {ticker}'s 10-K (SEC EDGAR accession {accn}, filed {filed}). "
    "Identify the top 3 risk factors. For each: translate the legal/"
    "boilerplate language into one or two plain-English sentences, then add "
    "a one-line framing of the potential dollar/financial impact if the risk "
    "materializes. Format as a numbered list of exactly 3 items, nothing "
    "else. Under 250 words total.\n\n---\n{excerpt}\n---"
)

OFF_BALANCE_SHEET_PROMPT = (
    "Act as a forensic equity analyst reviewing {ticker}'s 10-K footnotes "
    "(SEC EDGAR accession {accn}, filed {filed}). Below are excerpts that may "
    "mention operating leases, pension/retirement obligations, or contingent "
    "liabilities/commitments not fully reflected on the balance sheet. "
    "Identify any such items, quantify each in dollars where a figure is "
    "given, and note which excerpt it came from. If an excerpt doesn't "
    "actually disclose a quantifiable off-balance-sheet item, say so -- do "
    "not invent a number. Under 250 words.\n\n---\n{excerpt}\n---"
)

GOODWILL_PROMPT = (
    "Act as a forensic equity analyst. Below is goodwill-impairment-testing "
    "footnote language from {ticker}'s 10-K (SEC EDGAR accession {accn}, "
    "filed {filed}). Identify the discount rate and revenue/cash-flow growth "
    "rate assumptions used in the impairment test, if stated. Comment on "
    "whether those assumptions look aggressive (e.g. optimistic growth, low "
    "discount rate) given typical market conditions. If the excerpt doesn't "
    "state specific assumptions, say so -- do not invent numbers. Under 200 "
    "words.\n\n---\n{excerpt}\n---"
)

_OFF_BALANCE_SHEET_KEYWORDS = [
    r"operating\s+lease",
    r"(defined\s+benefit|pension)\s+(plan|obligation)",
    r"(contingent\s+liabilit|commitments?\s+and\s+contingenc)",
]


def _section_top_risks(ticker: str, tenk_text: str, accn: str, filed: str) -> str:
    excerpt = sf._extract_section(
        tenk_text, r"Item\s*1A\.?\s*Risk\s*Factors\b", r"Item\s*1B\.?\s*Unresolved", max_len=6000)
    if not excerpt:
        return "N/A -- could not locate the 'Item 1A. Risk Factors' section in the filing text."
    try:
        return _generate(RISKS_PROMPT.format(ticker=ticker, accn=accn, filed=filed, excerpt=excerpt[:6000]))
    except Exception as exc:
        log.warning("%s: top-risks generation failed: %s", ticker, exc)
        return f"N/A -- local LLM analysis failed ({exc})."


def _section_off_balance_sheet(ticker: str, tenk_text: str, accn: str, filed: str) -> str:
    windows = []
    for pat in _OFF_BALANCE_SHEET_KEYWORDS:
        w = sf._keyword_window(tenk_text, pat, before=300, after=2500)
        if w:
            windows.append(w)
    if not windows:
        return "N/A -- could not locate lease/pension/contingent-liability footnote language in the filing text."
    excerpt = "\n---\n".join(windows)[:7000]
    try:
        return _generate(OFF_BALANCE_SHEET_PROMPT.format(ticker=ticker, accn=accn, filed=filed, excerpt=excerpt))
    except Exception as exc:
        log.warning("%s: off-balance-sheet generation failed: %s", ticker, exc)
        return f"N/A -- local LLM analysis failed ({exc})."


def _section_goodwill(ticker: str, facts: dict, accn: str, tenk_text: str, filed: str) -> dict:
    _, goodwill_series = _xbrl_multi_year(facts, GOODWILL_TAGS, n=1)
    _, equity_series = sf._xbrl_series(facts, sf.STOCKHOLDERS_EQUITY_TAGS, accn)
    goodwill = goodwill_series[-1]["val"] if goodwill_series else None
    equity = equity_series[-1]["val"] if equity_series else None
    pct = (goodwill / equity * 100) if (goodwill is not None and equity) else None
    flagged = pct is not None and pct > 30

    excerpt = sf._keyword_window(tenk_text, r"goodwill\s+impairment", before=300, after=3000)
    if not excerpt:
        commentary = "N/A -- could not locate goodwill-impairment footnote language in the filing text."
    else:
        try:
            commentary = _generate(GOODWILL_PROMPT.format(ticker=ticker, accn=accn, filed=filed, excerpt=excerpt[:5000]))
        except Exception as exc:
            log.warning("%s: goodwill commentary generation failed: %s", ticker, exc)
            commentary = f"N/A -- local LLM analysis failed ({exc})."
    return {"goodwill": goodwill, "equity": equity, "pct_of_equity": pct, "flagged": flagged, "commentary": commentary}


# ---------------------------------------------------------------------------
# Sections 4/5: receivables/inventory vs revenue trends -- 100% deterministic
# ---------------------------------------------------------------------------

def _section_dso_trend(facts: dict, accn: str) -> dict:
    _, ar_series = _xbrl_multi_year(facts, ACCOUNTS_RECEIVABLE_TAGS, n=3)
    _, rev_series = sf._xbrl_series(facts, sf.REVENUE_TAGS, accn)
    rev_by_end = {i["end"]: i["val"] for i in rev_series}
    rows = []
    for ar in ar_series:
        rev = rev_by_end.get(ar["end"])
        dso = (ar["val"] / rev * 365) if rev else None
        rows.append({"end": ar["end"], "ar": ar["val"], "revenue": rev, "dso": dso})
    dsos = [r["dso"] for r in rows if r["dso"] is not None]
    rising = (dsos[-1] > dsos[0]) if len(dsos) >= 2 else None
    return {"rows": rows, "rising": rising}


def _section_inventory_trend(facts: dict, accn: str) -> dict:
    _, inv_series = _xbrl_multi_year(facts, INVENTORY_TAGS, n=3)
    _, rev_series = sf._xbrl_series(facts, sf.REVENUE_TAGS, accn)
    rev_by_end = {i["end"]: i["val"] for i in rev_series}
    rows = []
    for inv in inv_series:
        rev = rev_by_end.get(inv["end"])
        pct = (inv["val"] / rev * 100) if rev else None
        rows.append({"end": inv["end"], "inventory": inv["val"], "revenue": rev, "pct_of_revenue": pct})
    pcts = [r["pct_of_revenue"] for r in rows if r["pct_of_revenue"] is not None]
    rising = (pcts[-1] > pcts[0]) if len(pcts) >= 2 else None
    note = None
    if rising is True:
        note = "Inventory is growing faster than revenue -- may signal slowing demand, overstocking, or obsolescence risk."
    elif rising is False:
        note = "Inventory is not outpacing revenue growth over this window."
    return {"rows": rows, "rising": rising, "note": note}


def _dso_table(rows: list) -> str:
    if not rows:
        return "N/A -- insufficient multi-year Accounts Receivable/Revenue XBRL data."
    lines = []
    for r in rows:
        if r["dso"] is not None:
            lines.append(f"{r['end']}: AR ${r['ar']:,.0f} / Revenue ${r['revenue']:,.0f} => DSO {r['dso']:.1f} days")
        else:
            lines.append(f"{r['end']}: AR ${r['ar']:,.0f}, revenue unavailable for this period")
    return "\n".join(lines)


def _inventory_table(rows: list, note: "str | None") -> str:
    if not rows:
        return "N/A -- insufficient multi-year Inventory/Revenue XBRL data."
    lines = []
    for r in rows:
        if r["pct_of_revenue"] is not None:
            lines.append(f"{r['end']}: Inventory ${r['inventory']:,.0f} / Revenue ${r['revenue']:,.0f} => {r['pct_of_revenue']:.1f}% of revenue")
        else:
            lines.append(f"{r['end']}: Inventory ${r['inventory']:,.0f}, revenue unavailable for this period")
    text = "\n".join(lines)
    return text + (f"\n\n{note}" if note else "")


# ---------------------------------------------------------------------------
# Section 6: debt maturity schedule -- 100% deterministic
# ---------------------------------------------------------------------------

def _section_debt_maturity(facts: dict, accn: str) -> dict:
    buckets = {}
    for key, tags in DEBT_MATURITY_TAGS.items():
        _, series = sf._xbrl_series(facts, tags, accn)
        buckets[key] = series[-1]["val"] if series else None
    if not any(v is not None for v in buckets.values()):
        return {
            "buckets": buckets, "due_24mo": None, "flagged": None,
            "basis": "N/A -- this filing doesn't tag a granular debt-maturity schedule (not all filers break it out).",
        }
    due_24mo = (buckets["y1"] or 0) + (buckets["y2"] or 0)
    cash, sti = sf._cash_and_sti(facts, accn)
    cash_sti = (cash or 0) + (sti or 0)
    _, ocf_series = sf._xbrl_series(facts, sf.OCF_TAGS, accn)
    ocf = ocf_series[-1]["val"] if ocf_series else None
    coverage = cash_sti + (ocf or 0)
    flagged = due_24mo > coverage
    basis = (
        f"${due_24mo:,.0f} due within 24 months vs ${coverage:,.0f} available "
        f"(cash + short-term investments + latest annual operating cash flow)."
    )
    return {"buckets": buckets, "due_24mo": due_24mo, "flagged": flagged, "basis": basis}


# ---------------------------------------------------------------------------
# Section 7: auditor changes & going-concern -- raw cited excerpts, no LLM
# paraphrase (a going-concern flag must never be softened or missed).
# ---------------------------------------------------------------------------

def _section_auditor_going_concern(tenk_text: str, accn: str, filed: str) -> dict:
    gc_excerpt = sf._keyword_window(tenk_text, r"substantial\s+doubt.{0,80}going\s+concern", before=400, after=1200)
    going_concern = gc_excerpt.strip() if gc_excerpt else (
        f"No going-concern language found in the 10-K (accession {accn}, filed {filed}).")

    auditor_excerpt = sf._extract_section(
        tenk_text, r"Item\s*9\.?\s*Changes\s+in\s+and\s+Disagreements",
        r"Item\s*9A\.?\s*Controls\s+and\s+Procedures", max_len=1500)
    auditor_changes = auditor_excerpt.strip() if auditor_excerpt else (
        f"Could not locate Item 9 in the 10-K (accession {accn}, filed {filed}).")
    return {"going_concern": going_concern, "auditor_changes": auditor_changes}


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_report(ticker: str, cik: str, tenk: dict) -> "dict | None":
    facts = sf._company_facts(cik)
    accn, filed = tenk["accn"], tenk["filed"]
    tenk_url = sf._filing_url(cik, accn, tenk["doc"])
    try:
        tenk_text = sf._fetch_text(tenk_url)
    except Exception as exc:
        log.error("%s: failed to fetch 10-K text: %s", ticker, exc)
        tenk_text = ""

    if tenk_text:
        top_risks = _section_top_risks(ticker, tenk_text, accn, filed)
        off_balance_sheet = _section_off_balance_sheet(ticker, tenk_text, accn, filed)
        auditor_gc = _section_auditor_going_concern(tenk_text, accn, filed)
    else:
        top_risks = off_balance_sheet = "N/A -- could not fetch 10-K text."
        auditor_gc = {"going_concern": "N/A -- could not fetch 10-K text.",
                      "auditor_changes": "N/A -- could not fetch 10-K text."}

    return {
        "ticker": ticker, "accn": accn, "filed": filed, "tenk_url": tenk_url,
        "top_risks": top_risks,
        "off_balance_sheet": off_balance_sheet,
        "goodwill": _section_goodwill(ticker, facts, accn, tenk_text, filed),
        "dso": _section_dso_trend(facts, accn),
        "inventory": _section_inventory_trend(facts, accn),
        "debt_maturity": _section_debt_maturity(facts, accn),
        "auditor_gc": auditor_gc,
    }


# ---------------------------------------------------------------------------
# Notion (duplicated per-script, not shared -- established norm in this
# project)
# ---------------------------------------------------------------------------

def _notion(method: str, path: str, payload: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"notion {exc.code} on {path}: {detail}") from exc


def _rich(text: str) -> list:
    return [{"type": "text", "text": {"content": (text or "")[:2000]}}]


def load_config() -> "dict | None":
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _existing_parent_page(cfg: dict) -> str:
    result = _notion("GET", f"/databases/{cfg['database_id']}", {}, cfg["api_key"])
    parent = result.get("parent", {})
    if parent.get("type") != "page_id":
        raise RuntimeError(f"unexpected parent type for existing database: {parent}")
    return parent["page_id"]


def create_database(parent_page_id: str, api_key: str) -> str:
    result = _notion("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": "股票風險與紅旗 Risk & Red Flags"}}],
        "properties": {
            "Ticker": {"title": {}},
            "Filed Date": {"date": {}},
            "Accession": {"rich_text": {}},
            "Top Risks": {"rich_text": {}},
            "Off-Balance-Sheet Liabilities": {"rich_text": {}},
            "Goodwill % of Equity": {"number": {"format": "percent"}},
            "Goodwill Flag": {"select": {"options": [{"name": "Elevated"}, {"name": "Normal"}]}},
            "Goodwill Commentary": {"rich_text": {}},
            "DSO Trend": {"select": {"options": [{"name": "Rising"}, {"name": "Falling/Flat"}]}},
            "DSO Detail": {"rich_text": {}},
            "Inventory/Revenue Trend": {"select": {"options": [{"name": "Rising"}, {"name": "Falling/Flat"}]}},
            "Inventory Detail": {"rich_text": {}},
            "Debt Due in 24mo": {"number": {"format": "number"}},
            "Refinancing Risk Flag": {"select": {"options": [{"name": "Flagged"}, {"name": "Clear"}]}},
            "Debt Maturity Basis": {"rich_text": {}},
            "Going Concern": {"rich_text": {}},
            "Auditor Changes": {"rich_text": {}},
        },
    }, api_key)
    return result["id"]


def _page_properties(report: dict) -> dict:
    goodwill, dso, inventory, debt = report["goodwill"], report["dso"], report["inventory"], report["debt_maturity"]
    props = {
        "Ticker": {"title": _rich(report["ticker"])},
        "Filed Date": {"date": {"start": report["filed"]}},
        "Accession": {"rich_text": _rich(report["accn"])},
        "Top Risks": {"rich_text": _rich(report["top_risks"])},
        "Off-Balance-Sheet Liabilities": {"rich_text": _rich(report["off_balance_sheet"])},
        "Goodwill Commentary": {"rich_text": _rich(goodwill["commentary"])},
        "DSO Detail": {"rich_text": _rich(_dso_table(dso["rows"]))},
        "Inventory Detail": {"rich_text": _rich(_inventory_table(inventory["rows"], inventory.get("note")))},
        "Debt Maturity Basis": {"rich_text": _rich(debt["basis"])},
        "Going Concern": {"rich_text": _rich(report["auditor_gc"]["going_concern"])},
        "Auditor Changes": {"rich_text": _rich(report["auditor_gc"]["auditor_changes"])},
    }
    if goodwill.get("pct_of_equity") is not None:
        props["Goodwill % of Equity"] = {"number": round(goodwill["pct_of_equity"] / 100, 4)}
    if goodwill.get("flagged") is not None:
        props["Goodwill Flag"] = {"select": {"name": "Elevated" if goodwill["flagged"] else "Normal"}}
    if dso.get("rising") is not None:
        props["DSO Trend"] = {"select": {"name": "Rising" if dso["rising"] else "Falling/Flat"}}
    if inventory.get("rising") is not None:
        props["Inventory/Revenue Trend"] = {"select": {"name": "Rising" if inventory["rising"] else "Falling/Flat"}}
    if debt.get("due_24mo") is not None:
        props["Debt Due in 24mo"] = {"number": round(debt["due_24mo"], 2)}
    if debt.get("flagged") is not None:
        props["Refinancing Risk Flag"] = {"select": {"name": "Flagged" if debt["flagged"] else "Clear"}}
    return props


# ---------------------------------------------------------------------------
# Trigger poll (new mechanism -- watches for a new 10-K, not the 8-K
# item-2.02 event stock_earnings.py/stock_valuation.py react to)
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _generate_and_write(ticker: str, cik: str, tenk: dict, cfg: "dict | None") -> "dict | None":
    report = build_report(ticker, cik, tenk)
    if not report:
        return None
    if cfg and cfg.get("api_key") and cfg.get("risk_database_id"):
        _notion("POST", "/pages", {
            "parent": {"database_id": cfg["risk_database_id"]},
            "properties": _page_properties(report),
        }, cfg["api_key"])
        if sf.regenerate(ticker):
            log.info("%s: regenerated Category 1 report after risk-flags update", ticker)
    return report


def poll_and_generate() -> int:
    cfg = load_config()
    if not cfg or not cfg.get("api_key") or not cfg.get("risk_database_id"):
        log.info("not configured (%s); skipping", CONFIG)
        return 0

    state = _load_json(WATCH_STATE, {})
    written = 0
    for ticker in WATCHLIST:
        if ticker in sf.EXCLUDE_NO_SEC_FILINGS:
            continue
        try:
            cik = sf._cik_for_ticker(ticker)
            if not cik:
                log.warning("%s: no CIK found", ticker)
                continue
            subs = sf._submissions(cik)
            hit = sf._latest_filing(subs, "10-K")
            if not hit:
                continue
            already = state.get(ticker, {}).get("last_processed_accn")
            if hit["accn"] == already:
                continue
            # Deliberately no STALE_DAYS-style first-run guard here (see
            # module docstring): a 10-K's risk factors aren't stale "news"
            # the way a quarterly earnings surprise is, so the first-ever
            # encounter of any ticker always generates a report.
            report = _generate_and_write(ticker, cik, hit, cfg)
            if not report:
                continue
            state.setdefault(ticker, {})["last_processed_accn"] = hit["accn"]
            state[ticker]["last_processed_filed"] = hit["filed"]
            written += 1
            log.info("%s: generated risk-flags report for accession %s (filed %s)", ticker, hit["accn"], hit["filed"])
        except Exception as exc:
            log.error("%s: risk-flags report failed: %s", ticker, exc)

    _save_json(WATCH_STATE, state)
    log.info("wrote %d report(s)", written)
    return written


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--setup":
        cfg = load_config()
        if not cfg or not cfg.get("api_key") or not cfg.get("database_id"):
            print(f"{CONFIG} needs an existing api_key + database_id first "
                  "(see notion_sync.py --setup)")
            sys.exit(1)
        parent_page_id = _existing_parent_page(cfg)
        db_id = create_database(parent_page_id, cfg["api_key"])
        cfg["risk_database_id"] = db_id
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print(f"database created and saved to config: {db_id}")
        return

    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=None,
                         help="comma-separated ticker list; bypasses the reactive dedupe and always regenerates")
    args = parser.parse_args()

    if args.tickers:
        cfg = load_config()
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        written = 0
        for ticker in tickers:
            try:
                cik = sf._cik_for_ticker(ticker)
                if not cik:
                    print(f"{ticker}: no CIK found")
                    continue
                subs = sf._submissions(cik)
                hit = sf._latest_filing(subs, "10-K")
                if not hit:
                    print(f"{ticker}: no 10-K found")
                    continue
                report = _generate_and_write(ticker, cik, hit, cfg)
                if not report:
                    print(f"{ticker}: report build failed")
                    continue
                print(f"=== {ticker} (accession {hit['accn']}, filed {hit['filed']}) ===")
                print(json.dumps(_page_properties(report), indent=2, ensure_ascii=False)[:4000])
                written += 1
            except Exception as exc:
                log.error("%s: risk-flags report failed: %s", ticker, exc)
                print(f"{ticker}: FAILED -- {exc}")
        print(f"processed {written}/{len(tickers)} tickers")
        return

    try:
        poll_and_generate()
    except Exception as exc:
        log.error("run failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
