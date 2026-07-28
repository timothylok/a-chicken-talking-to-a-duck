"""Category 3: multi-method valuation (multiples, DCF, reverse-DCF,
sensitivity, sum-of-the-parts), generated automatically the day after each
watchlist ticker's earnings release, saved to Notion.

Runs daily via the "VoiceOS Valuation Watch" scheduled task, 1:30 AM NZT
(30 min after "VoiceOS Earnings Watch" -- staggered to avoid concurrent
SEC/Ollama load, not a hard dependency; this script re-derives its own
trigger independently). Deliberately a SEPARATE script/task/state file from
stock_earnings.py, not coupled into its loop -- every category script in
this project is independently runnable/disableable.

Trigger: identical mechanism to stock_earnings.py (reactive SEC 8-K
item-2.02 poll, accession-based dedupe, 30-day staleness guard) -- imported
from there, not reimplemented. Cadence is quarterly-reactive only: the
inputs this report depends on (DCF assumptions, WACC components, multiples
denominators) only change when new financials land, so a separate daily
price-refresh tier wasn't built for v1.

Scope, and why: of the user's original 8 prompts (15-22), 3 needed real
scoping decisions --
  - Prompt 15 (quick multiples table): forward P/E dropped -- no free
    forward-fiscal-year consensus EPS source exists (Nasdaq's free
    calendar only has next-quarter consensus, not a forward-year
    aggregate). Trailing multiples + a 5-year historical median are built
    instead. Historical coverage varies genuinely by ticker (confirmed
    live: TSLA's own revenue XBRL tag has a ~4-year gap, 2018Q4-2022Q2,
    while AAPL's is clean back to 2017) -- the report states the actual
    point count and date range used, never an unconditional "5-year" claim.
  - Prompt 16 (peer multiples): "3 closest public peers" comes from a new
    user-curated ops/peers.json (same owner-edited/no-secrets/committed
    pattern as ops/workflows.json) -- SEC's SIC-code company listing is
    real but too coarse (e.g. AAPL's SIC 3571 groups it with legacy PC
    makers) and has response-quality issues to auto-match "closest peers".
  - Prompt 22 (sum-of-the-parts): EV/Revenue only, never EV/EBITDA -- SEC's
    XBRL API has no dimensional segment breakdown at all (confirmed live:
    neither companyfacts nor the frames API expose it), only segment
    REVENUE is available, and only from some filers' own press-release
    tables (confirmed: AAPL, TSLA; not yet verified for the rest of the
    watchlist).
Prompts 17-21 (DCF setup, WACC, full DCF, reverse DCF, sensitivity matrix)
are fully in scope, no data gaps -- and are 100% deterministic Python, zero
LLM calls: pure arithmetic where wrong math is strictly worse than wrong
prose, and every number is fully derivable from already-gathered inputs.
Only prompts 15/16/22 have a genuine narrative component (LLM call over
already-computed numbers, same "compute first, narrate second" split
stock_earnings.py already uses).

XBRL gotcha found live this session, not caught by design alone: MSFT and
GOOGL report NO combined "DepreciationDepletionAndAmortization"-style tag
at all -- D&A must be built from a standalone "Depreciation" tag PLUS a
separate "AmortizationOfIntangibleAssets" tag summed together (confirmed:
MSFT FY2025 Depreciation $22.0B + AmortizationOfIntangibleAssets $6.0B,
two genuinely distinct, non-overlapping figures). AAPL, by contrast, does
report a single combined tag. See _da_qtd_series's design -- do not assume
one XBRL tag idiom covers every filer; this is the same lesson
stock_earnings.py's income-figure extraction learned the hard way,
confirmed to recur here on a different data point.

Setup:
  1. Requires ops/notion.json already configured with api_key + database_id
     (see notion_sync.py's own --setup).
  2. ops/peers.json (optional, user-curated) for prompt 16 -- tickers
     missing from it get "N/A" peer comparisons, not a guess.
  3. python ops/stock_valuation.py --setup
     (creates the "股票估值 Valuation" database under the same parent page
     as the existing Voice History database, adds valuation_database_id
     to the config)

Until valuation_database_id exists in the config, runs are silent no-ops.
"""

import datetime as dt
import json
import logging
import os
import re
import statistics
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_fundamentals as sf  # noqa: E402
import stock_earnings as se  # noqa: E402

# Deliberately NOT sf.OLLAMA_MODEL (lfm2.5) or se.EARNINGS_MODEL blindly --
# benchmark this script's own 3 narrative prompts (15/16/22) before locking
# in a default; see the model-benchmark note near the narrative section.
VALUATION_MODEL = "qwen3:8b"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "asr", "logs", "stock_valuation.log")
CONFIG = os.path.join(ROOT, "ops", "notion.json")
PEERS_CONFIG = os.path.join(ROOT, "ops", "peers.json")
WATCH_STATE = os.path.join(ROOT, "asr", "logs", "valuation_watch_state.json")
NOTION_VERSION = "2022-06-28"
NZ_TZ = ZoneInfo("Pacific/Auckland")
STALE_DAYS = 30
TERMINAL_GROWTH_RATE = 2.5  # percent; long-run-GDP proxy, a stated assumption, not derived from data
EQUITY_RISK_PREMIUM = 5.5  # percent; fixed per the prompt's own spec

WATCHLIST = [
    t.strip().upper()
    for t in os.environ.get("STOCK_WATCHLIST", "AAPL,MSFT,NVDA,TSLA,GOOGL,AMZN,META,SPCX").split(",")
    if t.strip()
]

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
# logging.basicConfig() would be a silent no-op here -- importing
# stock_fundamentals/stock_earnings above already configured the root
# logger via their own basicConfig() calls (confirmed live, the exact bug
# stock_earnings.py itself hit and fixed the same way).
log = logging.getLogger("stock_valuation")
log.propagate = False
log.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)


# ---------------------------------------------------------------------------
# New XBRL tag lists (verified present for AAPL/MSFT/GOOGL 2026-07-28;
# NOT yet verified for correctness across the full watchlist -- follow the
# same live-testing discipline that caught 3 real bugs in stock_earnings.py)
# ---------------------------------------------------------------------------

LONGTERM_DEBT_NONCURRENT_TAGS = ["LongTermDebtNoncurrent"]
LONGTERM_DEBT_CURRENT_TAGS = ["LongTermDebtCurrent"]
LONGTERM_DEBT_COMBINED_TAGS = ["LongTermDebt"]
CASH_TAGS = ["CashAndCashEquivalentsAtCarryingValue"]
SHORT_TERM_INVESTMENTS_TAGS = ["ShortTermInvestments", "MarketableSecuritiesCurrent"]
TAX_TAGS = ["IncomeTaxExpenseBenefit"]
PRETAX_INCOME_TAGS = [
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
]
INTEREST_EXPENSE_TAGS = ["InterestExpense", "InterestExpenseDebt"]
# Combined D&A tags (single value, complete -- AAPL uses this style).
COMBINED_DA_TAGS = ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization"]
# Fallback when no combined tag exists (MSFT/GOOGL, confirmed live): sum
# these two instead. Do NOT add amortization on top of an already-combined
# tag -- double-counting.
STANDALONE_DEPRECIATION_TAGS = ["Depreciation"]
AMORTIZATION_INTANGIBLES_TAGS = ["AmortizationOfIntangibleAssets"]
DILUTED_EPS_TAGS = ["EarningsPerShareDiluted"]
STOCKHOLDERS_EQUITY_TAGS = ["StockholdersEquity"]
DILUTED_SHARES_TAGS = ["WeightedAverageNumberOfDilutedSharesOutstanding"]


