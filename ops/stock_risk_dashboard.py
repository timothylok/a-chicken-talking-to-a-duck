"""Category 6: a 10-KPI composite risk-score dashboard (0-1 per KPI, a
Red/Amber/Green traffic light per KPI, composite = round(10 * mean of the
available KPIs, 1)). Generated daily for the whole watchlist -- unlike
Category 2/3/5, this is a pure snapshot job, not reactive: valuation and
technical inputs move every trading day even when nothing new has been
filed with the SEC, so there's nothing to "detect changed."

Runs daily via the "VoiceOS Risk Dashboard Daily" scheduled task, 10:00
NZT (staggered 30 min after "VoiceOS Technicals Daily" 09:30, same
stagger convention as Category 2 -> 3). Writes one Notion page per ticker
per day (append-only, same as every other category -- no script in this
project ever updates/upserts a Notion page). A separate Next.js app
(dashboard/) reads this database to render the actual dashboard; this
script only writes.

Scoring engine: reuses the deterministic helpers already built for
Category 1/2/3/4/5 (imported and called directly, in-process) rather than
calling those categories' own build_report()/poll_and_generate() entry
points -- those trigger LLM narration, Notion writes, and reactive
state-file dedupe that would be wrong to re-trigger from a daily snapshot
job. Where a category's own section function bundles a deterministic
number together with an LLM narrative call (stock_technicals.py's
_section_weekly/_section_daily/_section_volume), this script recomputes
just the small deterministic piece itself instead of calling the
LLM-wrapped version -- thin, deliberate duplication, matching this
project's already-established norm (e.g. _current_price is independently
duplicated in stock_fundamentals.py and stock_valuation.py). The one LLM
call this script does make is a single 3-sentence per-ticker summary,
fed only the already-computed KPI scores/lights/detail strings -- it
narrates locked-in numbers, it never re-judges them.

KPI 9 ("Market & Sector-Relative Pressure") blends the ticker's own
3-month return vs SPY with its GICS sector ETF's (ops/sectors.json) --
real sector data since 2026-10-03, when it replaced a peer-multiple-spread
proxy. The same run computes a market-wide sector rotation table (11 SPDR
ETFs, 1/3/6-month relative strength, a market-implied regime and a
rule-based overweight/underweight call) to dashboard/data/sectors.json.
There is still no macro data feed (ISM, credit spreads, claims), so the
regime is labelled market-implied, never presented as an economic read.

Tickers with no SEC filings (sf.EXCLUDE_NO_SEC_FILINGS, currently only
SPCX) get every SEC-dependent KPI (1,2,3,4,5,6,10) marked N/A rather than
guessed -- they still get a dashboard row, just with a low Coverage
count. KPI 7 (technical) and KPI 9 (market/sector-relative, weighted 100%
onto its own relative-strength term when no sector is mapped) still
compute, since Category 4 already proved SPCX's thin trading history is
still usable for pure price-based technicals.

Setup:
  1. Requires ops/notion.json already configured with api_key +
     database_id (see notion_sync.py's own --setup).
  2. python ops/stock_risk_dashboard.py --setup
     (creates the "股票風險評分 Risk Dashboard" database under the same
     parent page as the existing Voice History database, adds
     risk_dashboard_database_id to the config)

Until risk_dashboard_database_id exists in the config, scheduled runs
are silent no-ops (--tickers still computes/prints if configured).
"""

import argparse
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
import stock_valuation as sv  # noqa: E402
import stock_technicals as st  # noqa: E402
import stock_risk_flags as srf  # noqa: E402

# Narration runs on Cloudflare Workers AI (sf.WORKERS_AI_MODEL), not local
# Ollama -- see sf.workers_ai_generate for why.

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "asr", "logs", f"stock_risk_dashboard-{dt.date.today():%Y-%m-%d}.log")
CONFIG = os.path.join(ROOT, "ops", "notion.json")
NOTION_VERSION = "2022-06-28"
NZ_TZ = ZoneInfo("Pacific/Auckland")

# The dashboard/ Next.js app reads this file directly at build time (no
# live Notion API call from the deployed page, no Notion token in Vercel
# env) -- generate here, then `vercel --prod` from dashboard/ to publish
# a fresh static build. The Notion write above is kept purely as a
# human-browsable historical log, same role Notion plays for every other
# category -- it is no longer what the live dashboard page reads from.
DASHBOARD_DATA_PATH = os.path.join(ROOT, "dashboard", "data", "latest.json")

WATCHLIST = [
    t.strip().upper()
    for t in os.environ.get("STOCK_WATCHLIST", "AAPL,MSFT,NVDA,TSLA,GOOGL,AMZN,META,AON,SPCX").split(",")
    if t.strip()
]

# Traffic-light thresholds, applied consistently to every KPI and the
# composite. A fresh tertile split -- no existing category has a 3-way
# continuous threshold to inherit (Cat1/Cat5's own flags are binary).
GREEN_THRESHOLD = 0.70
AMBER_THRESHOLD = 0.40

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
# logging.basicConfig() would be a silent no-op here -- importing
# stock_fundamentals above already configured the root logger via its own
# basicConfig() call (same gotcha every other Category script hit).
log = logging.getLogger("stock_risk_dashboard")
log.propagate = False
log.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------

def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _lerp_score(val: "float | None", low: float, low_score: float, high: float, high_score: float) -> "float | None":
    # Linear interpolation between two (value, score) anchor points,
    # clamped beyond either end. low/high can be in either order.
    if val is None:
        return None
    lo_v, hi_v = (low, high) if low <= high else (high, low)
    lo_s, hi_s = (low_score, high_score) if low <= high else (high_score, low_score)
    if val <= lo_v:
        return lo_s
    if val >= hi_v:
        return hi_s
    frac = (val - lo_v) / (hi_v - lo_v)
    return lo_s + frac * (hi_s - lo_s)


def _light(score: "float | None") -> "str | None":
    if score is None:
        return None
    if score >= GREEN_THRESHOLD:
        return "Green"
    if score >= AMBER_THRESHOLD:
        return "Amber"
    return "Red"


def _kpi(score: "float | None", detail: str) -> dict:
    if score is not None:
        score = round(_clamp01(score), 3)
    return {"score": score, "light": _light(score), "detail": detail}


def _kpi_na(reason: str) -> dict:
    return {"score": None, "light": None, "detail": f"N/A -- {reason}"}


# ---------------------------------------------------------------------------
# Shared per-ticker SEC data bundle -- one fetch pass reused across
# KPIs 1/3/4/5/6/10, mirroring exactly what sf.build_report()/
# sv.build_valuation_report() each already assemble for their own reports.
# ---------------------------------------------------------------------------

def _sec_bundle(ticker: str) -> "dict | None":
    if ticker in sf.EXCLUDE_NO_SEC_FILINGS:
        return None
    cik = sf._cik_for_ticker(ticker)
    if not cik:
        log.warning("%s: no CIK found", ticker)
        return None
    subs = sf._submissions(cik)
    tenk = sf._latest_filing(subs, "10-K")
    if not tenk:
        log.warning("%s: no 10-K found", ticker)
        return None
    facts = sf._company_facts(cik)
    accn, filed = tenk["accn"], tenk["filed"]

    _, rev = sf._xbrl_series(facts, sf.REVENUE_TAGS, accn)
    _, ni = sf._xbrl_series(facts, sf.NET_INCOME_TAGS, accn)
    _, gp = sf._xbrl_series(facts, sf.GROSS_PROFIT_TAGS, accn)
    _, oi = sf._xbrl_series(facts, sf.OPERATING_INCOME_TAGS, accn)
    _, ocf = sf._xbrl_series(facts, sf.OCF_TAGS, accn)
    _, capex = sf._xbrl_series(facts, sf.CAPEX_TAGS, accn)
    _, repurchases = sf._xbrl_series(facts, sf.REPURCHASE_TAGS, accn)
    _, dividends = sf._xbrl_series(facts, sf.DIVIDEND_TAGS, accn)
    _, acquisitions = sf._xbrl_series(facts, sf.ACQUISITION_TAGS, accn)
    _, equity_series = sf._xbrl_series(facts, sf.STOCKHOLDERS_EQUITY_TAGS, accn)
    _, cur_assets_series = sf._xbrl_series(facts, sf.CURRENT_ASSETS_TAGS, accn)
    _, cur_liab_series = sf._xbrl_series(facts, sf.CURRENT_LIABILITIES_TAGS, accn)
    fcf = sf._fcf_series(ocf, capex)
    debt = sf._total_debt(facts, accn)
    cash, sti = sf._cash_and_sti(facts, accn)
    equity = equity_series[-1]["val"] if equity_series else None
    cur_assets = cur_assets_series[-1]["val"] if cur_assets_series else None
    cur_liab = cur_liab_series[-1]["val"] if cur_liab_series else None

    return {
        "cik": cik, "subs": subs, "facts": facts, "accn": accn, "filed": filed,
        "tenk_url": sf._filing_url(cik, accn, tenk["doc"]),
        "rev": rev, "ni": ni, "gp": gp, "oi": oi, "ocf": ocf, "capex": capex,
        "repurchases": repurchases, "dividends": dividends, "acquisitions": acquisitions,
        "fcf": fcf, "debt": debt, "cash": cash, "sti": sti, "equity": equity,
        "cur_assets": cur_assets, "cur_liab": cur_liab,
    }


# ---------------------------------------------------------------------------
# KPI 1: Fundamental Health
# ---------------------------------------------------------------------------

