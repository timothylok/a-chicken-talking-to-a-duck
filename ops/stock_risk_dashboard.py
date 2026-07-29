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

KPI 9 ("Market & Peer-Relative Pressure") is an explicit, labeled
approximation: no sector classification, macro calendar, or regulatory
tracking exists anywhere in this codebase, so it's built from two
signals that already exist for other purposes (relative-strength-vs-SPY
as a market-wide proxy, peer-multiple spread as a sector-stress proxy).
The tile name itself carries the "(approximation)" caveat in Notion and
should carry it into the dashboard UI too -- never implied as real
sector/macro data.

Tickers with no SEC filings (sf.EXCLUDE_NO_SEC_FILINGS, currently only
SPCX) get every SEC-dependent KPI (1,2,3,4,5,6,10) marked N/A rather than
guessed -- they still get a dashboard row, just with a low Coverage
count. KPI 7 (technical) and KPI 9 (market/peer-relative, weighted 100%
onto its non-SEC relative-strength term when peers are missing) still
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

# Not sf.OLLAMA_MODEL (lfm2.5) -- same reasoning as Category 2/3/5/4: a
# structured, numbers-grounded narration the user may act on, not spoken-
# Cantonese brevity.
DASHBOARD_MODEL = "qwen3:8b"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "asr", "logs", "stock_risk_dashboard.log")
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
    for t in os.environ.get("STOCK_WATCHLIST", "AAPL,MSFT,NVDA,TSLA,GOOGL,AMZN,META,SPCX").split(",")
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
    usable = [
        f for f in filings
        if f.get("eps_actual") is not None and f.get("eps_consensus")
    ][-8:]
    if not usable:
        return _kpi_na("no earnings-history entries yet (asr/logs/earnings_history.json)")
    latest = usable[-1]
    surprise_pct = (latest["eps_actual"] - latest["eps_consensus"]) / abs(latest["eps_consensus"]) * 100
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
    if returned <= 0:
        return _kpi(0.7, "No buybacks/dividends this year to evaluate coverage against.")
    coverage = fcf_val / returned
    score = _lerp_score(coverage, 1.0, 0.2, 1.5, 1.0)
    detail = f"FCF covers {coverage:.1f}x this year's buybacks+dividends ({sf._fmt_usd(fcf_val)} FCF vs {sf._fmt_usd(returned)} returned)."
    return _kpi(score, detail)


# ---------------------------------------------------------------------------
# KPI 6: Valuation vs History & Peers
# ---------------------------------------------------------------------------

def _kpi_valuation(ticker: str, bundle: "dict | None") -> "tuple[dict, dict | None]":
    # Returns (KpiResult, peer_comparison) -- peer_comparison is handed
    # back so KPI9 can reuse the peer-spread signal it already computed
    # rather than a third fetch of the same peer set.
    if bundle is None:
        return _kpi_na("no SEC filings for this ticker"), None
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
        return _kpi_na("no trailing/historical/peer P/E available to compare"), peer_comparison
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
    return _kpi(score, detail), peer_comparison


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
# KPI 9: Market & Peer-Relative Pressure (approximation) -- see module
# docstring. Explicitly NOT presented as true sector/macro data.
# ---------------------------------------------------------------------------

def _kpi_market_peer_pressure(ticker: str, daily: dict, spy_daily: dict, peer_comparison: "dict | None") -> dict:
    rs = st._section_relative_strength(ticker, daily, spy_daily)
    diff3 = rs.get("rs_3m_diff")
    market_score = _lerp_score(diff3, -15, 0.0, 15, 1.0) if diff3 is not None else None

    peer_score = None
    if peer_comparison and peer_comparison.get("peers"):
        pe_vals = [p["pe"] for p in peer_comparison["peers"] if p.get("pe")]
        all_pe = pe_vals + ([peer_comparison["own_pe"]] if peer_comparison.get("own_pe") else [])
        if len(all_pe) >= 2:
            spread_pct = (max(all_pe) - min(all_pe)) / statistics.median(all_pe) * 100
            # A narrow peer-multiple spread reads as sector calm; a wide
            # spread reads as sector re-rating stress. Never presented as
            # real macro data -- purely a spread-of-multiples proxy.
            peer_score = _lerp_score(spread_pct, 20, 1.0, 100, 0.0)

    if market_score is None and peer_score is None:
        return _kpi_na("insufficient relative-strength/peer data for this ticker")
    if peer_score is None:
        score = market_score
    else:
        score = 0.6 * market_score + 0.4 * peer_score if market_score is not None else peer_score
    detail = (
        f"Market-relative (3mo vs SPY): {diff3:+.1f}pp" if diff3 is not None else "Market-relative: N/A"
    ) + "; peer-multiple spread " + ("N/A (no peers configured)" if peer_score is None else "included") + \
        " -- approximation only, not real sector/macro data."
    return _kpi(score, detail)


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
    ("kpi9", "Market & Peer-Relative Pressure (approx.)"),
    ("kpi10", "Red Flags & Accounting Risk"),
]


def _composite(kpis: dict) -> "tuple[float | None, str | None, str]":
    scored = [kpis[k]["score"] for k, _ in KPI_ORDER if kpis[k]["score"] is not None]
    coverage = f"{len(scored)}/{len(KPI_ORDER)} KPIs"
    if not scored:
        return None, None, coverage
    composite = round(10 * (sum(scored) / len(scored)), 1)
    return composite, _light(composite / 10), coverage


def _generate(prompt: str, num_predict: int = 300) -> str:
    payload = json.dumps({
        "model": DASHBOARD_MODEL,
        "think": False,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_ctx": 4096, "num_predict": num_predict},
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

    kpi4, debt_flagged = _kpi_balance_sheet_debt(bundle)
    kpi6, peer_comparison = _kpi_valuation(ticker, bundle)

    kpis = {
        "kpi1": _kpi_fundamental_health(bundle),
        "kpi2": _kpi_earnings_quality(ticker),
        "kpi3": _kpi_revenue_margin(bundle),
        "kpi4": kpi4,
        "kpi5": _kpi_cash_flow_dividend(bundle),
        "kpi6": kpi6,
        "kpi7": _kpi_technical_momentum(daily, weekly),
        "kpi8": _kpi_volatility_event(ticker, daily, spy_daily),
        "kpi9": _kpi_market_peer_pressure(ticker, daily, spy_daily, peer_comparison),
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


def write_local_snapshot(reports: list) -> None:
    write_local_snapshot_rows([_to_ticker_row(r) for r in reports])


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

    write_local_snapshot(reports)
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
        cfg["risk_dashboard_database_id"] = db_id
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print(f"database created and saved to config: {db_id}")
        return

    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=None,
                         help="comma-separated ticker list; always regenerates, ignores no config")
    args = parser.parse_args()

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