# ---------------------------------------------------------------------------
# Full-history XBRL helpers (unscoped by accession, unlike
# sf._xbrl_series -- needed for 5-year historical multiples and TTM builds)
# ---------------------------------------------------------------------------

def _xbrl_full_series(facts: dict, tags: list, unit: str = "USD") -> "tuple[str | None, list]":
    # Pulls every entry for the first matching tag across the WHOLE
    # companyfacts payload (not scoped to one accession like
    # sf._xbrl_series). The same (start, end) period genuinely appears
    # multiple times across filings (confirmed live: a quarter filed once
    # as the current period, then again a year later as the prior-year
    # comparative) -- dedupe by (start, end), keeping the entry with the
    # latest "filed" date (most-recently-reported value, picks up
    # restatements).
    for tag in tags:
        node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
        if not node:
            continue
        arr = node.get("units", {}).get(unit)
        if not arr:
            continue
        by_period = {}
        for item in arr:
            key = (item.get("start"), item["end"])
            existing = by_period.get(key)
            if not existing or item.get("filed", "") >= existing.get("filed", ""):
                by_period[key] = item
        return tag, sorted(by_period.values(), key=lambda i: i["end"])
    return None, []


def _quarterly_qtd_series(full_series: list) -> list:
    # Filters to QTD (~3-month) duration entries, excluding YTD/6-month/
    # 9-month cumulative entries that share the same tag. Instant
    # (balance-sheet) tags have start == end and aren't relevant here.
    out = []
    for item in full_series:
        if not item.get("start"):
            continue
        days = (dt.date.fromisoformat(item["end"]) - dt.date.fromisoformat(item["start"])).days
        if 80 <= days <= 100:
            out.append(item)
    return out


def _fy_series(full_series: list) -> list:
    out = []
    for item in full_series:
        if not item.get("start"):
            continue
        days = (dt.date.fromisoformat(item["end"]) - dt.date.fromisoformat(item["start"])).days
        if days > 350:
            out.append(item)
    return out


def _derive_q4(qtd_series: list, fy_series: list) -> list:
    # XBRL never reports a standalone Q4 duration fact -- only Q1-Q3 (via
    # 10-Qs) and the full fiscal year (via the 10-K). Synthesize Q4 =
    # FY_total - (Q1+Q2+Q3) when all three QTD quarters for that fiscal
    # year and the FY total are present. Fiscal year identified by the FY
    # entry's end date; the three preceding QTD quarters are matched by
    # falling within that FY's start/end window.
    out = list(qtd_series)
    by_end = {q["end"]: q for q in qtd_series}
    for fy in fy_series:
        fy_start, fy_end = fy["start"], fy["end"]
        in_year = [q for q in qtd_series if fy_start <= q["start"] < fy_end]
        if len(in_year) != 3:
            continue
        q4_val = fy["val"] - sum(q["val"] for q in in_year)
        # Q4's own end date is the FY's end date; start is the latest of
        # the three QTD quarters' end dates (approximation -- exact start
        # isn't independently knowable without another data point, but
        # only "end" is used downstream for TTM windowing).
        q4_end = fy_end
        if q4_end not in by_end:
            out.append({"start": max(q["end"] for q in in_year), "end": q4_end,
                        "val": q4_val, "accn": fy["accn"], "filed": fy["filed"], "derived": True})
    return sorted(out, key=lambda i: i["end"])


def _da_qtd_series(facts: dict) -> list:
    tag, full = _xbrl_full_series(facts, COMBINED_DA_TAGS)
    if full:
        return _quarterly_qtd_series(full)
    # No combined tag (MSFT/GOOGL, confirmed live) -- sum standalone
    # depreciation + amortization-of-intangibles by matching period.
    _, dep_full = _xbrl_full_series(facts, STANDALONE_DEPRECIATION_TAGS)
    _, amort_full = _xbrl_full_series(facts, AMORTIZATION_INTANGIBLES_TAGS)
    dep_qtd = _quarterly_qtd_series(dep_full)
    amort_by_period = {(a["start"], a["end"]): a["val"] for a in _quarterly_qtd_series(amort_full)}
    out = []
    for d in dep_qtd:
        val = d["val"] + amort_by_period.get((d["start"], d["end"]), 0)
        out.append({**d, "val": val})
    return out


def _instant_value_at_or_before(facts: dict, tags: list, as_of: str) -> "float | None":
    # For balance-sheet (instant) tags: the value reported at the closest
    # date <= as_of. Used to pull period-specific shares/equity/debt/cash
    # at each historical quarter-end, not just the latest -- buybacks and
    # dilution mean share count isn't constant across 5 years.
    for tag in tags:
        node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
        if not node:
            continue
        arr = node.get("units", {}).get("USD") or node.get("units", {}).get("shares")
        if not arr:
            continue
        candidates = [i for i in arr if not i.get("start") and i["end"] <= as_of]
        if not candidates:
            continue
        return max(candidates, key=lambda i: i["end"])["val"]
    return None


def _total_debt_at(facts: dict, as_of: str) -> "float | None":
    combined = _instant_value_at_or_before(facts, LONGTERM_DEBT_COMBINED_TAGS, as_of)
    noncurrent = _instant_value_at_or_before(facts, LONGTERM_DEBT_NONCURRENT_TAGS, as_of)
    current = _instant_value_at_or_before(facts, LONGTERM_DEBT_CURRENT_TAGS, as_of)
    if noncurrent is not None or current is not None:
        return (noncurrent or 0) + (current or 0)
    return combined


def _cash_and_sti_at(facts: dict, as_of: str) -> "float | None":
    cash = _instant_value_at_or_before(facts, CASH_TAGS, as_of)
    sti = _instant_value_at_or_before(facts, SHORT_TERM_INVESTMENTS_TAGS, as_of)
    if cash is None:
        return None
    return cash + (sti or 0)


def _net_debt_at(facts: dict, as_of: str) -> "float | None":
    debt = _total_debt_at(facts, as_of)
    cash = _cash_and_sti_at(facts, as_of)
    if debt is None and cash is None:
        return None
    return (debt or 0) - (cash or 0)


# ---------------------------------------------------------------------------
# External data: Treasury 10yr yield, historical/current prices (different
# hosts than SEC -- SEC_UA/SEC_DELAY politeness constants don't apply)
# ---------------------------------------------------------------------------

def _treasury_10yr_yield(as_of: "dt.date | None" = None) -> "float | None":
    as_of = as_of or dt.datetime.now(NZ_TZ).date()
    for months_back in (0, 1):  # this month, falling back to last month if empty (e.g. run on the 1st)
        year_month = (as_of.replace(day=1) - dt.timedelta(days=months_back * 28)).strftime("%Y%m")
        url = (f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/all/{year_month}?type=daily_treasury_yield_curve"
               f"&field_tdr_date_value_month={year_month}&page&_format=csv")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                lines = resp.read().decode("utf-8").splitlines()
        except Exception as exc:
            log.warning("treasury fetch failed for %s: %s", year_month, exc)
            continue
        if len(lines) < 2:
            continue
        header = lines[0].split(",")
        try:
            idx = header.index('"10 Yr"')
        except ValueError:
            continue
        row = lines[1].split(",")  # most recent date first
        try:
            return float(row[idx])
        except (IndexError, ValueError):
            continue
    return None