def _kpi_fundamental_health(bundle: "dict | None") -> dict:
    if bundle is None:
        return _kpi_na("no SEC filings for this ticker")
    _, cash_conversion_pct = sf._section_quality_of_earnings(bundle["ni"], bundle["fcf"])
    fcf_val = bundle["fcf"][-1]["val"] if bundle["fcf"] else None
    returned = sum(x for x in (
        bundle["repurchases"][-1]["val"] if bundle["repurchases"] else None,
        bundle["dividends"][-1]["val"] if bundle["dividends"] else None,
    ) if x)
    funding_score = None
    if fcf_val is not None:
        # Organic: FCF alone covers what was returned to shareholders.
        # Anything above FCF was necessarily funded by debt or a cash
        # drawdown -- same "returned vs reinvested" arithmetic
        # sf._section_capital_allocation() already does, recomputed here
        # directly since that function only returns an HTML string.
        funding_score = 1.0 if returned <= max(fcf_val, 0) else 0.2
    conversion_score = _lerp_score(cash_conversion_pct, 60, 0.0, 90, 1.0) if cash_conversion_pct is not None else None
    parts = [s for s in (funding_score, conversion_score) if s is not None]
    if not parts:
        return _kpi_na("insufficient cash-flow/earnings data for this filing")
    detail = (
        f"Funding: {'organic (FCF-covered)' if funding_score == 1.0 else 'debt/cash-funded' if funding_score is not None else 'N/A'}; "
        f"cash conversion (FCF/NI): {cash_conversion_pct:.0f}%" if cash_conversion_pct is not None else "cash conversion: N/A"
    )
    return _kpi(sum(parts) / len(parts), detail)


# ---------------------------------------------------------------------------
# KPI 2: Earnings Quality & Surprise Stability
# ---------------------------------------------------------------------------

def _kpi_earnings_quality(ticker: str) -> dict:
    history = _load_json(se.EARNINGS_HISTORY, {})
    filings = history.get(ticker, {}).get("filings", [])
    recorded = [
        f for f in filings
        if f.get("eps_actual") is not None and f.get("eps_consensus")
    ][-8:]
    if not recorded:
        return _kpi_na("no earnings-history entries yet (asr/logs/earnings_history.json)")

    # A surprise beyond +/-100% is never a real beat for a mega-cap -- it means
    # the recorded actual and the Street consensus are on different bases.
    # Filers that publish a non-GAAP EPS are handled upstream (stock_earnings.py's
    # _consensus_basis_eps); the ones left are filers that publish only GAAP
    # while carrying a huge one-off investment gain the Street excludes and
    # never restates per share (GOOGL Q2-26: GAAP $9.11 includes a disclosed
    # $6.26/share equity-securities gain, against a $2.88 consensus; AMZN
    # Q2-26: $53.4bn of non-operating other income, quantified only pre-tax
    # and in dollars, so no comparable per-share figure exists at all).
    # Scoring those as +200% beats put two fake Greens on the live dashboard.
    # Dropped from the stability spread too, not just the headline surprise --
    # one such quarter would otherwise dominate the standard deviation. N/A
    # with the reason is the honest answer, the same house rule the price
    # helpers follow: return None rather than a guessed number.
    def _surprise(f):
        return (f["eps_actual"] - f["eps_consensus"]) / abs(f["eps_consensus"]) * 100

    usable = [f for f in recorded if abs(_surprise(f)) <= 100]
    if not usable:
        latest = recorded[-1]
        return _kpi_na(
            f"latest actual (EPS {latest['eps_actual']:.2f}) is not comparable to the "
            f"{latest['eps_consensus']:.2f} consensus -- a {_surprise(latest):+.0f}% gap means "
            "a GAAP-vs-adjusted basis mismatch, most likely a one-off investment gain "
            "the filer never restated per share"
        )
    latest = usable[-1]
    surprise_pct = _surprise(latest)
    base_score = _lerp_score(surprise_pct, -5, 0.2, 5, 1.0)
    base_score = 0.7 if -2 <= surprise_pct <= 2 else base_score

    n = len(usable)
    stability_score = None
    if n >= 2:
        ratios = [(f["eps_actual"] / f["eps_consensus"]) - 1 for f in usable]
        spread = statistics.pstdev(ratios)
        # A tighter spread of surprise ratios = more stable/predictable
        # earnings. 0.05 (5% typical surprise) -> 1.0, 0.30+ -> 0.0.
        stability_score = _lerp_score(spread, 0.05, 1.0, 0.30, 0.0)

    weight = min(1.0, n / 4)
    if stability_score is None:
        score = base_score
    else:
        score = (1 - weight) * base_score + weight * stability_score
    detail = (
        f"Latest quarter surprise: {surprise_pct:+.1f}% (EPS {latest['eps_actual']:.2f} vs "
        f"consensus {latest['eps_consensus']:.2f}). Stability based on {n} quarter(s) of history "
        f"-- {'thin, low-confidence' if n < 4 else 'reasonable'} sample."
    )
    return _kpi(score, detail)


# ---------------------------------------------------------------------------
# KPI 3: Revenue & Margin Trajectory
# ---------------------------------------------------------------------------

def _kpi_revenue_margin(bundle: "dict | None") -> dict:
    if bundle is None:
        return _kpi_na("no SEC filings for this ticker")
    revenue_yoy = sf._yoy(bundle["rev"]) if bundle["rev"] else None
    _, biggest_bps = sf._section_margins(bundle["rev"], bundle["gp"], bundle["oi"], bundle["ni"])
    rev_score = _lerp_score(revenue_yoy, 0, 0.0, 15, 1.0) if revenue_yoy is not None else None
    margin_score = _lerp_score(biggest_bps, -200, 0.0, 200, 1.0) if biggest_bps is not None else None
    parts = [s for s in (rev_score, margin_score) if s is not None]
    if not parts:
        return _kpi_na("insufficient multi-year revenue/margin data for this filing")
    detail = (
        f"Revenue YoY: {revenue_yoy:+.1f}%" if revenue_yoy is not None else "Revenue YoY: N/A"
    ) + "; " + (
        f"biggest margin swing: {biggest_bps:+.0f} bps" if biggest_bps is not None else "margin swing: N/A"
    )
    return _kpi(sum(parts) / len(parts), detail)


# ---------------------------------------------------------------------------
# KPI 4: Balance Sheet & Debt Maturity Risk
# ---------------------------------------------------------------------------

def _kpi_balance_sheet_debt(bundle: "dict | None") -> "tuple[dict, bool | None]":
    # Returns (KpiResult, debt_maturity_flagged) -- the flag is handed back
    # so KPI10 can deliberately NOT re-penalize the same refinancing risk.
    if bundle is None:
        return _kpi_na("no SEC filings for this ticker"), None
    _, leverage, net_cash = sf._section_balance_sheet(
        bundle["cash"], bundle["sti"], bundle["debt"], bundle["equity"],
        bundle["cur_assets"], bundle["cur_liab"], bundle["accn"], bundle["filed"],
    )
    debt_maturity = srf._section_debt_maturity(bundle["facts"], bundle["accn"])
    flagged = debt_maturity.get("flagged")

    if leverage is None and net_cash is None:
        return _kpi_na("leverage/net-cash data unavailable for this filing"), flagged

    if net_cash is not None and net_cash >= 0:
        score = 1.0
    elif leverage is not None:
        score = _lerp_score(leverage, 0.5, 1.0, 3.0, 0.0)
    else:
        score = 0.5
    if flagged:
        score = min(score, 0.2)  # severity override, not averaged -- refinancing risk is a hard cap

    detail = (
        f"Leverage (Debt/Equity): {leverage:.2f}" if leverage is not None else "Leverage: N/A"
    ) + f"; {'net cash positive' if (net_cash or 0) >= 0 and net_cash is not None else 'net debt position'}"
    if flagged:
        detail += f"; REFINANCING RISK: {debt_maturity.get('basis', '')}"
    return _kpi(score, detail), flagged


# ---------------------------------------------------------------------------
# KPI 5: Cash Flow & Dividend Coverage
# ---------------------------------------------------------------------------

def _kpi_cash_flow_dividend(bundle: "dict | None") -> dict:
    if bundle is None:
        return _kpi_na("no SEC filings for this ticker")
    fcf_val = bundle["fcf"][-1]["val"] if bundle["fcf"] else None
    returned = sum(x for x in (
        bundle["repurchases"][-1]["val"] if bundle["repurchases"] else None,
        bundle["dividends"][-1]["val"] if bundle["dividends"] else None,
    ) if x)
    if fcf_val is None:
        return _kpi_na("free cash flow unavailable for this filing")
    # Capex strain: share of operating cash flow consumed by capex. A
    # non-payer used to get a flat 0.7 here, which scored AMZN (capex 94%
    # of OCF) the same as GOOGL (55%).
    fcf_end = bundle["fcf"][-1]["end"]
    ocf_val = next((i["val"] for i in bundle["ocf"] if i["end"] == fcf_end), None)
    capex_val = next((i["val"] for i in bundle["capex"] if i["end"] == fcf_end), None)
    strain = capex_val / ocf_val if ocf_val and ocf_val > 0 else None
    strain_score = _lerp_score(strain, 0.5, 1.0, 1.0, 0.2) if strain is not None else 0.0
    strain_detail = (f"Capex uses {strain:.0%} of operating cash flow." if strain is not None
                     else "Operating cash flow not positive -- capex unfunded by operations.")
    if returned <= 0:
        return _kpi(strain_score, f"{strain_detail} No buybacks/dividends this year.")
    coverage = fcf_val / returned
    coverage_score = _lerp_score(coverage, 1.0, 0.2, 1.5, 1.0)
    detail = (f"{strain_detail} FCF covers {coverage:.1f}x this year's buybacks+dividends "
              f"({sf._fmt_usd(fcf_val)} FCF vs {sf._fmt_usd(returned)} returned).")
    return _kpi((strain_score + coverage_score) / 2, detail)


# ---------------------------------------------------------------------------
# KPI 6: Valuation vs History & Peers
# ---------------------------------------------------------------------------