def _historical_prices(ticker: str, range_: str = "5y", interval: str = "1mo") -> list:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={range_}&interval={interval}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read())
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        return []
    timestamps = result.get("timestamp") or []
    closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    out = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        date = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date().isoformat()
        out.append({"date": date, "close": float(close)})
    return out


def _current_price(ticker: str) -> "float | None":
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=5d&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read())
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        return None
    return result.get("meta", {}).get("regularMarketPrice")


# ---------------------------------------------------------------------------
# Beta (pure Python, no numpy -- matches this project's explicit
# anti-dependency stance stated elsewhere: "no yfinance/pandas/numpy")
# ---------------------------------------------------------------------------

def _monthly_returns(prices: list) -> list:
    return [prices[i]["close"] / prices[i - 1]["close"] - 1 for i in range(1, len(prices)) if prices[i - 1]["close"]]


def _beta(stock_prices: list, market_prices: list) -> "float | None":
    stock_returns = _monthly_returns(stock_prices)
    market_returns = _monthly_returns(market_prices)
    n = min(len(stock_returns), len(market_returns))
    if n < 12:  # not enough overlap for a meaningful regression
        return None
    # Trim both to the LAST n months, not the first -- a recently-IPO'd
    # ticker naturally has a shorter series; align on the most recent
    # shared window rather than erroring.
    s = stock_returns[-n:]
    m = market_returns[-n:]
    mean_s = sum(s) / n
    mean_m = sum(m) / n
    covariance = sum((s[i] - mean_s) * (m[i] - mean_m) for i in range(n)) / (n - 1)
    variance_m = sum((x - mean_m) ** 2 for x in m) / (n - 1)
    if variance_m == 0:
        return None
    return covariance / variance_m


# ---------------------------------------------------------------------------
# WACC (prompt 18)
# ---------------------------------------------------------------------------

def _effective_tax_rate(facts: dict, accn: str) -> float:
    _, tax_series = sf._xbrl_series(facts, TAX_TAGS, accn)
    _, pretax_series = sf._xbrl_series(facts, PRETAX_INCOME_TAGS, accn)
    if tax_series and pretax_series and pretax_series[-1]["val"]:
        rate = tax_series[-1]["val"] / pretax_series[-1]["val"] * 100
        if 0 <= rate <= 50:
            return rate
    return 24.0  # flat fallback; caller states this explicitly in the report


def _compute_wacc(facts: dict, tenk_accn: str, current_price: float, diluted_shares: float,
                   beta: "float | None", risk_free_rate: float) -> dict:
    out = {"risk_free_rate": risk_free_rate}
    _, interest_series = sf._xbrl_series(facts, INTEREST_EXPENSE_TAGS, tenk_accn)
    interest_stale = False
    if not interest_series:
        # Some filers (AAPL, confirmed live 2026-07-28) no longer report a
        # standalone "InterestExpense" line at all in recent 10-Ks -- their
        # income statement folds it into "Other income/(expense), net"
        # with no free way to decompose it. Fall back to the most recent
        # value from ANY filing (via the unscoped full-history search)
        # rather than giving up outright; flag it as stale if found this
        # way, since it may be several years old and no longer reflect the
        # capital structure.
        _, interest_full = _xbrl_full_series(facts, INTEREST_EXPENSE_TAGS)
        if interest_full:
            interest_series = [interest_full[-1]]
            interest_stale = True
    total_debt = _total_debt_at(facts, dt.date.today().isoformat())
    tax_rate = _effective_tax_rate(facts, tenk_accn)
    out["tax_rate"] = tax_rate
    out["tax_rate_is_fallback"] = tax_rate == 24.0

    if beta is not None:
        cost_of_equity = risk_free_rate + beta * EQUITY_RISK_PREMIUM
    else:
        cost_of_equity = None
    out["beta"] = beta
    out["cost_of_equity"] = cost_of_equity

    cost_of_debt_aftertax = None
    if interest_series and total_debt:
        interest_expense = interest_series[-1]["val"]
        cost_of_debt_pretax = interest_expense / total_debt * 100
        cost_of_debt_aftertax = cost_of_debt_pretax * (1 - tax_rate / 100)
    out["cost_of_debt_aftertax"] = cost_of_debt_aftertax
    out["cost_of_debt_stale"] = interest_stale
    out["cost_of_debt_unavailable"] = cost_of_debt_aftertax is None
    out["total_debt"] = total_debt

    if current_price is None or diluted_shares is None:
        out["wacc"] = None
        return out
    market_cap = current_price * diluted_shares
    out["market_cap"] = market_cap
    denom = market_cap + (total_debt or 0)
    if denom <= 0 or cost_of_equity is None:
        out["wacc"] = None
        return out
    we = market_cap / denom
    wd = (total_debt or 0) / denom
    wacc = we * cost_of_equity + wd * (cost_of_debt_aftertax or 0)
    out["weight_equity"] = we
    out["weight_debt"] = wd
    out["wacc"] = wacc
    return out


# ---------------------------------------------------------------------------
# Trailing + 5-year historical multiples (prompt 15)
# ---------------------------------------------------------------------------

def _consecutive_ttm_windows(qtd_series: list) -> list:
    # Every window of 4 TRULY consecutive quarters (gap-checked via date
    # arithmetic between each pair, not just index adjacency) -- a real
    # gap (confirmed live: TSLA's own revenue tag is missing 2018Q4 to
    # 2022Q2 entirely) must never silently produce a TTM sum that quietly
    # spans across the hole.
    s = sorted(qtd_series, key=lambda x: x["end"])
    windows = []
    for i in range(3, len(s)):
        four = s[i - 3:i + 1]
        consecutive = all(
            75 <= (dt.date.fromisoformat(four[j]["end"]) - dt.date.fromisoformat(four[j - 1]["end"])).days <= 105
            for j in range(1, 4)
        )
        if consecutive:
            windows.append(four)
    return windows


def _closest_price(prices: list, target_date: str) -> "float | None":
    if not prices:
        return None
    target = dt.date.fromisoformat(target_date)
    best = min(prices, key=lambda p: abs((dt.date.fromisoformat(p["date"]) - target).days))
    return best["close"]