def _kpi_valuation(ticker: str, bundle: "dict | None") -> dict:
    if bundle is None:
        return _kpi_na("no SEC filings for this ticker")
    facts, tenk_accn = bundle["facts"], bundle["accn"]
    price = sf._current_price(ticker)
    _, shares_series = sf._xbrl_series(facts, sv.DILUTED_SHARES_TAGS, tenk_accn, unit="shares")
    shares = shares_series[-1]["val"] if shares_series else None
    net_debt = sv._net_debt_at(facts, dt.date.today().isoformat())
    trailing = sv._trailing_multiples(facts, tenk_accn, price, shares, net_debt)

    try:
        weekly_prices = sv._historical_prices(ticker, "5y", "1wk")
        historical = sv._historical_multiples(facts, weekly_prices)
    except Exception as exc:
        log.warning("%s: historical multiples fetch failed: %s", ticker, exc)
        historical = {"medians": {}}

    peer_comparison = sv._peer_comparison(ticker, trailing)

    def discount_score(own: "float | None", ref: "float | None") -> "float | None":
        # -20% or more below ref -> 1.0, at ref -> 0.5, +20% or more above -> 0.0
        if own is None or not ref:
            return None
        pct = (own - ref) / ref * 100
        return _lerp_score(pct, -20, 1.0, 20, 0.0)

    own_pe = trailing.get("pe")
    hist_score = discount_score(own_pe, historical.get("medians", {}).get("pe"))
    peer_score = discount_score(own_pe, peer_comparison.get("peer_median_pe"))

    if hist_score is None and peer_score is None:
        return _kpi_na("no trailing/historical/peer P/E available to compare")
    if peer_score is None:
        # peer_comparison["note"] distinguishes "no peers configured in
        # ops/peers.json" from "peers configured but their data couldn't be
        # fetched" (sv._peer_comparison sets a specific note for each) --
        # don't collapse both into one message, they're different situations.
        reason = peer_comparison.get("note") or "peer comparison unavailable"
        score, weight_note = hist_score, f"own 5yr history only ({reason})"
    else:
        score = statistics.mean([s for s in (hist_score, peer_score) if s is not None])
        weight_note = "own 5yr history + peer group"
    detail = f"Trailing P/E {own_pe:.1f}x" if own_pe else "Trailing P/E: N/A"
    detail += f"; compared against {weight_note}."
    return _kpi(score, detail)


# ---------------------------------------------------------------------------
# KPI 7: Technical Trend & Momentum -- deliberately 100% deterministic.
# Category 4's own daily Long/Short/Wait verdict is LLM-derived
# (_section_daily calls the local model); calling that function directly
# would trigger a second, redundant LLM call ~30 min after Category 4's
# own 09:30 run already made one for the same trading day. This computes
# an equivalent deterministic proxy instead, from the same SMA/RSI inputs.
# ---------------------------------------------------------------------------

def _technical_verdict_score(price, sma50, sma200, rsi) -> "float | None":
    if price is None or sma50 is None or sma200 is None:
        return None
    if price > sma50 > sma200 and (rsi is None or rsi < 75):
        return 1.0  # Long-leaning
    if price < sma50 < sma200 and (rsi is None or rsi > 25):
        return 0.0  # Short-leaning
    return 0.5  # Wait / mixed


def _kpi_technical_momentum(daily: dict, weekly: dict) -> dict:
    closes = daily.get("closes") or []
    price = daily.get("price")
    sma50, sma200 = st._sma(closes, 50), st._sma(closes, 200)
    rsi = st._rsi(closes, 14)
    base = _technical_verdict_score(price, sma50, sma200, rsi)
    if base is None:
        return _kpi_na("insufficient daily price history for SMA50/200")

    volumes = daily.get("volumes") or []
    obv = st._obv(closes, volumes)
    price_chg = st._pct_return(closes, 20)
    obv_adjust = 0.0
    if len(obv) > 20 and price_chg is not None:
        obv_chg = obv[-1] - obv[-21]
        confirming = (price_chg > 0) == (obv_chg > 0)
        obv_adjust = 0.15 if confirming else -0.15

    weekly_closes = weekly.get("closes") or []
    ema50w, ema200w = st._ema(weekly_closes, 50), st._ema(weekly_closes, 200)
    weekly_adjust = 0.0
    if ema50w is not None and ema200w is not None:
        weekly_bullish = ema50w > ema200w
        daily_bullish = base >= 0.5
        if weekly_bullish != daily_bullish:
            # Weekly trend disagrees with the daily-derived verdict --
            # widen toward uncertainty rather than trust the shorter frame.
            base = base + (0.5 - base) * 0.2

    score = base + obv_adjust
    setup = "Long-leaning" if base >= 0.75 else "Short-leaning" if base <= 0.25 else "Mixed/Wait"
    detail = (
        f"{setup} (price {price:.2f} vs SMA50 {sma50:.2f}/SMA200 {sma200:.2f}, RSI {rsi:.0f})"
        if rsi is not None else f"{setup} (price {price:.2f} vs SMA50 {sma50:.2f}/SMA200 {sma200:.2f})"
    )
    return _kpi(score, detail)


# ---------------------------------------------------------------------------
# KPI 8: Volatility & Event Risk -- historical-realized only (Yahoo's
# options endpoint is confirmed unauthenticated-401, same limitation
# stock_technicals.py already documents; no implied vol is invented here).
# ---------------------------------------------------------------------------

def _kpi_volatility_event(ticker: str, daily: dict, spy_daily: dict) -> dict:
    vol = st._section_earnings_volatility(ticker, daily)
    avg_move = vol.get("avg_move_pct")
    if avg_move is None:
        return _kpi_na(vol.get("basis", "no historical earnings-day volatility available"))
    score = _lerp_score(avg_move, 3, 1.0, 8, 0.0)
    rs = st._section_relative_strength(ticker, daily, spy_daily)
    diff3 = rs.get("rs_3m_diff")
    if diff3 is not None and diff3 < -10:
        score -= min(0.2, abs(diff3) / 100)
    detail = f"Avg realized earnings-day move: {avg_move:.1f}% ({vol.get('basis', '')})"
    return _kpi(score, detail)


# ---------------------------------------------------------------------------
# Sector rotation ("Sector Rotation Signal" prompt) -- market-wide, computed
# once per run. 100% deterministic, no LLM. There is still no ISM, credit
# spread or claims data anywhere in this codebase, so the regime is labelled
# market-implied: cyclical-vs-defensive sector leadership plus the Treasury
# 10Y-3M curve. The overweight/underweight call is a fixed rule (playbook
# fit AND 3+6-month relative strength), not a forecast.
# ---------------------------------------------------------------------------

SECTORS_PATH = os.path.join(ROOT, "ops", "sectors.json")
SECTORS_DATA_PATH = os.path.join(ROOT, "dashboard", "data", "sectors.json")

SECTOR_ETFS = {
    "XLK": "Technology", "XLC": "Communication Services", "XLY": "Consumer Discretionary",
    "XLF": "Financials", "XLI": "Industrials", "XLB": "Materials", "XLE": "Energy",
    "XLRE": "Real Estate", "XLV": "Health Care", "XLP": "Consumer Staples", "XLU": "Utilities",
}
CYCLICALS = {"XLK", "XLC", "XLY", "XLF", "XLI", "XLB", "XLE"}
DEFENSIVES = {"XLV", "XLP", "XLU", "XLRE"}

# Conventional business-cycle sector playbook (the Fidelity-style map most
# rotation write-ups use). "Slowdown" = defensives leading on a positive
# curve, which reads either as a slowing expansion or an early recovery.
REGIME_FAVOURED = {
    "Expansion": {"XLK", "XLC", "XLI", "XLY", "XLF"},
    "Late-cycle": {"XLE", "XLB", "XLV", "XLP"},
    "Slowdown": {"XLV", "XLP", "XLU", "XLK"},
    "Contraction risk": {"XLP", "XLU", "XLV"},
}


def _sector_map() -> dict:
    return {k: v for k, v in _load_json(SECTORS_PATH, {}).items() if not k.startswith("_")}


def _regime(cyc_minus_def: "float | None", curve: "float | None") -> "tuple[str | None, str]":
    if cyc_minus_def is None:
        return None, "N/A -- sector relative strength unavailable"
    lead = "cyclicals" if cyc_minus_def > 0 else "defensives"
    curve_txt = f"10Y-3M curve {curve:+.2f}pp" if curve is not None else "10Y-3M curve unavailable"
    if curve is None:
        label = "Expansion" if cyc_minus_def > 0 else "Slowdown"
    elif cyc_minus_def > 0:
        label = "Expansion" if curve >= 0 else "Late-cycle"
    else:
        label = "Slowdown" if curve >= 0 else "Contraction risk"
    return label, f"{lead} leading by {abs(cyc_minus_def):.1f}pp over 3 months; {curve_txt}"


def sector_rotation(spy_daily: dict) -> dict:
    rows = []
    for etf, name in SECTOR_ETFS.items():
        try:
            closes = st._fetch_series(etf, "1y", "1d")["closes"]
        except Exception as exc:
            log.warning("%s: sector fetch failed: %s", etf, exc)
            continue
        row = {"etf": etf, "name": name, "group": "Cyclical" if etf in CYCLICALS else "Defensive"}
        for key, days in (("rs1m", 21), ("rs3m", 63), ("rs6m", 126)):
            own, spy = st._pct_return(closes, days), st._pct_return(spy_daily["closes"], days)
            row[key] = round(own - spy, 2) if own is not None and spy is not None else None
        rows.append(row)

    def avg(group):
        vals = [r["rs3m"] for r in rows if r["etf"] in group and r["rs3m"] is not None]
        return statistics.mean(vals) if vals else None

    cyc, dfn = avg(CYCLICALS), avg(DEFENSIVES)
    ten, three = sv._treasury_10yr_yield(), sv._treasury_10yr_yield(col="3 Mo")
    curve = round(ten - three, 2) if ten is not None and three is not None else None
    regime, basis = _regime(cyc - dfn if cyc is not None and dfn is not None else None, curve)
    favoured = REGIME_FAVOURED.get(regime, set())
    for r in rows:
        strong = r["rs3m"] is not None and r["rs6m"] is not None and r["rs3m"] > 0 and r["rs6m"] > 0
        weak = r["rs3m"] is not None and r["rs6m"] is not None and r["rs3m"] < 0 and r["rs6m"] < 0
        r["favoured"] = r["etf"] in favoured
        r["call"] = ("Overweight" if r["favoured"] and strong else
                     "Underweight" if not r["favoured"] and weak else "Neutral")
    rows.sort(key=lambda r: -(r["rs3m"] if r["rs3m"] is not None else -999))
    return {
        "generatedAt": dt.datetime.now(NZ_TZ).strftime("%Y-%m-%d %H:%M"),
        "regime": regime, "regimeBasis": basis, "curve10y3m": curve,
        "sectors": rows,
    }


def write_sector_snapshot(snapshot: dict) -> None:
    if not snapshot["sectors"]:
        log.warning("no sector data fetched; keeping the previous sectors.json")
        return
    os.makedirs(os.path.dirname(SECTORS_DATA_PATH), exist_ok=True)
    with open(SECTORS_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    log.info("sector regime %s (%s); wrote %s", snapshot["regime"], snapshot["regimeBasis"], SECTORS_DATA_PATH)


# ---------------------------------------------------------------------------
# KPI 9: Market & Sector-Relative Pressure -- the ticker's own 3-month return
# vs SPY, plus its GICS sector ETF's 3-month return vs SPY (a sector
# tailwind or headwind). Replaced the peer-multiple-spread proxy 2026-10-03.
# ---------------------------------------------------------------------------

def _kpi_market_sector_pressure(ticker: str, daily: dict, spy_daily: dict, sectors: dict) -> dict:
    rs = st._section_relative_strength(ticker, daily, spy_daily)
    diff3 = rs.get("rs_3m_diff")
    market_score = _lerp_score(diff3, -15, 0.0, 15, 1.0) if diff3 is not None else None

    etf = _sector_map().get(ticker)
    sector = next((r for r in sectors.get("sectors", []) if r["etf"] == etf), None) if etf else None
    sector_rs = sector["rs3m"] if sector else None
    sector_score = _lerp_score(sector_rs, -10, 0.0, 10, 1.0) if sector_rs is not None else None

    if market_score is None and sector_score is None:
        return _kpi_na("insufficient relative-strength data for this ticker or its sector")
    if sector_score is None:
        score = market_score
    elif market_score is None:
        score = sector_score
    else:
        score = 0.6 * market_score + 0.4 * sector_score
    detail = (
        (f"Market-relative (3mo vs SPY): {diff3:+.1f}pp" if diff3 is not None else "Market-relative: N/A")
        + "; sector "
        + (f"{etf} ({SECTOR_ETFS[etf]}) {sector_rs:+.1f}pp vs SPY" if sector_rs is not None else
           f"N/A ({'not mapped in ops/sectors.json' if not etf else etf + ' data unavailable'})")
    )
    return _kpi(score, detail)


# ---------------------------------------------------------------------------
# Mean reversion scanner ("Mean Reversion Scanner" prompt) -- the watchlist
# plus every ops/peers.json peer, ranked by how far each sits from its own
# norms. 100% deterministic, no LLM. "Sector median" EV/EBITDA is the median
# of the ticker's peers.json group(s), computed from this same scan, so it
# costs no extra fetches. Value trap vs candidate is a fixed rule: cheap or
# oversold with shrinking TTM revenue reads as a possible trap.
# ---------------------------------------------------------------------------

REVERSION_DATA_PATH = os.path.join(ROOT, "dashboard", "data", "reversion.json")
REVERSION_TOP_N = 5
REVERSION_MIN_PEERS = 3


def _reversion_universe() -> "tuple[list, dict]":
    """(tickers, groups): watchlist + all peers, and each anchor's peer group."""
    peers = {k: v for k, v in _load_json(sv.PEERS_CONFIG, {}).items() if not k.startswith("_")}
    groups = {anchor: [anchor, *members] for anchor, members in peers.items()}
    tickers = [t for t in WATCHLIST if t not in sf.EXCLUDE_NO_SEC_FILINGS]
    for members in groups.values():
        tickers += [t for t in members if t not in tickers]
    return tickers, groups


def _reversion_metrics(ticker: str) -> "dict | None":
    cik = sf._cik_for_ticker(ticker)
    if not cik:
        return None
    facts = sf._company_facts(cik)
    tenk = sf._latest_filing(sf._submissions(cik), "10-K")
    if not tenk:
        return None
    price = sf._current_price(ticker)
    _, shares_series = sf._xbrl_series(facts, sv.DILUTED_SHARES_TAGS, tenk["accn"], unit="shares")
    shares = shares_series[-1]["val"] if shares_series else None
    trailing = sv._trailing_multiples(facts, tenk["accn"], price, shares,
                                      sv._net_debt_at(facts, dt.date.today().isoformat()))
    historical = sv._historical_multiples(facts, sv._historical_prices(ticker, "5y", "1wk"))
    closes = st._fetch_series(ticker, "2y", "1d")["closes"]
    sma200 = st._sma(closes, 200)
    return {
        "ticker": ticker,
        "pe": trailing.get("pe"), "pe_5y_median": historical.get("medians", {}).get("pe"),
        "ev_ebitda": trailing.get("ev_ebitda"),
        "revenue_growth": trailing.get("revenue_growth"), "eps_growth": trailing.get("eps_growth"),
        "rsi": st._rsi(closes, 14),
        "vs_sma200": (closes[-1] / sma200 - 1) * 100 if closes and sma200 else None,
    }


def _clamp(x: float, lim: float = 1.5) -> float:
    return max(-lim, min(lim, x))


def _reversion_score(m: dict, group_median_ev: "float | None") -> dict:
    pe_dev = ((m["pe"] / m["pe_5y_median"] - 1) * 100
              if m["pe"] and m["pe_5y_median"] and m["pe"] > 0 and m["pe_5y_median"] > 0 else None)
    ev_dev = ((m["ev_ebitda"] / group_median_ev - 1) * 100
              if m["ev_ebitda"] and group_median_ev and m["ev_ebitda"] > 0 else None)
    # Each deviation is scaled to roughly -1..+1 at its own "extreme" level
    # (50% off a multiple, 20% off the 200-day, RSI 30/70), so no single
    # metric dominates the blend. Negative = cheap/oversold, positive = rich.
    parts = [x for x in (
        _clamp(pe_dev / 50) if pe_dev is not None else None,
        _clamp(ev_dev / 50) if ev_dev is not None else None,
        _clamp(m["vs_sma200"] / 20) if m["vs_sma200"] is not None else None,
        _clamp((m["rsi"] - 50) / 20) if m["rsi"] is not None else None,
    ) if x is not None]
    extremes = [name for name, hit in (
        ("P/E vs 5y", pe_dev is not None and abs(pe_dev) >= 30),
        ("EV/EBITDA vs peers", ev_dev is not None and abs(ev_dev) >= 30),
        ("price vs 200-day", m["vs_sma200"] is not None and abs(m["vs_sma200"]) >= 15),
        ("RSI", m["rsi"] is not None and (m["rsi"] < 30 or m["rsi"] > 70)),
    ) if hit]
    score = statistics.mean(parts) if len(parts) >= 2 else None
    if score is None:
        verdict = None
    elif score > 0:
        verdict = "Stretched -- rich vs its norms; reversion risk is to the downside"
    elif m["revenue_growth"] is not None and m["revenue_growth"] < 0:
        verdict = "Possible value trap -- cheap, but TTM revenue is shrinking"
    elif (pe_dev is not None and pe_dev < -30 and m["eps_growth"] is not None and m["revenue_growth"] is not None
          and m["eps_growth"] - m["revenue_growth"] > 0.25):
        # AON 2026-10-03: P/E -54% vs its 5y median, but TTM EPS +53% on
        # revenue +5% (a Q4 2025 EPS of 7.81 vs 3.29) -- a low P/E that a
        # one-off gain manufactured is not cheapness.
        verdict = (f"Possible value trap -- P/E flattered: TTM EPS {m['eps_growth']*100:+.0f}% on revenue "
                   f"{m['revenue_growth']*100:+.0f}%, likely one-off gains")
    else:
        verdict = "Reversion candidate -- cheap or oversold while revenue still grows"
    return {**m, "pe_dev": pe_dev, "ev_dev": ev_dev, "group_median_ev_ebitda": group_median_ev,
            "score": round(score, 2) if score is not None else None,
            "extremes": extremes, "verdict": verdict}


def mean_reversion_scan() -> dict:
    tickers, groups = _reversion_universe()
    metrics = {}
    for t in tickers:
        try:
            m = _reversion_metrics(t)
            if m:
                metrics[t] = m
        except Exception as exc:
            log.warning("%s: reversion metrics failed: %s", t, exc)
    scored = []
    for t, m in metrics.items():
        pool = {x for members in groups.values() if t in members for x in members if x != t}
        evs = [metrics[x]["ev_ebitda"] for x in pool if x in metrics and metrics[x]["ev_ebitda"]
               and metrics[x]["ev_ebitda"] > 0]
        # A two-member "median" is just the other stock: TSLA read +4447% vs
        # a GM/F pair (RIVN has negative EBITDA) and F read -96% vs TSLA/GM.
        scored.append(_reversion_score(m, statistics.median(evs) if len(evs) >= REVERSION_MIN_PEERS else None))
    ranked = sorted((r for r in scored if r["score"] is not None), key=lambda r: -abs(r["score"]))
    return {
        "generatedAt": dt.datetime.now(NZ_TZ).strftime("%Y-%m-%d %H:%M"),
        "universe": len(tickers), "scored": len(ranked),
        "top": ranked[:REVERSION_TOP_N],
    }


def write_reversion_snapshot(snapshot: dict) -> None:
    if not snapshot["top"]:
        log.warning("mean reversion scan scored nothing; keeping the previous reversion.json")
        return
    os.makedirs(os.path.dirname(REVERSION_DATA_PATH), exist_ok=True)
    with open(REVERSION_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    log.info("mean reversion: scored %d/%d, top %s", snapshot["scored"], snapshot["universe"],
             ", ".join(f"{r['ticker']} {r['score']:+.2f}" for r in snapshot["top"]))


# ---------------------------------------------------------------------------
# Volatility regime ("Volatility Regime Analysis" prompt) -- market-wide,
# 100% deterministic, no LLM. Cboe's VIX-family indices come through the
# same free Yahoo chart endpoint as everything else; Yahoo has no ^RVX
# (Russell 2000 implied, 404 live), so IWM shows realized vol only. The
# "trades that suit this regime" lines are fixed textbook pairings per
# regime, not recommendations.
# ---------------------------------------------------------------------------

VOLATILITY_DATA_PATH = os.path.join(ROOT, "dashboard", "data", "volatility.json")
VIX_TERM = [("^VIX9D", "9-day"), ("^VIX", "30-day"), ("^VIX3M", "3-month"), ("^VIX6M", "6-month")]
VOL_INDICES = [("SPY", "S&P 500", "^VIX"), ("QQQ", "Nasdaq-100", "^VXN"), ("IWM", "Russell 2000", None)]

REGIME_TRADES = {
    "Calm": "Options are cheap: hedges (puts, collars) cost little; premium-selling pays little and is "
            "exposed to a vol spike.",
    "Normal": "No strong vol edge either way; positioning matters more than vol trades.",
    "Elevated": "Premium is rich: covered calls and cash-secured puts collect more; hedges are expensive.",
    "Stressed": "Backwardation means near-term fear dominates: short-vol and leveraged carry are at their "
                "riskiest; historically vol mean-reverts after the spike, but timing it is the hard part.",
}


def _realized_vol(closes: list, days: int = 20) -> "float | None":
    if len(closes) < days + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - days, len(closes))]
    return statistics.stdev(rets) * (252 ** 0.5) * 100