def _historical_multiples(facts: dict, weekly_prices: list) -> dict:
    _, rev_full = _xbrl_full_series(facts, sf.REVENUE_TAGS)
    rev_qtd = _derive_q4(_quarterly_qtd_series(rev_full), _fy_series(rev_full))

    _, oi_full = _xbrl_full_series(facts, sf.OPERATING_INCOME_TAGS)
    oi_qtd = _derive_q4(_quarterly_qtd_series(oi_full), _fy_series(oi_full))

    _, da_combined_full = _xbrl_full_series(facts, COMBINED_DA_TAGS)
    if da_combined_full:
        da_qtd = _derive_q4(_quarterly_qtd_series(da_combined_full), _fy_series(da_combined_full))
    else:
        da_qtd = _da_qtd_series(facts)  # already summed standalone+amortization; no combined FY tag to derive Q4 from

    _, eps_full = _xbrl_full_series(facts, DILUTED_EPS_TAGS, unit="USD/shares")
    eps_qtd = _derive_q4(_quarterly_qtd_series(eps_full), _fy_series(eps_full))

    rev_by_end = {r["end"]: r["val"] for r in rev_qtd}
    oi_by_end = {r["end"]: r["val"] for r in oi_qtd}
    da_by_end = {r["end"]: r["val"] for r in da_qtd}
    eps_by_end = {r["end"]: r["val"] for r in eps_qtd}

    points = []
    for window in _consecutive_ttm_windows(rev_qtd):
        end = window[-1]["end"]
        ends = [q["end"] for q in window]
        ttm_revenue = sum(rev_by_end[e] for e in ends)
        ttm_ebitda = None
        if all(e in oi_by_end and e in da_by_end for e in ends):
            ttm_ebitda = sum(oi_by_end[e] for e in ends) + sum(da_by_end[e] for e in ends)
        ttm_eps = sum(eps_by_end[e] for e in ends) if all(e in eps_by_end for e in ends) else None

        equity = _instant_value_at_or_before(facts, STOCKHOLDERS_EQUITY_TAGS, end)
        shares = _instant_value_at_or_before(facts, ["CommonStockSharesOutstanding"], end)
        net_debt = _net_debt_at(facts, end)
        price = _closest_price(weekly_prices, end)

        if not price or not shares:
            continue
        market_cap = price * shares
        ev = market_cap + (net_debt or 0)

        point = {"end": end}
        point["pe"] = price / ttm_eps if ttm_eps and ttm_eps > 0 else None
        point["ev_revenue"] = ev / ttm_revenue if ttm_revenue and ttm_revenue > 0 else None
        point["ev_ebitda"] = ev / ttm_ebitda if ttm_ebitda and ttm_ebitda > 0 else None
        point["pb"] = market_cap / equity if equity and equity > 0 else None
        points.append(point)

    medians = {}
    for key in ("pe", "ev_revenue", "ev_ebitda", "pb"):
        vals = [p[key] for p in points if p[key] is not None]
        medians[key] = statistics.median(vals) if vals else None
        medians[f"{key}_n"] = len(vals)

    return {
        "points": points,
        "medians": medians,
        "date_range": (points[0]["end"], points[-1]["end"]) if points else None,
    }


def _latest_ttm(qtd_series: list) -> "float | None":
    # Sum of the most recent 4 TRULY CONSECUTIVE quarters (reuses the same
    # gap-checked windowing _historical_multiples uses) -- NOT a naive
    # [-4:] slice. Confirmed live this matters: AAPL's D&A tag only ever
    # has Q1 entries (one per year, no Q2/Q3), so a naive [-4:] slice
    # silently summed 4 DIFFERENT YEARS' Q1 figures as if they were one
    # TTM window, producing a wrong "trailing EBITDA" that was actually
    # ~4 years of Q1-only figures added together.
    windows = _consecutive_ttm_windows(qtd_series)
    if not windows:
        return None
    by_end = {q["end"]: q["val"] for q in qtd_series}
    return sum(by_end[q["end"]] for q in windows[-1])


def _trailing_multiples(facts: dict, tenk_accn: str, current_price: float,
                         shares: "float | None", net_debt: "float | None") -> dict:
    # Same 4 multiples as _historical_multiples, but for "right now" --
    # uses TTM EPS/revenue where quarterly data supports it, falling back
    # to the latest fiscal year's figures for EBITDA specifically. Some
    # filers (AAPL, confirmed live) never report D&A at quarterly
    # granularity at all (every quarterly-shaped D&A tag only has Q1
    # entries, Q2/Q3 simply aren't separately tagged) -- TTM EBITDA can't
    # be assembled for them even for a single trailing point, so this uses
    # latest-FY operating income + latest-FY D&A instead, clearly flagged
    # as annual-not-trailing rather than silently reported as if it were
    # a true trailing-twelve-month figure.
    out = {}
    if current_price and shares:
        market_cap = current_price * shares
        out["market_cap"] = market_cap
        ev = market_cap + (net_debt or 0)
        out["ev"] = ev
    else:
        market_cap = ev = None

    _, eps_full = _xbrl_full_series(facts, DILUTED_EPS_TAGS, unit="USD/shares")
    eps_qtd = _derive_q4(_quarterly_qtd_series(eps_full), _fy_series(eps_full))
    ttm_eps = _latest_ttm(eps_qtd)
    out["pe"] = current_price / ttm_eps if current_price and ttm_eps and ttm_eps > 0 else None

    _, rev_full = _xbrl_full_series(facts, sf.REVENUE_TAGS)
    rev_qtd = _derive_q4(_quarterly_qtd_series(rev_full), _fy_series(rev_full))
    ttm_revenue = _latest_ttm(rev_qtd)
    out["ttm_revenue"] = ttm_revenue
    out["ev_revenue"] = ev / ttm_revenue if ev and ttm_revenue and ttm_revenue > 0 else None

    _, oi_full = _xbrl_full_series(facts, sf.OPERATING_INCOME_TAGS)
    oi_qtd = _derive_q4(_quarterly_qtd_series(oi_full), _fy_series(oi_full))
    da_qtd = _da_qtd_series(facts)
    ttm_oi = _latest_ttm(oi_qtd)
    ttm_da = _latest_ttm(da_qtd)
    ttm_ebitda = None
    ebitda_is_annual = False
    if ttm_oi is not None and ttm_da is not None:
        ttm_ebitda = ttm_oi + ttm_da
    else:
        # Fall back to latest full fiscal year (10-K), not a TTM figure.
        _, oi_scoped = sf._xbrl_series(facts, sf.OPERATING_INCOME_TAGS, tenk_accn)
        _, da_combined_scoped = sf._xbrl_series(facts, COMBINED_DA_TAGS, tenk_accn)
        if oi_scoped and da_combined_scoped:
            ttm_ebitda = oi_scoped[-1]["val"] + da_combined_scoped[-1]["val"]
            ebitda_is_annual = True
        elif oi_scoped:
            _, dep_scoped = sf._xbrl_series(facts, STANDALONE_DEPRECIATION_TAGS, tenk_accn)
            _, amort_scoped = sf._xbrl_series(facts, AMORTIZATION_INTANGIBLES_TAGS, tenk_accn)
            if dep_scoped:
                da_val = dep_scoped[-1]["val"] + (amort_scoped[-1]["val"] if amort_scoped else 0)
                ttm_ebitda = oi_scoped[-1]["val"] + da_val
                ebitda_is_annual = True
    out["ebitda_is_annual"] = ebitda_is_annual
    out["ev_ebitda"] = ev / ttm_ebitda if ev and ttm_ebitda and ttm_ebitda > 0 else None

    equity = _instant_value_at_or_before(facts, STOCKHOLDERS_EQUITY_TAGS, dt.date.today().isoformat())
    out["pb"] = market_cap / equity if market_cap and equity and equity > 0 else None
    return out


# ---------------------------------------------------------------------------
# DCF setup / growth glide path (prompt 17)
# ---------------------------------------------------------------------------

def _growth_glide_path(g0: float, terminal_growth: float, years: int = 5) -> list:
    # Linear interpolation landing exactly on terminal_growth at year 5 --
    # internally consistent with the terminal-value assumption used later.
    # A stated modeling choice, not derived from data: year-1 growth is
    # already partway from g0 toward terminal (a smooth glide), not a hard
    # g0-in-year-1 step.
    return [g0 + (terminal_growth - g0) * (i / years) for i in range(1, years + 1)]


# ---------------------------------------------------------------------------
# Full DCF / reverse DCF / sensitivity matrix (prompts 19-21) -- 100%
# deterministic Python, zero LLM calls: wrong math here is strictly worse
# than wrong prose, and every number is fully derivable without a model.
# ---------------------------------------------------------------------------