def volatility_regime() -> dict:
    series = {}
    for t in {t for t, _ in VIX_TERM} | {i for _, _, i in VOL_INDICES if i} | {e for e, _, _ in VOL_INDICES}:
        try:
            series[t] = st._fetch_series(t, "10y", "1d")["closes"]
        except Exception as exc:
            log.warning("%s: volatility fetch failed: %s", t, exc)

    vix = series.get("^VIX") or []
    vix_now = vix[-1] if vix else None
    pctile = (sum(v <= vix_now for v in vix) / len(vix) * 100) if vix else None
    term = [{"index": t, "tenor": tenor, "level": round(series[t][-1], 2) if series.get(t) else None}
            for t, tenor in VIX_TERM]
    ratio = (vix_now / series["^VIX3M"][-1]) if vix_now and series.get("^VIX3M") else None
    shape = None if ratio is None else "Backwardation" if ratio > 1 else "Contango"

    indices = []
    for etf, name, implied in VOL_INDICES:
        rv = _realized_vol(series.get(etf) or [])
        iv = series[implied][-1] if implied and series.get(implied) else None
        indices.append({
            "etf": etf, "name": name, "implied_index": implied,
            "realized_20d": round(rv, 1) if rv is not None else None,
            "implied": round(iv, 1) if iv is not None else None,
            "spread": round(iv - rv, 1) if iv is not None and rv is not None else None,
        })

    # Fixed bands on the 10-year VIX percentile, with backwardation
    # overriding: an inverted curve is the stress signal even at a middling
    # VIX level.
    if pctile is None:
        regime = None
    elif shape == "Backwardation":
        regime = "Stressed"
    elif pctile < 25:
        regime = "Calm"
    elif pctile < 75:
        regime = "Normal"
    else:
        regime = "Elevated"
    return {
        "generatedAt": dt.datetime.now(NZ_TZ).strftime("%Y-%m-%d %H:%M"),
        "vix": round(vix_now, 2) if vix_now is not None else None,
        "vixPercentile10y": round(pctile, 0) if pctile is not None else None,
        "vixRange10y": [round(min(vix), 2), round(max(vix), 2)] if vix else None,
        "termStructure": term,
        "vixToVix3m": round(ratio, 3) if ratio is not None else None,
        "shape": shape,
        "indices": indices,
        "regime": regime,
        "trades": REGIME_TRADES.get(regime, ""),
    }