def _dcf_fair_value(wacc: float, growth_path: list, terminal_growth: float,
                     base_revenue: float, operating_margin: float,
                     capex_pct_revenue: float, tax_rate: float,
                     net_debt: float, shares: float) -> "dict | None":
    if wacc <= terminal_growth or shares <= 0:
        return None  # Gordon growth is undefined/nonsensical when WACC <= terminal growth
    revenue = base_revenue
    revenues, fcfs, pvs = [], [], []
    for i, g in enumerate(growth_path, start=1):
        revenue = revenue * (1 + g / 100)
        # Unlevered FCF proxy: EBIT*(1-tax) - capex. D&A add-back and
        # working-capital changes deliberately omitted for simplicity --
        # stated in the report's methodology line, not hidden.
        fcf = revenue * (operating_margin / 100) * (1 - tax_rate / 100) - revenue * (capex_pct_revenue / 100)
        pv = fcf / (1 + wacc / 100) ** i
        revenues.append(revenue)
        fcfs.append(fcf)
        pvs.append(pv)
    terminal_value = fcfs[-1] * (1 + terminal_growth / 100) / (wacc / 100 - terminal_growth / 100)
    pv_terminal = terminal_value / (1 + wacc / 100) ** len(growth_path)
    enterprise_value = sum(pvs) + pv_terminal
    equity_value = enterprise_value - net_debt
    fair_value_per_share = equity_value / shares
    return {
        "revenues": revenues, "fcfs": fcfs, "pvs": pvs,
        "terminal_value": terminal_value, "pv_terminal": pv_terminal,
        "enterprise_value": enterprise_value, "equity_value": equity_value,
        "fair_value_per_share": fair_value_per_share,
    }


def _reverse_dcf(current_price: float, wacc: float, terminal_growth: float,
                  base_revenue: float, operating_margin: float,
                  capex_pct_revenue: float, tax_rate: float,
                  net_debt: float, shares: float) -> dict:
    # Bisection over the starting growth rate g0: fair-value-per-share is
    # monotonically increasing in g0 (more growth -> more FCF -> higher
    # EV), so bisection is safe and auditable (every iteration loggable) --
    # preferred over deriving a closed-form solve of the 6-term PV+TV
    # equation by hand, which is more error-prone to get right.
    def fv(g0):
        path = _growth_glide_path(g0, terminal_growth)
        result = _dcf_fair_value(wacc, path, terminal_growth, base_revenue, operating_margin,
                                  capex_pct_revenue, tax_rate, net_debt, shares)
        return result["fair_value_per_share"] if result else None

    lo, hi = 0.0, wacc - 0.5
    fv_lo, fv_hi = fv(lo), fv(hi)
    if fv_lo is None or fv_hi is None:
        return {"implied_growth": None, "out_of_bounds": "wacc_too_low"}
    if fv_lo > current_price:
        return {"implied_growth": None, "out_of_bounds": "below_zero_growth",
                "note": f"Even 0% growth implies a fair value (${fv_lo:.2f}) above the current price -- "
                        "the market may be pricing in a lower WACC/risk than assumed here, not negative growth."}
    if fv_hi < current_price:
        return {"implied_growth": None, "out_of_bounds": "above_bracket_ceiling",
                "note": f"Even growth just under WACC ({hi:.1f}%) implies a fair value (${fv_hi:.2f}) below "
                        "the current price -- the market may be pricing in margin expansion, lower risk, or "
                        "a terminal multiple this simple linear-glide model can't capture."}
    for _ in range(60):
        mid = (lo + hi) / 2
        if fv(mid) < current_price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-4:
            break
    return {"implied_growth": (lo + hi) / 2, "out_of_bounds": None}


def _sensitivity_matrix(base_wacc: float, base_terminal_growth: float, g0: float,
                         base_revenue: float, operating_margin: float, capex_pct_revenue: float,
                         tax_rate: float, net_debt: float, shares: float) -> list:
    # 3x3 grid; the glide path is re-derived per cell (not reused fixed)
    # since terminal growth changes the glide's endpoint too, for internal
    # consistency with the growth-glide-path design.
    rows = []
    for wacc_delta in (-1, 0, 1):
        row = []
        for g_delta in (-0.5, 0, 0.5):
            wacc = base_wacc + wacc_delta
            terminal_growth = base_terminal_growth + g_delta
            if wacc <= terminal_growth:
                row.append(None)
                continue
            path = _growth_glide_path(g0, terminal_growth)
            result = _dcf_fair_value(wacc, path, terminal_growth, base_revenue, operating_margin,
                                      capex_pct_revenue, tax_rate, net_debt, shares)
            row.append(result["fair_value_per_share"] if result else None)
        rows.append({"wacc": base_wacc + wacc_delta, "values": row})
    return rows


# ---------------------------------------------------------------------------
# Peer multiples comparison (prompt 16)
# ---------------------------------------------------------------------------

def _load_peers(ticker: str) -> list:
    try:
        with open(PEERS_CONFIG, encoding="utf-8") as f:
            peers = json.load(f)
    except (OSError, ValueError):
        return []
    return peers.get(ticker, [])


def _ticker_trailing_multiples(ticker: str) -> "dict | None":
    # Standalone fetch-everything-and-compute wrapper, used for peer
    # tickers that aren't the watchlist ticker already being processed
    # (which has this data gathered inline in build_valuation_report).
    try:
        cik = sf._cik_for_ticker(ticker)
        if not cik:
            return None
        facts = sf._company_facts(cik)
        subs = sf._submissions(cik)
        tenk = sf._latest_filing(subs, "10-K")
        if not tenk:
            return None
        price = _current_price(ticker)
        _, shares_series = sf._xbrl_series(facts, DILUTED_SHARES_TAGS, tenk["accn"], unit="shares")
        shares = shares_series[-1]["val"] if shares_series else None
        net_debt = _net_debt_at(facts, dt.date.today().isoformat())
        return _trailing_multiples(facts, tenk["accn"], price, shares, net_debt)
    except Exception as exc:
        log.warning("peer %s: fetch failed: %s", ticker, exc)
        return None


def _peer_comparison(ticker: str, own_multiples: dict) -> dict:
    peer_tickers = _load_peers(ticker)
    if not peer_tickers:
        return {"peers": [], "note": "N/A -- no peers configured for this ticker in ops/peers.json"}
    peers = []
    for p in peer_tickers:
        m = _ticker_trailing_multiples(p)
        if m:
            peers.append({"ticker": p, **m})
    if not peers:
        return {"peers": [], "note": "N/A -- peer data could not be fetched"}
    peer_pe = [p["pe"] for p in peers if p.get("pe")]
    peer_ev_rev = [p["ev_revenue"] for p in peers if p.get("ev_revenue")]
    peer_ev_ebitda = [p["ev_ebitda"] for p in peers if p.get("ev_ebitda")]
    return {
        "peers": peers,
        "peer_median_pe": statistics.median(peer_pe) if peer_pe else None,
        "peer_median_ev_revenue": statistics.median(peer_ev_rev) if peer_ev_rev else None,
        "peer_median_ev_ebitda": statistics.median(peer_ev_ebitda) if peer_ev_ebitda else None,
        "own_pe": own_multiples.get("pe"),
        "own_ev_revenue": own_multiples.get("ev_revenue"),
        "own_ev_ebitda": own_multiples.get("ev_ebitda"),
    }


# ---------------------------------------------------------------------------
# Sum-of-the-parts (prompt 22) -- EV/Revenue only, never EV/EBITDA (SEC's
# XBRL API has no dimensional segment breakdown at all, confirmed live;
# only segment REVENUE is available, and only from some filers' own
# press-release tables). New extraction, not a reuse of stock_earnings.py's
# segment code -- that hands raw prose to an LLM for a trend narrative
# across 4 quarters, never parses actual current-quarter {segment: dollar}
# data.
# ---------------------------------------------------------------------------

def _extract_segment_revenue(text: str) -> dict:
    # Style A (AAPL, confirmed live): "Net sales by reportable segment:
    # <Name> $NUM $NUM $NUM $NUM <Name> ... Total net sales $NUM ..." --
    # 4 numbers per segment (current Q, prior Q, current 6mo, prior 6mo);
    # current-quarter is the first number.
    m = re.search(r"Net sales by (?:reportable segment|category):", text)
    if m:
        window = text[m.end():m.end() + 1500]
        end_m = re.search(r"Total net sales", window)
        window = window[:end_m.start()] if end_m else window
        segments = {}
        for seg_m in re.finditer(r"([A-Z][A-Za-z .]{1,30}?)\s+\$?\s*([\d,]+)\s+\$?\s*[\d,]+\s+\$?\s*[\d,]+\s+\$?\s*[\d,]+", window):
            name, val = seg_m.group(1).strip(), float(seg_m.group(2).replace(",", ""))
            if val > 0:
                segments[name] = val
        if segments:
            return segments

    # Style B (TSLA, confirmed live): "Total <Name> revenue(s) NUM NUM NUM
    # NUM NUM YoY%" -- a trailing-quarter table (see
    # _is_trailing_quarter_table). A document-wide search for "...revenue(s)"
    # is far too permissive (confirmed live: it matches unrelated lines
    # like "Cost of revenues", "Deferred revenue" from other tables
    # entirely) -- scope strictly to the SAME table block the total-revenue
    # line lives in: from the "Q#-YYYY Q#-YYYY" header down to the "Total
    # revenues" line itself, nothing outside that window.
    header_m = re.search(r"Q\d-20\d\d\s+Q\d-20\d\d", text)
    total_m = re.search(r"Total\s+revenues?\b", text)
    if header_m and total_m and header_m.start() < total_m.start():
        window = text[header_m.start():total_m.start()]
        segments = {}
        for seg_m in re.finditer(r"([A-Z][A-Za-z ]{2,40}?revenues?)\s+", window):
            nums = se._number_run(window, seg_m.end())
            if len(nums) >= 3:  # a real trailing-quarter row, not a stray match
                # The table header ("...Q2-2026 YoY") sits immediately
                # before the first segment row with no clear separator --
                # strip a leaked "YoY " prefix rather than keep it as
                # part of the segment name.
                name = re.sub(r"^YoY\s+", "", seg_m.group(1).strip())
                segments[name] = nums[-1]
        if segments:
            return segments

    return {}


def _sum_of_parts(ticker: str, ex99_text: str, own_ev_revenue_multiple: "float | None",
                   current_market_cap: "float | None") -> dict:
    segments = _extract_segment_revenue(ex99_text)
    if not segments or len(segments) < 3 or own_ev_revenue_multiple is None:
        return {"segments": segments, "sum_of_parts_ev": None,
                "note": "N/A -- fewer than 3 clearly-broken-out segments found in this release, "
                        "or no blended EV/Revenue multiple available"}
    # Two unit-consistency fixes, both required: (1) segment figures are
    # reported in millions in the press release table, but market_cap/EV
    # elsewhere in this script are raw dollars -- scale up by 1e6.
    # (2) segment figures are ONE QUARTER's revenue, but
    # own_ev_revenue_multiple is TTM-basis -- annualize (x4, a stated
    # approximation, not a real annual figure) so the multiple is applied
    # to a comparable basis rather than understating each segment's
    # implied value ~4x.
    segments_annualized_usd = {k: v * 1_000_000 * 4 for k, v in segments.items()}
    sum_of_parts_ev = sum(v * own_ev_revenue_multiple for v in segments_annualized_usd.values())
    return {
        "segments": segments,
        "blended_multiple_used": own_ev_revenue_multiple,
        "sum_of_parts_ev": sum_of_parts_ev,
        "current_market_cap": current_market_cap,
        "premium_discount_pct": ((sum_of_parts_ev - current_market_cap) / current_market_cap * 100)
                                 if current_market_cap else None,
    }


# ---------------------------------------------------------------------------
# LLM narrative (prompts 15/16/22 only -- 17-21 are pure computation).
# "Compute first, narrate second": the LLM only ever explains numbers
# already computed deterministically above, never produces a number itself.
# ---------------------------------------------------------------------------