def write_volatility_snapshot(snapshot: dict) -> None:
    if snapshot["regime"] is None:
        log.warning("no VIX data fetched; keeping the previous volatility.json")
        return
    os.makedirs(os.path.dirname(VOLATILITY_DATA_PATH), exist_ok=True)
    with open(VOLATILITY_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    log.info("volatility regime %s: VIX %s (%sth pct), %s", snapshot["regime"], snapshot["vix"],
             snapshot["vixPercentile10y"], snapshot["shape"])


# ---------------------------------------------------------------------------
# Drawdown analogues ("Drawdown Scenario Planner" prompt) -- each watchlist
# ticker's actual peak-to-trough fall and recovery in four historical
# sell-offs, against SPY. 100% deterministic, no LLM. Weekly closes over
# range=30y (range=max silently downsamples to monthly/quarterly bars).
# Closes are split- but not dividend-adjusted, so recoveries read slightly
# slow for dividend payers. A ticker not yet listed in an episode gets a
# beta-scaled estimate instead (2-year daily beta on SPY's log return), labelled as
# one. Drivers and hedges are fixed per-episode notes, not generated.
# ---------------------------------------------------------------------------

DRAWDOWN_DATA_PATH = os.path.join(ROOT, "dashboard", "data", "drawdowns.json")
DRAWDOWN_MIN_BETA_DAYS = 60

EPISODES = [
    {"id": "dotcom", "name": "Dot-com bust", "start": "2000-01-01", "end": "2002-12-31",
     "driver": "A valuation bust: profitless tech and telecom growth repriced after the 1990s bubble.",
     "hedge": "Underweighting high-multiple growth and holding index puts; value and defensives held up."},
    {"id": "gfc", "name": "2008 financial crisis", "start": "2007-07-01", "end": "2009-06-30",
     "driver": "A credit and banking crisis: forced deleveraging sold nearly every risk asset at once.",
     "hedge": "Long Treasuries, cash and index puts; stock diversification failed as correlations went to 1."},
    {"id": "covid", "name": "2020 COVID crash", "start": "2020-02-01", "end": "2020-04-30",
     "driver": "A sudden-stop liquidity shock: the fastest 30%+ fall on record, recovered within months.",
     "hedge": "Short-dated index puts or VIX calls paid most; selling near the bottom was the bigger risk."},
    {"id": "rates2022", "name": "2022 rate shock", "start": "2021-11-01", "end": "2022-12-31",
     "driver": "Inflation and Fed hikes repriced long-duration growth; stocks and bonds fell together.",
     "hedge": "Bonds failed as a hedge; T-bills, energy and commodities, and value tilts held up."},
]


def _episode_drawdown(dates: list, closes: list, start: str, end: str) -> "dict | None":
    dates = [d if isinstance(d, str) else d.isoformat() for d in dates]
    idx = [i for i, d in enumerate(dates) if start <= d <= end]
    if len(idx) < 4 or dates[0] > start:
        return None  # not listed (or no history) for the whole episode
    peak_i = trough_i = idx[0]
    run_peak_i, worst = idx[0], 0.0
    for i in idx:
        if closes[i] > closes[run_peak_i]:
            run_peak_i = i
        dd = closes[i] / closes[run_peak_i] - 1
        if dd < worst:
            worst, peak_i, trough_i = dd, run_peak_i, i
    if worst == 0.0:
        return {"drawdown": 0.0, "peak": dates[peak_i], "trough": dates[trough_i], "recovered": dates[trough_i],
                "weeksToRecover": 0}
    recovered = next((i for i in range(trough_i + 1, len(closes)) if closes[i] >= closes[peak_i]), None)
    return {
        "drawdown": round(worst * 100, 1),
        "peak": dates[peak_i], "trough": dates[trough_i],
        "recovered": dates[recovered] if recovered is not None else None,
        "weeksToRecover": (round((dt.date.fromisoformat(dates[recovered]) - dt.date.fromisoformat(dates[trough_i])).days / 7)
                           if recovered is not None else None),
    }


def _beta(closes: list, spy_closes: list) -> "tuple[float | None, int]":
    n = min(len(closes), len(spy_closes))
    if n < DRAWDOWN_MIN_BETA_DAYS + 1:
        return None, max(0, n - 1)
    a, b = closes[-n:], spy_closes[-n:]
    ra = [a[i] / a[i - 1] - 1 for i in range(1, n)]
    rb = [b[i] / b[i - 1] - 1 for i in range(1, n)]
    var = statistics.pvariance(rb)
    if not var:
        return None, n - 1
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / len(ra)
    return cov / var, n - 1


def drawdown_analogues(spy_daily: dict) -> dict:
    spy_w = st._fetch_series(st.BENCHMARK_TICKER, "30y", "1wk")
    spy_eps = {ep["id"]: _episode_drawdown(spy_w["dates"], spy_w["closes"], ep["start"], ep["end"]) for ep in EPISODES}
    rows = []
    for ticker in WATCHLIST:
        try:
            w = st._fetch_series(ticker, "30y", "1wk")
            daily = st._fetch_series(ticker, "2y", "1d")
        except Exception as exc:
            log.warning("%s: drawdown history fetch failed: %s", ticker, exc)
            continue
        beta, beta_days = _beta(daily["closes"], spy_daily["closes"])
        cells = {}
        for ep in EPISODES:
            actual = _episode_drawdown(w["dates"], w["closes"], ep["start"], ep["end"])
            spy = spy_eps[ep["id"]]
            if actual:
                cells[ep["id"]] = {**actual, "isEstimate": False}
            elif beta is not None and spy:
                # Scaled on log returns, not linearly: beta x SPY's -47% put
                # TSLA at -107% for the dot-com bust. (1 + dd)^beta keeps any
                # beta inside -100%.
                est = (1 - (1 + spy["drawdown"] / 100) ** max(beta, 0.0)) * -100
                cells[ep["id"]] = {"drawdown": round(est, 1), "isEstimate": True}
            else:
                cells[ep["id"]] = None
        rows.append({"ticker": ticker, "beta": round(beta, 2) if beta is not None else None,
                     "betaDays": beta_days, "episodes": cells})
    return {
        "generatedAt": dt.datetime.now(NZ_TZ).strftime("%Y-%m-%d %H:%M"),
        "episodes": [{k: ep[k] for k in ("id", "name", "start", "end", "driver", "hedge")} for ep in EPISODES],
        "spy": spy_eps,
        "rows": rows,
    }


def write_drawdown_snapshot(snapshot: dict) -> None:
    if not snapshot["rows"]:
        log.warning("no drawdown rows computed; keeping the previous drawdowns.json")
        return
    os.makedirs(os.path.dirname(DRAWDOWN_DATA_PATH), exist_ok=True)
    with open(DRAWDOWN_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    log.info("drawdown analogues: %d tickers", len(snapshot["rows"]))


# ---------------------------------------------------------------------------
# Factor performance ("Factor Performance Dashboard" prompt) -- six iShares
# factor ETFs' returns vs SPY over 1/3/6/12 months, 100% deterministic, no
# LLM. Value shows both MSCI's factor ETF (VLUE) and broad Russell 1000
# value (IWD). Size is IWM (Russell 2000 small caps) against cap-weighted SPY, the
# plain small-vs-large read. "In favour" = beating SPY over both 3 and 6
# months, "out of favour" = lagging both; the rotation is read from three
# fixed spreads, and the positioning lines are fixed per-factor notes.
# ---------------------------------------------------------------------------

FACTORS_DATA_PATH = os.path.join(ROOT, "dashboard", "data", "factors.json")
FACTORS = [
    ("VLUE", "Value (MSCI factor)", "Cheap stocks leading usually means investors are paying for current earnings over future growth."),
    ("IWD", "Value (Russell 1000)", "Broad value, the like-for-like partner of IWF; VLUE is sector-neutral and concentrated, so it can diverge."),
    ("IWF", "Growth", "Growth leading usually means falling or stable yields and appetite for long-duration earnings."),
    ("MTUM", "Momentum", "Momentum leading means existing trends are persisting; it reverses hardest at turning points."),
    ("QUAL", "Quality", "Quality leading often marks a late-cycle or uncertain market favouring strong balance sheets."),
    ("USMV", "Low volatility", "Low volatility leading is a defensive signal: investors are paying for stability."),
    ("IWM", "Size (small caps)", "Small caps leading usually signals risk appetite and expectations of easier credit."),
]
FACTOR_WINDOWS = (("rs1m", 21), ("rs3m", 63), ("rs6m", 126), ("rs12m", 252))


def factor_performance(spy_daily: dict) -> dict:
    rows = {}
    for etf, name, note in FACTORS:
        try:
            closes = st._fetch_series(etf, "2y", "1d")["closes"]
        except Exception as exc:
            log.warning("%s: factor fetch failed: %s", etf, exc)
            continue
        row = {"etf": etf, "name": name, "note": note}
        for key, days in FACTOR_WINDOWS:
            own, spy = st._pct_return(closes, days), st._pct_return(spy_daily["closes"], days)
            row[key] = round(own - spy, 2) if own is not None and spy is not None else None
        both = (row["rs3m"], row["rs6m"])
        row["status"] = ("In favour" if None not in both and min(both) > 0 else
                         "Out of favour" if None not in both and max(both) < 0 else "Mixed")
        rows[etf] = row

    def spread(a: str, b: str, label_a: str, label_b: str) -> "dict | None":
        x, y = rows.get(a, {}).get("rs6m"), rows.get(b, {}).get("rs6m")
        if x is None or y is None:
            return None
        gap = round(x - y, 1)
        return {"pair": f"{label_a} vs {label_b}", "spread6m": gap,
                "leader": label_a if gap > 0 else label_b}

    return {
        "generatedAt": dt.datetime.now(NZ_TZ).strftime("%Y-%m-%d %H:%M"),
        "factors": list(rows.values()),
        "rotation": [s for s in (
            # IWD/IWF, not VLUE: the Russell pair splits the same index, while
            # VLUE ran +43pp vs SPY over 12 months to Oct 2026 on its own mix.
            spread("IWD", "IWF", "Value", "Growth"),
            spread("IWM", "QUAL", "Small caps", "Quality large caps"),
            spread("USMV", "MTUM", "Low volatility", "Momentum"),
        ) if s],
    }


def write_factor_snapshot(snapshot: dict) -> None:
    if not snapshot["factors"]:
        log.warning("no factor data fetched; keeping the previous factors.json")
        return
    os.makedirs(os.path.dirname(FACTORS_DATA_PATH), exist_ok=True)
    with open(FACTORS_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    log.info("factors: %s", ", ".join(f"{r['name']} {r['status']}" for r in snapshot["factors"]))


# ---------------------------------------------------------------------------
# Research notes (Tranche C: "Research Note Template", "Debate Simulator",
# "Pre-Mortem Analysis") -- one Workers AI call per ticker returning all
# three as JSON, ~240 neurons each. The model only argues from a fact sheet
# built from today's saved snapshots (the ticker's KPIs, drawdown history,
# and the sector/factor/volatility readings); code validates the shape, and
# any number in the reply that is not in the fact sheet counts as invented:
# one retry, then the note is published flagged. Model-written opinion,
# labelled as such on the page -- never investment advice.
# ---------------------------------------------------------------------------

NOTES_DATA_PATH = os.path.join(ROOT, "dashboard", "data", "notes.json")
NOTE_ACTIONS = {"Buy", "Hold", "Sell"}
NOTE_CONVICTIONS = {"Low", "Medium", "High"}
_NUMBER = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
_STR, _STRS = {"type": "string"}, {"type": "array", "items": {"type": "string"}}
NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "thesis": _STR, "key_points": _STRS, "risks": _STRS, "valuation": _STR,
        "action": {"type": "string", "enum": sorted(NOTE_ACTIONS)},
        "conviction": {"type": "string", "enum": sorted(NOTE_CONVICTIONS)},
        "bull": _STRS, "bear": _STRS, "debate_verdict": _STR,
        "premortem": {"type": "array", "items": {
            "type": "object",
            "properties": {"reason": _STR, "probability": {"type": "integer"}, "warning_sign": _STR},
            "required": ["reason", "probability", "warning_sign"]}},
    },
    "required": ["thesis", "key_points", "risks", "valuation", "action", "conviction",
                 "bull", "bear", "debate_verdict", "premortem"],
}

NOTES_PROMPT = """You are a portfolio manager's analyst. Using ONLY the fact sheet below, write
three things about {ticker}. Every number you write must appear in the fact sheet; do not
invent prices, targets, growth rates or dates. Plain English, no markdown.

Return ONLY a JSON object with exactly these keys:
{{
  "thesis": "one sentence",
  "key_points": ["3 to 5 short points, each citing a fact-sheet number"],
  "risks": ["3 primary risks"],
  "valuation": "1-2 sentences on valuation from the fact sheet",
  "action": "Buy" or "Hold" or "Sell",
  "conviction": "Low" or "Medium" or "High",
  "bull": ["the 3 strongest arguments for buying"],
  "bear": ["the 3 strongest arguments for selling"],
  "debate_verdict": "2 sentences: which side is stronger and why",
  "premortem": [{{"reason": "why it lost 40% in 12 months", "probability": whole-number percent, "warning_sign": "what to monitor"}}]
}}
The premortem list has exactly 5 entries.

FACT SHEET
{facts}
"""


def _fact_sheet(row: dict, drawdowns: dict, sectors: dict, factors: dict, vol: dict) -> str:
    lines = [f"Ticker: {row['ticker']}",
             f"Composite risk score: {row.get('composite')}/10 (higher = lower risk), {row.get('coverage')} available"]
    for key, name in KPI_ORDER:
        k = row.get("kpis", {}).get(key) or {}
        if k.get("score") is not None:
            lines.append(f"- {name}: {k['score']:.2f} ({k.get('light')}) -- {k.get('detail', '')}")
        else:
            lines.append(f"- {name}: N/A")
    dd = next((r for r in drawdowns.get("rows", []) if r["ticker"] == row["ticker"]), None)
    if dd:
        lines.append(f"Beta vs SPY (2-year daily): {dd.get('beta')}")
        for ep in drawdowns.get("episodes", []):
            cell = dd["episodes"].get(ep["id"])
            if cell:
                kind = "estimated from beta" if cell["isEstimate"] else (
                    f"recovered in {cell['weeksToRecover']} weeks" if cell.get("weeksToRecover") is not None else "not recovered")
                lines.append(f"- {ep['name']} drawdown: {cell['drawdown']}% ({kind})")
    if sectors.get("regime"):
        lines.append(f"Market regime (from sector leadership and yield curve): {sectors['regime']} -- {sectors['regimeBasis']}")
    if vol.get("regime"):
        lines.append(f"Volatility regime: {vol['regime']}, VIX {vol['vix']} ({vol['vixPercentile10y']}th percentile of 10 years)")
    for f in factors.get("factors", []):
        lines.append(f"- Factor {f['name']}: {f['status']} ({f['rs6m']}pp vs SPY over 6 months)")
    lines.append(f"Dashboard summary: {row.get('summary', '')}")
    lines.append("Pre-mortem premise: the stock has lost 40% over the next 12 months.")
    return "\n".join(lines)


def _invented_numbers(note: dict, facts: str) -> list:
    known = [float(x.replace(",", "")) for x in _NUMBER.findall(facts)]
    texts = [note["thesis"], note["valuation"], note["debate_verdict"], *note["key_points"], *note["risks"],
             *note["bull"], *note["bear"], *(p["reason"] for p in note["premortem"]),
             *(p["warning_sign"] for p in note["premortem"])]
    bad = []
    for n in (float(x.replace(",", "")) for t in texts for x in _NUMBER.findall(t)):
        if n.is_integer() and 0 <= n <= 10:
            continue  # counts ("3 insiders", "10 KPIs")
        if not any(abs(n - v) <= max(0.05, 0.006 * abs(v)) or n == round(v) or abs(n) == abs(round(v)) for v in known):
            bad.append(n)
    return bad


def _parse_note(reply: str) -> dict:
    text = reply.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    note = json.loads(text[text.index("{"): text.rindex("}") + 1])
    lists = ("key_points", "risks", "bull", "bear")
    if any(not isinstance(note.get(k), str) or not note[k].strip() for k in ("thesis", "valuation", "debate_verdict")):
        raise ValueError("missing text field")
    if any(not isinstance(note.get(k), list) or not all(isinstance(x, str) for x in note[k]) or not note[k] for k in lists):
        raise ValueError("missing list field")
    if note.get("action") not in NOTE_ACTIONS or note.get("conviction") not in NOTE_CONVICTIONS:
        raise ValueError(f"bad action/conviction: {note.get('action')}/{note.get('conviction')}")
    pm = note.get("premortem")
    if not isinstance(pm, list) or len(pm) != 5:
        raise ValueError("premortem must have 5 entries")
    for p in pm:
        prob = p.get("probability")
        if isinstance(prob, str):
            prob = int(prob.strip().rstrip("%"))
        if not isinstance(prob, (int, float)) or not 0 <= prob <= 100 or not p.get("reason") or not p.get("warning_sign"):
            raise ValueError("bad premortem entry")
        p["probability"] = int(round(prob))
    return {k: note[k] for k in ("thesis", "key_points", "risks", "valuation", "action", "conviction",
                                 "bull", "bear", "debate_verdict", "premortem")}


def _research_note(row: dict, facts: str) -> dict:
    last_error = None
    for attempt in range(2):
        try:
            note = _parse_note(_generate(NOTES_PROMPT.format(ticker=row["ticker"], facts=facts), num_predict=1300,
                                         json_schema=NOTE_SCHEMA))
        except Exception as exc:
            last_error = exc
            log.warning("%s: research note attempt %d failed: %s", row["ticker"], attempt + 1, exc)
            continue
        invented = _invented_numbers(note, facts)
        if not invented or attempt == 1:
            if invented:
                log.warning("%s: research note keeps numbers not in the fact sheet: %s", row["ticker"], invented)
            return {**note, "unverified": invented}
        log.warning("%s: research note invented %s; retrying", row["ticker"], invented)
    raise RuntimeError(f"no valid research note: {last_error}")


def research_notes() -> dict:
    rows = _load_json(DASHBOARD_DATA_PATH, [])
    drawdowns = _load_json(DRAWDOWN_DATA_PATH, {})
    sectors = _load_json(SECTORS_DATA_PATH, {})
    factors = _load_json(FACTORS_DATA_PATH, {})
    vol = _load_json(VOLATILITY_DATA_PATH, {})
    notes = []
    for row in rows:
        if row.get("composite") is None:
            continue
        try:
            note = _research_note(row, _fact_sheet(row, drawdowns, sectors, factors, vol))
            notes.append({"ticker": row["ticker"], "composite": row["composite"], **note})
        except Exception as exc:
            log.error("%s: research note failed: %s", row["ticker"], exc)
    return {"generatedAt": dt.datetime.now(NZ_TZ).strftime("%Y-%m-%d %H:%M"), "model": sf.WORKERS_AI_MODEL,
            "notes": notes}


def write_notes_snapshot(snapshot: dict) -> None:
    if not snapshot["notes"]:
        log.warning("no research notes generated; keeping the previous notes.json")
        return
    os.makedirs(os.path.dirname(NOTES_DATA_PATH), exist_ok=True)
    with open(NOTES_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    log.info("research notes: %s", ", ".join(f"{n['ticker']} {n['action']}/{n['conviction']}" for n in snapshot["notes"]))


# ---------------------------------------------------------------------------
# KPI 10: Red Flags & Accounting Risk
# ---------------------------------------------------------------------------

def _kpi_red_flags(bundle: "dict | None") -> dict:
    if bundle is None:
        return _kpi_na("no SEC filings for this ticker")
    facts, accn = bundle["facts"], bundle["accn"]

    _, goodwill_series = srf._xbrl_multi_year(facts, srf.GOODWILL_TAGS, n=1)
    goodwill = goodwill_series[-1]["val"] if goodwill_series else None
    goodwill_flagged = (goodwill is not None and bundle["equity"]) and (goodwill / bundle["equity"] * 100 > 30)

    dso = srf._section_dso_trend(facts, accn)
    inventory = srf._section_inventory_trend(facts, accn)

    going_concern_present = False
    try:
        tenk_text = sf._fetch_text(bundle["tenk_url"])
        gc = srf._section_auditor_going_concern(tenk_text, accn, bundle["filed"])
        going_concern_present = not gc["going_concern"].startswith("No going-concern language found")
    except Exception as exc:
        log.warning("going-concern check failed: %s", exc)

    score = 1.0
    flags = []
    if goodwill_flagged:
        score -= 0.2
        flags.append("goodwill >30% of equity")
    if dso.get("rising"):
        score -= 0.2
        flags.append("DSO rising")
    if inventory.get("rising"):
        score -= 0.2
        flags.append("inventory/revenue rising")
    if going_concern_present:
        score = min(score, 0.2)  # hard override -- never averaged away
        flags.append("GOING-CONCERN LANGUAGE PRESENT")

    detail = "No flags raised." if not flags else "Flags: " + "; ".join(flags) + "."
    return _kpi(score, detail)


# ---------------------------------------------------------------------------
# Composite + LLM summary
# ---------------------------------------------------------------------------

KPI_ORDER = [
    ("kpi1", "Fundamental Health"),
    ("kpi2", "Earnings Quality & Surprise Stability"),
    ("kpi3", "Revenue & Margin Trajectory"),
    ("kpi4", "Balance Sheet & Debt Maturity Risk"),
    ("kpi5", "Cash Flow & Dividend Coverage"),
    ("kpi6", "Valuation vs History & Peers"),
    ("kpi7", "Technical Trend & Momentum"),
    ("kpi8", "Volatility & Event Risk"),
    ("kpi9", "Market & Sector-Relative Pressure"),
    ("kpi10", "Red Flags & Accounting Risk"),
]


def _composite(kpis: dict) -> "tuple[float | None, str | None, str]":
    scored = [kpis[k]["score"] for k, _ in KPI_ORDER if kpis[k]["score"] is not None]
    coverage = f"{len(scored)}/{len(KPI_ORDER)} KPIs"
    if not scored:
        return None, None, coverage
    composite = round(10 * (sum(scored) / len(scored)), 1)
    return composite, _light(composite / 10), coverage


def _generate(prompt: str, num_predict: int = 300, json_schema: "dict | None" = None) -> str:
    try:
        reply = sf.workers_ai_generate(prompt, max_tokens=num_predict, json_schema=json_schema)
        if not reply:
            raise RuntimeError("model returned empty content")
    except Exception:
        sf.llm_record(False)
        raise
    sf.llm_record(True)
    return reply


SUMMARY_PROMPT = (
    "Act as a risk analyst. Below are 10 already-computed KPI scores (0-1, "
    "higher is better) and a composite score (0-10) for {ticker}. Write "
    "exactly 3 sentences summarizing the overall risk picture. Do NOT "
    "invent numbers or contradict the scores given -- narrate what's "
    "already here, don't re-judge it.\n\n"
    "Composite: {composite}/10 ({coverage} available)\n{kpi_lines}"
)


def _summary_narrative(ticker: str, kpis: dict, composite: "float | None", coverage: str) -> str:
    lines = []
    for key, name in KPI_ORDER:
        r = kpis[key]
        if r["score"] is None:
            lines.append(f"- {name}: N/A")
        else:
            lines.append(f"- {name}: {r['score']:.2f} ({r['light']}) -- {r['detail']}")
    try:
        return _generate(SUMMARY_PROMPT.format(
            ticker=ticker, composite=composite if composite is not None else "N/A",
            coverage=coverage, kpi_lines="\n".join(lines),
        ))
    except Exception as exc:
        log.warning("%s: summary narration failed: %s", ticker, exc)
        return f"N/A -- local LLM summary failed ({exc})."


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

_sector_snapshot = None


def build_report(ticker: str) -> "dict | None":
    bundle = _sec_bundle(ticker)

    try:
        daily = st._fetch_series(ticker, "2y", "1d")
        weekly = st._fetch_series(ticker, "10y", "1wk")
    except Exception as exc:
        log.error("%s: price fetch failed: %s", ticker, exc)
        daily = weekly = {"closes": []}
    try:
        spy_daily = st._fetch_series(st.BENCHMARK_TICKER, "2y", "1d")
    except Exception as exc:
        log.error("SPY fetch failed, relative-strength KPIs will be N/A: %s", exc)
        spy_daily = {"closes": []}

    global _sector_snapshot
    if _sector_snapshot is None and spy_daily["closes"]:
        _sector_snapshot = sector_rotation(spy_daily)
        write_sector_snapshot(_sector_snapshot)

    kpi4, debt_flagged = _kpi_balance_sheet_debt(bundle)
    kpi6 = _kpi_valuation(ticker, bundle)

    kpis = {
        "kpi1": _kpi_fundamental_health(bundle),
        "kpi2": _kpi_earnings_quality(ticker),
        "kpi3": _kpi_revenue_margin(bundle),
        "kpi4": kpi4,
        "kpi5": _kpi_cash_flow_dividend(bundle),
        "kpi6": kpi6,
        "kpi7": _kpi_technical_momentum(daily, weekly),
        "kpi8": _kpi_volatility_event(ticker, daily, spy_daily),
        "kpi9": _kpi_market_sector_pressure(ticker, daily, spy_daily, _sector_snapshot or {}),
        "kpi10": _kpi_red_flags(bundle),
    }
    composite, composite_light, coverage = _composite(kpis)
    summary = _summary_narrative(ticker, kpis, composite, coverage)

    return {
        "ticker": ticker,
        "date": dt.datetime.now(NZ_TZ).date().isoformat(),
        "generated_at": dt.datetime.now(NZ_TZ).strftime("%Y-%m-%d %H:%M"),
        "composite": composite,
        "composite_light": composite_light,
        "coverage": coverage,
        "kpis": kpis,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Notion (duplicated per-script, not shared -- established norm in this
# project, see stock_risk_flags.py's own comment on the same pattern)
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
    light_options = {"options": [{"name": "Green"}, {"name": "Amber"}, {"name": "Red"}]}
    properties = {
        "Ticker": {"title": {}},
        "Date": {"date": {}},
        "Composite Score": {"number": {"format": "number"}},
        "Composite Light": {"select": light_options},
        "Coverage": {"rich_text": {}},
        "KPI Detail JSON": {"rich_text": {}},
        "Summary": {"rich_text": {}},
        "Generated At": {"rich_text": {}},
    }
    for key, name in KPI_ORDER:
        properties[f"{name}"] = {"number": {"format": "number"}}
        properties[f"{name} Light"] = {"select": light_options}
    properties["Earnings History Depth"] = {"number": {"format": "number"}}
    result = _notion("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": "股票風險評分 Risk Dashboard"}}],
        "properties": properties,
    }, api_key)
    return result["id"]