def _generate(prompt: str, num_predict: int = 400) -> str:
    payload = json.dumps({
        "model": VALUATION_MODEL,
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


MULTIPLES_PROMPT = (
    "Act as an equity analyst. {ticker}'s trailing multiples: P/E {pe}, "
    "EV/Revenue {ev_rev}, EV/EBITDA {ev_ebitda}, P/B {pb}. Its 5-year "
    "historical median (based on {n} quarterly data points from {date_start} "
    "to {date_end}): P/E {med_pe}, EV/Revenue {med_ev_rev}, EV/EBITDA "
    "{med_ev_ebitda}, P/B {med_pb}. Using ONLY these numbers -- do not "
    "invent any other figures -- identify which multiple(s) look most "
    "stretched or cheapest vs. their own historical median, and state "
    "plainly whether the stock is trading above or below its own historical "
    "valuation range. Under 200 words."
)

PEER_PROMPT = (
    "Act as an equity analyst. {ticker}'s trailing P/E is {own_pe}, "
    "EV/Revenue is {own_ev_rev}. Its peer group ({peer_list}) has a median "
    "P/E of {peer_pe} and median EV/Revenue of {peer_ev_rev}. Using ONLY "
    "these numbers, state whether {ticker} trades at a premium or discount "
    "to its peers, and explain in 2 sentences why that might be justified "
    "or not, based only on what these multiples themselves suggest -- do "
    "not invent qualitative facts about the business not given here. "
    "Under 250 words."
)

SUM_OF_PARTS_PROMPT = (
    "Act as an equity analyst. Below is a sum-of-the-parts estimate for "
    "{ticker}: each reportable segment's quarterly revenue, annualized and "
    "valued at the company's own blended EV/Revenue multiple of {multiple}x "
    "(a simplification -- not a per-segment peer multiple). Segments: "
    "{segments}. This sums to an implied enterprise value of {sop_ev}, "
    "vs. a current market cap of {market_cap} ({premium_discount}% "
    "premium/discount). Using ONLY these numbers, caveat this is a rough "
    "estimate (same multiple applied to every segment, not segment-specific "
    "multiples) and note which segment is the largest share of the total. "
    "Under 200 words."
)


# ---------------------------------------------------------------------------
# Report orchestration
# ---------------------------------------------------------------------------

def _pct(val: "float | None") -> str:
    return f"{val:+.1f}%" if val is not None else "N/A"


def build_valuation_report(ticker: str, cik: str, subs: dict, trigger: dict) -> "dict | None":
    facts = sf._company_facts(cik)
    tenk = sf._latest_filing(subs, "10-K")
    if not tenk:
        raise RuntimeError(f"no 10-K found for {ticker}")
    tenk_accn = tenk["accn"]

    ex99_doc = se._exhibit_doc(cik, trigger["accn"])
    ex99_text = sf._fetch_text(sf._filing_url(cik, trigger["accn"], ex99_doc)) if ex99_doc else ""

    price = _current_price(ticker)
    _, shares_series = sf._xbrl_series(facts, DILUTED_SHARES_TAGS, tenk_accn, unit="shares")
    shares = shares_series[-1]["val"] if shares_series else None
    net_debt = _net_debt_at(facts, dt.date.today().isoformat())

    trailing = _trailing_multiples(facts, tenk_accn, price, shares, net_debt)
    weekly_prices = _historical_prices(ticker, "5y", "1wk")
    historical = _historical_multiples(facts, weekly_prices)

    monthly_prices = _historical_prices(ticker, "5y", "1mo")
    market_monthly = _historical_prices("%5EGSPC", "5y", "1mo")
    beta = _beta(monthly_prices, market_monthly)
    risk_free = _treasury_10yr_yield()
    wacc_data = _compute_wacc(facts, tenk_accn, price, shares, beta, risk_free)

    _, rev_scoped = sf._xbrl_series(facts, sf.REVENUE_TAGS, tenk_accn)
    g0 = sf._yoy(rev_scoped) if rev_scoped else None
    _, oi_scoped = sf._xbrl_series(facts, sf.OPERATING_INCOME_TAGS, tenk_accn)
    _, capex_scoped = sf._xbrl_series(facts, sf.CAPEX_TAGS, tenk_accn)
    operating_margin = (oi_scoped[-1]["val"] / rev_scoped[-1]["val"] * 100) if oi_scoped and rev_scoped else None
    capex_pct_revenue = (capex_scoped[-1]["val"] / rev_scoped[-1]["val"] * 100) if capex_scoped and rev_scoped else None
    base_revenue = trailing.get("ttm_revenue") or (rev_scoped[-1]["val"] if rev_scoped else None)
    tax_rate = wacc_data.get("tax_rate")
    wacc = wacc_data.get("wacc")

    dcf = reverse = sensitivity = None
    dcf_negative_fcf_warning = None
    dcf_inputs_complete = all(v is not None for v in (
        wacc, g0, base_revenue, operating_margin, capex_pct_revenue, tax_rate, net_debt, shares))
    if dcf_inputs_complete and operating_margin * (1 - tax_rate / 100) < capex_pct_revenue:
        # A real, confirmed-live outcome for TSLA (FY2025: 4.6% operating
        # margin after tax doesn't cover 9.0% capex intensity) -- not a
        # bug. Holding margin/capex % flat per the prompt's own stated
        # assumption makes near-term FCF negative, which then propagates
        # into a nonsensical-looking negative "fair value per share" once
        # compounded through the terminal value. The math is correct given
        # the stated simplification; what's missing is a caveat so a
        # negative number isn't mistaken for a bug by whoever reads it.
        dcf_negative_fcf_warning = (
            f"Warning: current after-tax operating margin ({operating_margin * (1 - tax_rate / 100):.1f}%) "
            f"does not cover current capex intensity ({capex_pct_revenue:.1f}% of revenue). Holding both flat "
            "(per this model's stated simplification) produces negative near-term FCF, so the DCF fair value "
            "below may be negative or not meaningful -- a real analysis would need to model margin recovery "
            "explicitly, which this automated pipeline doesn't attempt."
        )
    if dcf_inputs_complete and wacc > TERMINAL_GROWTH_RATE:
        growth_path = _growth_glide_path(g0, TERMINAL_GROWTH_RATE)
        dcf = _dcf_fair_value(wacc, growth_path, TERMINAL_GROWTH_RATE, base_revenue,
                               operating_margin, capex_pct_revenue, tax_rate, net_debt, shares)
        if price:
            reverse = _reverse_dcf(price, wacc, TERMINAL_GROWTH_RATE, base_revenue,
                                    operating_margin, capex_pct_revenue, tax_rate, net_debt, shares)
        sensitivity = _sensitivity_matrix(wacc, TERMINAL_GROWTH_RATE, g0, base_revenue,
                                           operating_margin, capex_pct_revenue, tax_rate, net_debt, shares)

    peer_comp = _peer_comparison(ticker, trailing)
    sop = _sum_of_parts(ticker, ex99_text, trailing.get("ev_revenue"), trailing.get("market_cap"))

    # Narratives -- compute first, narrate second; every number in these
    # prompts was already computed deterministically above.
    try:
        n15 = _generate(MULTIPLES_PROMPT.format(
            ticker=ticker, pe=_fmt1(trailing.get("pe")), ev_rev=_fmt1(trailing.get("ev_revenue")),
            ev_ebitda=_fmt1(trailing.get("ev_ebitda")), pb=_fmt1(trailing.get("pb")),
            n=historical["medians"].get("pe_n", 0),
            date_start=(historical.get("date_range") or ("N/A", "N/A"))[0],
            date_end=(historical.get("date_range") or ("N/A", "N/A"))[1],
            med_pe=_fmt1(historical["medians"].get("pe")), med_ev_rev=_fmt1(historical["medians"].get("ev_revenue")),
            med_ev_ebitda=_fmt1(historical["medians"].get("ev_ebitda")), med_pb=_fmt1(historical["medians"].get("pb")),
        ))
    except Exception as exc:
        log.warning("%s: section15 narrative failed: %s", ticker, exc)
        n15 = f"N/A -- narrative failed ({exc})"

    if peer_comp.get("peers"):
        try:
            n16 = _generate(PEER_PROMPT.format(
                ticker=ticker, own_pe=_fmt1(peer_comp.get("own_pe")), own_ev_rev=_fmt1(peer_comp.get("own_ev_revenue")),
                peer_list=", ".join(p["ticker"] for p in peer_comp["peers"]),
                peer_pe=_fmt1(peer_comp.get("peer_median_pe")), peer_ev_rev=_fmt1(peer_comp.get("peer_median_ev_revenue")),
            ))
        except Exception as exc:
            log.warning("%s: section16 narrative failed: %s", ticker, exc)
            n16 = f"N/A -- narrative failed ({exc})"
    else:
        n16 = peer_comp.get("note", "N/A -- no peers configured")

    if sop.get("sum_of_parts_ev") is not None:
        try:
            n22 = _generate(SUM_OF_PARTS_PROMPT.format(
                ticker=ticker, multiple=_fmt1(sop.get("blended_multiple_used")),
                segments=", ".join(f"{k}: ${v:,.0f}M/quarter" for k, v in sop["segments"].items()),
                sop_ev=f"${sop['sum_of_parts_ev']/1e9:.0f}B", market_cap=f"${sop['current_market_cap']/1e9:.0f}B",
                premium_discount=_fmt1(sop.get("premium_discount_pct")),
            ))
        except Exception as exc:
            log.warning("%s: section22 narrative failed: %s", ticker, exc)
            n22 = f"N/A -- narrative failed ({exc})"
    else:
        n22 = sop.get("note", "N/A")

    methodology = (
        (dcf_negative_fcf_warning + " " if dcf_negative_fcf_warning else "")
        + f"Terminal growth {TERMINAL_GROWTH_RATE}% (long-run-GDP proxy, not derived from data). "
        f"Equity risk premium {EQUITY_RISK_PREMIUM}% (fixed per spec). "
        f"Cost of debt approximated as interest expense / total debt"
        + (" (stale -- not found in the latest 10-K, using the most recent available figure)"
           if wacc_data.get("cost_of_debt_stale") else "")
        + (", could not be computed at all -- WACC excludes debt cost" if wacc_data.get("cost_of_debt_unavailable") else "")
        + f". Tax rate {'(flat fallback, computed rate was out of sane bounds)' if wacc_data.get('tax_rate_is_fallback') else '(computed from latest 10-K)'}. "
        "Unlevered FCF = EBIT*(1-tax) - capex; D&A add-back and working-capital changes omitted for simplicity. "
        "Base revenue is TTM (as of the triggering filing); operating margin and capex % of revenue held flat "
        "at the most recent fiscal year's actual figures. Sum-of-parts uses one blended EV/Revenue multiple "
        "across all segments (not segment-specific multiples), applied to annualized (x4) quarterly segment revenue."
    )

    return {
        "ticker": ticker, "accn": trigger["accn"], "filed": trigger["filed"],
        "trailing": trailing, "historical": historical, "wacc_data": wacc_data,
        "dcf": dcf, "reverse": reverse, "sensitivity": sensitivity,
        "peer_comp": peer_comp, "sop": sop,
        "n15": n15, "n16": n16, "n22": n22, "methodology": methodology,
        "current_price": price,
    }


def _fmt1(val: "float | None") -> str:
    return f"{val:.1f}" if isinstance(val, (int, float)) else "N/A"


# ---------------------------------------------------------------------------
# Notion (duplicated per-script, not shared -- established norm in this
# project: stock_daily.py/notion_sync.py/stock_earnings.py each keep their
# own copy)
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
        "title": [{"type": "text", "text": {"content": "股票估值 Valuation"}}],
        "properties": {
            "Ticker": {"title": {}},
            "Filed Date": {"date": {}},
            "Accession": {"rich_text": {}},
            "Trailing P/E": {"number": {"format": "number"}},
            "Trailing EV/Revenue": {"number": {"format": "number"}},
            "Trailing EV/EBITDA": {"number": {"format": "number"}},
            "Trailing P/B": {"number": {"format": "number"}},
            "5Y Median P/E": {"number": {"format": "number"}},
            "5Y Median EV/Revenue": {"number": {"format": "number"}},
            "Beta": {"number": {"format": "number"}},
            "WACC %": {"number": {"format": "percent"}},
            "DCF Fair Value": {"number": {"format": "number"}},
            "DCF Upside/Downside %": {"number": {"format": "percent"}},
            "Reverse DCF Implied Growth %": {"number": {"format": "percent"}},
            "Sum-of-Parts Premium/Discount %": {"number": {"format": "percent"}},
            "Multiples Commentary": {"rich_text": {}},
            "Peer Comparison": {"rich_text": {}},
            "Sensitivity Matrix": {"rich_text": {}},
            "Methodology": {"rich_text": {}},
            "Sum-of-Parts Commentary": {"rich_text": {}},
        },
    }, api_key)
    return result["id"]