def _page_properties(report: dict) -> dict:
    props = {
        "Ticker": {"title": _rich(report["ticker"])},
        "Date": {"date": {"start": report["date"]}},
        "Coverage": {"rich_text": _rich(report["coverage"])},
        "KPI Detail JSON": {"rich_text": _rich(json.dumps(report["kpis"], ensure_ascii=False))},
        "Summary": {"rich_text": _rich(report["summary"])},
        "Generated At": {"rich_text": _rich(report["generated_at"])},
    }
    if report["composite"] is not None:
        props["Composite Score"] = {"number": report["composite"]}
    if report["composite_light"]:
        props["Composite Light"] = {"select": {"name": report["composite_light"]}}
    for key, name in KPI_ORDER:
        r = report["kpis"][key]
        if r["score"] is not None:
            props[name] = {"number": r["score"]}
        if r["light"]:
            props[f"{name} Light"] = {"select": {"name": r["light"]}}
    kpi2_detail = report["kpis"]["kpi2"].get("detail", "")
    if "quarter(s) of history" in kpi2_detail:
        try:
            n = int(kpi2_detail.split(" based on ")[-1].split(" quarter")[0])
            props["Earnings History Depth"] = {"number": n}
        except (ValueError, IndexError):
            pass
    return props


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _generate_and_write(ticker: str, cfg: "dict | None") -> "dict | None":
    report = build_report(ticker)
    if not report:
        return None
    if cfg and cfg.get("api_key") and cfg.get("risk_dashboard_database_id"):
        _notion("POST", "/pages", {
            "parent": {"database_id": cfg["risk_dashboard_database_id"]},
            "properties": _page_properties(report),
        }, cfg["api_key"])
    return report


def _to_ticker_row(report: dict) -> dict:
    # Matches dashboard/app/lib/types.ts's TickerRow shape exactly -- the
    # Next.js app reads this JSON directly, no field-name translation.
    return {
        "ticker": report["ticker"],
        "date": report["date"],
        "composite": report["composite"],
        "compositeLight": report["composite_light"],
        "coverage": report["coverage"],
        "kpis": report["kpis"],
        "summary": report["summary"],
        "generatedAt": report["generated_at"],
    }


def write_local_snapshot_rows(rows: list) -> None:
    os.makedirs(os.path.dirname(DASHBOARD_DATA_PATH), exist_ok=True)
    rows = sorted(rows, key=lambda r: r["ticker"])
    with open(DASHBOARD_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    log.info("wrote %d row(s) to %s", len(rows), DASHBOARD_DATA_PATH)


def poll_and_generate() -> int:
    cfg = load_config()
    notion_ready = bool(cfg and cfg.get("api_key") and cfg.get("risk_dashboard_database_id"))
    if not notion_ready:
        log.info("Notion not configured (%s); dashboard JSON still written", CONFIG)

    written = 0
    reports = []
    for ticker in WATCHLIST:
        try:
            report = _generate_and_write(ticker, cfg if notion_ready else None)
            if not report:
                continue
            reports.append(report)
            written += 1
            log.info("%s: composite %s, %s", ticker, report["composite"], report["coverage"])
        except Exception as exc:
            log.error("%s: dashboard report failed: %s", ticker, exc)

    # Merge onto the previous snapshot rather than overwriting it -- the same
    # thing the --tickers path below already does. Writing only this run's
    # successes deleted any ticker whose single Yahoo fetch timed out from the
    # published dashboard for the whole day (NVDA on 3 separate days in one
    # week). A carried-forward row is marked stale so the UI can say so.
    fresh = {row["ticker"]: row for row in (_to_ticker_row(r) for r in reports)}
    merged = []
    for row in _load_json(DASHBOARD_DATA_PATH, []):
        if row["ticker"] not in fresh and row["ticker"] in WATCHLIST:
            row["stale"] = True
            merged.append(row)
            log.warning("%s: no fresh report, carrying forward %s snapshot as stale",
                        row["ticker"], row.get("date"))
    merged.extend(fresh.values())
    write_local_snapshot_rows(merged)
    try:
        write_reversion_snapshot(mean_reversion_scan())
    except Exception as exc:
        log.error("mean reversion scan failed: %s", exc)
    try:
        write_volatility_snapshot(volatility_regime())
    except Exception as exc:
        log.error("volatility regime failed: %s", exc)
    try:
        spy_daily = st._fetch_series(st.BENCHMARK_TICKER, "2y", "1d")
        write_drawdown_snapshot(drawdown_analogues(spy_daily))
        write_factor_snapshot(factor_performance(spy_daily))
    except Exception as exc:
        log.error("drawdown analogues / factor performance failed: %s", exc)
    try:
        write_notes_snapshot(research_notes())
    except Exception as exc:
        log.error("research notes failed: %s", exc)
    log.info("wrote %d report(s)", written)
    if written == 0:
        from notify import notify
        notify("股票報告失敗", f"Category 6 dashboard: 0/{len(WATCHLIST)} tickers, "
                               f"check asr/logs/{os.path.basename(LOG_PATH)}", priority=4)
    sf.alert_if_narration_dead("Category 6 dashboard", os.path.basename(LOG_PATH))
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
        cfg["risk_dashboard_database_id"] = db_id
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print(f"database created and saved to config: {db_id}")
        return

    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=None,
                         help="comma-separated ticker list; always regenerates, ignores no config")
    parser.add_argument("--reversion", action="store_true",
                        help="run only the mean reversion scan (no Notion, no LLM)")
    parser.add_argument("--notes", action="store_true",
                        help="run only the research notes from today's saved snapshots (Workers AI, no Notion)")
    parser.add_argument("--factors", action="store_true",
                        help="run only the factor performance snapshot (no Notion, no LLM)")
    parser.add_argument("--drawdowns", action="store_true",
                        help="run only the drawdown analogues (no Notion, no LLM)")
    parser.add_argument("--volatility", action="store_true",
                        help="run only the volatility regime snapshot (no Notion, no LLM)")
    args = parser.parse_args()

    if args.notes:
        snapshot = research_notes()
        write_notes_snapshot(snapshot)
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        return

    if args.factors:
        snapshot = factor_performance(st._fetch_series(st.BENCHMARK_TICKER, "2y", "1d"))
        write_factor_snapshot(snapshot)
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        return

    if args.drawdowns:
        snapshot = drawdown_analogues(st._fetch_series(st.BENCHMARK_TICKER, "2y", "1d"))
        write_drawdown_snapshot(snapshot)
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        return

    if args.volatility:
        snapshot = volatility_regime()
        write_volatility_snapshot(snapshot)
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        return

    if args.reversion:
        snapshot = mean_reversion_scan()
        write_reversion_snapshot(snapshot)
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        return

    if args.tickers:
        cfg = load_config()
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        written = 0
        reports = []
        for ticker in tickers:
            try:
                report = _generate_and_write(ticker, cfg)
                if not report:
                    print(f"{ticker}: report build failed")
                    continue
                print(f"=== {ticker} -- composite {report['composite']}/10 "
                      f"({report['composite_light']}), {report['coverage']} ===")
                print(json.dumps(report["kpis"], indent=2, ensure_ascii=False))
                print(report["summary"])
                reports.append(report)
                written += 1
            except Exception as exc:
                log.error("%s: dashboard report failed: %s", ticker, exc)
                print(f"{ticker}: FAILED -- {exc}")
        if reports:
            # Merge onto any existing snapshot rows for tickers not in this
            # --tickers run, so a partial re-run doesn't wipe the rest of
            # the watchlist out of the published dashboard.
            existing = _load_json(DASHBOARD_DATA_PATH, [])
            by_ticker = {row["ticker"]: row for row in existing}
            for r in reports:
                row = _to_ticker_row(r)
                by_ticker[row["ticker"]] = row
            write_local_snapshot_rows(list(by_ticker.values()))
        print(f"processed {written}/{len(tickers)} tickers")
        return

    try:
        poll_and_generate()
    except Exception as exc:
        log.error("run failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