def _sensitivity_table(sensitivity: "list | None") -> str:
    if not sensitivity:
        return "N/A -- DCF inputs incomplete"
    lines = ["WACC\\TermGrowth | -0.5% | 0 | +0.5%"]
    for row in sensitivity:
        vals = " | ".join(f"${v:.2f}" if v is not None else "N/A" for v in row["values"])
        lines.append(f"{row['wacc']:.1f}% | {vals}")
    return "\n".join(lines)


def _page_properties(report: dict) -> dict:
    trailing = report["trailing"]
    historical = report["historical"]["medians"]
    wacc_data = report["wacc_data"]
    dcf = report.get("dcf")
    reverse = report.get("reverse") or {}
    sop = report.get("sop") or {}

    props = {
        "Ticker": {"title": _rich(report["ticker"])},
        "Filed Date": {"date": {"start": report["filed"]}},
        "Accession": {"rich_text": _rich(report["accn"])},
        "Multiples Commentary": {"rich_text": _rich(report["n15"])},
        "Peer Comparison": {"rich_text": _rich(report["n16"])},
        "Sensitivity Matrix": {"rich_text": _rich(_sensitivity_table(report.get("sensitivity")))},
        "Methodology": {"rich_text": _rich(report["methodology"])},
        "Sum-of-Parts Commentary": {"rich_text": _rich(report["n22"])},
    }
    for key, prop in (("pe", "Trailing P/E"), ("ev_revenue", "Trailing EV/Revenue"),
                       ("ev_ebitda", "Trailing EV/EBITDA"), ("pb", "Trailing P/B")):
        if trailing.get(key) is not None:
            props[prop] = {"number": round(trailing[key], 2)}
    for key, prop in (("pe", "5Y Median P/E"), ("ev_revenue", "5Y Median EV/Revenue")):
        if historical.get(key) is not None:
            props[prop] = {"number": round(historical[key], 2)}
    if wacc_data.get("beta") is not None:
        props["Beta"] = {"number": round(wacc_data["beta"], 3)}
    if wacc_data.get("wacc") is not None:
        props["WACC %"] = {"number": round(wacc_data["wacc"] / 100, 4)}
    if dcf and report.get("current_price"):
        props["DCF Fair Value"] = {"number": round(dcf["fair_value_per_share"], 2)}
        upside = (dcf["fair_value_per_share"] - report["current_price"]) / report["current_price"] * 100
        props["DCF Upside/Downside %"] = {"number": round(upside / 100, 4)}
    if reverse.get("implied_growth") is not None:
        props["Reverse DCF Implied Growth %"] = {"number": round(reverse["implied_growth"] / 100, 4)}
    if sop.get("premium_discount_pct") is not None:
        props["Sum-of-Parts Premium/Discount %"] = {"number": round(sop["premium_discount_pct"] / 100, 4)}
    return props


# ---------------------------------------------------------------------------
# Trigger poll (mirrors stock_earnings.py's poll_and_generate almost
# exactly -- own state file, so disabling/re-enabling this script
# independently of stock_earnings.py doesn't silently inherit its cursor)
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


def poll_and_generate() -> int:
    cfg = load_config()
    if not cfg or not cfg.get("api_key") or not cfg.get("valuation_database_id"):
        log.info("not configured (%s); skipping", CONFIG)
        return 0

    state = _load_json(WATCH_STATE, {})
    today = dt.datetime.now(NZ_TZ).date()
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
            hit = se._latest_earnings_8k(subs)
            if not hit:
                continue
            already = state.get(ticker, {}).get("last_processed_accn")
            if hit["accn"] == already:
                continue
            filed_date = dt.date.fromisoformat(hit["filed"])
            if (today - filed_date).days > STALE_DAYS:
                log.info("%s: latest earnings 8-K (%s) is older than %d days, skipping (first-run guard)",
                          ticker, hit["filed"], STALE_DAYS)
                state.setdefault(ticker, {})["last_processed_accn"] = hit["accn"]
                continue

            report = build_valuation_report(ticker, cik, subs, hit)
            _notion("POST", "/pages", {
                "parent": {"database_id": cfg["valuation_database_id"]},
                "properties": _page_properties(report),
            }, cfg["api_key"])
            state.setdefault(ticker, {})["last_processed_accn"] = hit["accn"]
            state[ticker]["last_processed_filed"] = hit["filed"]
            written += 1
            log.info("%s: generated valuation report for accession %s (filed %s)", ticker, hit["accn"], hit["filed"])
        except Exception as exc:
            log.error("%s: valuation report failed: %s", ticker, exc)

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
        cfg["valuation_database_id"] = db_id
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print(f"database created and saved to config: {db_id}")
        return
    try:
        poll_and_generate()
    except Exception as exc:
        log.error("run failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

