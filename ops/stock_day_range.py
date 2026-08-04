"""Daily 1-day price-range/lean synthesis for the stock watchlist.

Not a new SEC-gated report tier -- a same-day synthesis over data the other
categories already produced, same "reuse in-process, don't refetch" pattern
`stock_risk_dashboard.py` (Category 6) uses for its own composite score:
  - Technical snapshot (price, SMA50/200, RSI14, support/resistance, 20-day
    realized volatility) is fetched fresh from Yahoo here rather than
    scraped from Category 4's HTML report -- that HTML embeds its numbers
    inside minified chart JS, which is not a stable thing to parse.
  - Fundamental/earnings/valuation/relative-strength grounding is read from
    `dashboard/data/latest.json`, written daily by Category 6
    (`ops/deploy_dashboard.py`, ~10:00 NZT). If a ticker is missing from
    that file (a real gap seen 2026-08-04: NVDA's KPI generation failed
    that day even though its Category 4 technicals succeeded) or has null
    KPIs (e.g. SPCX, no SEC filings), the report says so rather than
    guessing.

The 1-day "lean" (Bullish/Bearish/Neutral) is a deterministic rule over
price vs. SMA50/SMA200 and RSI14 -- never LLM-decided, so it's reproducible
run to run. `qwen3:8b` (`think:false`, same model as Category 4) only
narrates the reasoning paragraph from those already-computed numbers plus
the Category 6 grounding text -- "compute first, narrate second", same
guardrail as every other category in this project.

Explicitly not investment advice, and explicitly not a price prediction:
single-day market moves are dominated by noise no model here can resolve.
The "1-sigma range" is a volatility-based band (price +/- 20-day realized
daily stdev), not a forecast of where the price will land.

Runs daily via the "VoiceOS Stock Day Range" scheduled task, 10:15 NZT --
15 min after "VoiceOS Risk Dashboard Daily" (10:00 NZT), the last of the
automated daily report chain, so `dashboard/data/latest.json` is fresh.

Output: one self-contained HTML file per NZT day at
content/StockDayRange/{date}.html (overwritten on same-day reruns) --
all generated reports/HTML in this project live under content/, same
convention as Category 1/4's own output dirs.

Run manually: python ops/stock_day_range.py
"""

import datetime as dt
import json
import logging
import os
import statistics
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_fundamentals as sf  # noqa: E402
import stock_technicals as st  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "asr"))
from router import _sma, _rsi  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT, "content", "StockDayRange")
DASHBOARD_JSON = os.path.join(ROOT, "dashboard", "data", "latest.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", "stock_day_range.log")
NZ_TZ = ZoneInfo("Pacific/Auckland")

WATCHLIST = [
    t.strip().upper()
    for t in os.environ.get("STOCK_WATCHLIST", "AAPL,MSFT,NVDA,TSLA,GOOGL,AMZN,META,AON,SPCX").split(",")
    if t.strip()
]

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
log = logging.getLogger("stock_day_range")
log.propagate = False
log.setLevel(logging.INFO)
if not log.handlers:
    _handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_handler)

# KPIs pulled into the grounding context handed to the narrator, and the
# plain-English label each one gets in the prompt/report.
GROUNDING_KPIS = [
    ("kpi2", "Earnings quality"),
    ("kpi3", "Revenue/margin trend"),
    ("kpi6", "Valuation"),
    ("kpi9", "Relative strength vs SPY"),
    ("kpi10", "Red flags"),
]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _load_dashboard_snapshot() -> dict:
    try:
        with open(DASHBOARD_JSON, encoding="utf-8") as f:
            rows = json.load(f)
        return {row["ticker"]: row for row in rows}
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        log.warning("could not read %s: %s", DASHBOARD_JSON, exc)
        return {}


def _grounding_text(dash_row: "dict | None") -> str:
    if not dash_row:
        return "Not available -- ticker missing from today's Category 6 risk-dashboard run."
    parts = [f"Composite risk score {dash_row.get('composite')}/10 ({dash_row.get('compositeLight')})."]
    kpis = dash_row.get("kpis") or {}
    for key, label in GROUNDING_KPIS:
        kpi = kpis.get(key) or {}
        detail = kpi.get("detail")
        if detail and kpi.get("score") is not None:
            parts.append(f"{label}: {detail}")
    return " ".join(parts)


def _technical_snapshot(ticker: str) -> "dict | None":
    daily = st._fetch_series(ticker, "1y", "1d")
    closes, highs, lows = daily["closes"], daily["highs"], daily["lows"]
    if not closes:
        return None
    price = daily["price"] or closes[-1]
    sma50, sma200, rsi14 = _sma(closes, 50), _sma(closes, 200), _rsi(closes, 14)
    support, resistance = st._support_resistance(highs, lows, price)
    returns = [
        (closes[i] / closes[i - 1] - 1) * 100
        for i in range(max(1, len(closes) - 20), len(closes))
        if closes[i - 1]
    ]
    stdev = statistics.pstdev(returns) if len(returns) >= 2 else None
    return {
        "ticker": ticker,
        "price": price,
        "sma50": sma50,
        "sma200": sma200,
        "rsi14": rsi14,
        "support": support,
        "resistance": resistance,
        "daily_stdev_pct": stdev,
        "range_low": price * (1 - stdev / 100) if stdev is not None else None,
        "range_high": price * (1 + stdev / 100) if stdev is not None else None,
        "as_of": daily["dates"][-1].isoformat() if daily["dates"] and daily["dates"][-1] else None,
    }


# ---------------------------------------------------------------------------
# Deterministic lean -- trend + RSI only, same rule every run.
# ---------------------------------------------------------------------------

def _lean(price: float, sma50: "float | None", sma200: "float | None", rsi: "float | None") -> str:
    if sma50 is None or sma200 is None:
        return "Neutral -- insufficient price history for a trend read"
    if price > sma50 and price > sma200:
        if rsi is not None and rsi >= 70:
            return "Bullish (overbought -- pullback risk)"
        return "Bullish"
    if price < sma50 and price < sma200:
        if rsi is not None and rsi <= 30:
            return "Bearish (oversold -- bounce risk)"
        return "Bearish"
    return "Neutral -- mixed trend"


def _lean_color(lean: str) -> str:
    # "Bearish (oversold -- bounce risk)" gets the same red as plain
    # "Bearish" -- ops/palette.env only defines one bearish color, unlike
    # the bullish side which has a distinct pullback-risk shade.
    if lean.startswith("Bullish (overbought"):
        return sf.PALETTE["LEAN_BULLISH_PULLBACK"]
    if lean.startswith("Bullish"):
        return sf.PALETTE["LEAN_BULLISH"]
    if lean.startswith("Bearish"):
        return sf.PALETTE["LEAN_BEARISH"]
    if "insufficient" in lean.lower():
        return sf.PALETTE["LEAN_INSUFFICIENT"]
    return sf.PALETTE["LEAN_NEUTRAL"]


def _badge(text: str, color: str) -> str:
    return f'<span class="badge" style="background:{color}; color:#fff">{sf._esc(text)}</span>'


# ---------------------------------------------------------------------------
# LLM narration -- numbers are already fixed above; this only explains them.
# ---------------------------------------------------------------------------

NARRATIVE_PROMPT = """Write a short one-day trading-outlook note for {ticker}, for an internal automated report. Use ONLY the numbers given below -- never invent a price, target, or news event not listed here. Explain in 2-4 plain sentences why the lean is "{lean}", referencing the trend/RSI/volatility figures and the fundamental context. End with the single biggest risk to that lean. No markdown, no headers, plain sentences only.

Price: {price}
50-day SMA: {sma50}
200-day SMA: {sma200}
14-day RSI: {rsi}
Nearest support: {support}
Nearest resistance: {resistance}
20-day realized daily volatility: {stdev}
Approx 1-sigma next-day range: {range_low} to {range_high}
Fundamental/earnings/valuation context (from today's Category 6 risk dashboard): {context}
"""


def _narrate(snap: dict, lean: str, context: str) -> str:
    prompt = NARRATIVE_PROMPT.format(
        ticker=snap["ticker"], lean=lean,
        price=st._fmt(snap["price"]), sma50=st._fmt(snap["sma50"]), sma200=st._fmt(snap["sma200"]),
        rsi=st._fmt(snap["rsi14"], digits=1), support=st._fmt(snap["support"]), resistance=st._fmt(snap["resistance"]),
        stdev=st._fmt(snap["daily_stdev_pct"], "%"),
        range_low=st._fmt(snap["range_low"]), range_high=st._fmt(snap["range_high"]),
        context=context,
    )
    try:
        return st._generate(prompt, num_predict=280)
    except Exception as exc:
        log.error("%s: narration failed: %s", snap["ticker"], exc)
        return "(narrative generation failed -- see numbers above)"


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_ticker_report(ticker: str, dashboard: dict) -> "dict | None":
    snap = _technical_snapshot(ticker)
    if not snap:
        return None
    lean = _lean(snap["price"], snap["sma50"], snap["sma200"], snap["rsi14"])
    context = _grounding_text(dashboard.get(ticker))
    narrative = _narrate(snap, lean, context)
    return {**snap, "lean": lean, "context": context, "narrative": narrative}


CAVEAT = (
    "Single-day price moves are dominated by noise -- no model, including this one, can reliably "
    "predict a specific price for tomorrow. Each ticker below gets a directional lean (rule-based, from "
    "trend and RSI) and a volatility-based range, not a price target. Not investment advice."
)

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stock Day Range -- {date}</title>
<style>
  :root {{ --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --line: #e0e0e0; --accent: #b45309; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #16181d; --fg: #e8e8e8; --muted: #9a9a9a; --line: #333; --accent: #f59e0b; }}
  }}
  body {{ margin: 0 auto; max-width: 56rem; padding: 2rem 1.25rem 4rem;
         background: var(--bg); color: var(--fg);
         font-family: -apple-system, "Segoe UI", sans-serif; line-height: 1.6; }}
  h1 {{ font-size: 1.6rem; margin-bottom: 0.25rem; }}
  h2 {{ font-size: 1.15rem; margin-top: 2.5rem; border-bottom: 1px solid var(--line); padding-bottom: 0.35rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 0.75rem; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line); vertical-align: top; }}
  th {{ font-size: 0.85rem; color: var(--muted); font-weight: 600; }}
  p.cite {{ color: var(--muted); font-size: 0.8rem; margin-top: 0.6rem; }}
  .meta {{ color: var(--muted); font-size: 0.9rem; }}
  .caveat {{ border: 1px solid var(--accent); border-radius: 0.4rem; padding: 0.75rem 1rem;
            margin-top: 1rem; font-size: 0.9rem; }}
  .badge {{ display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px;
           font-weight: 700; font-size: 0.85rem; background: var(--line); }}
</style>
</head>
<body>
<h1>Stock Day Range</h1>
<p class="meta">Generated {generated} NZT.</p>
<p class="caveat">{caveat}</p>
<h2>Overview</h2>
{overview_table}
{ticker_sections}
</body>
</html>
"""


def _overview_table(reports: list) -> str:
    rows = [
        (
            r["ticker"],
            st._fmt(r["price"]),
            (r["lean"], _badge(r["lean"], _lean_color(r["lean"]))),
            (st._fmt(r["rsi14"], digits=1), sf._colored_span(st._fmt(r["rsi14"], digits=1), sf._rsi_color(r["rsi14"]))),
            f'{st._fmt(r["range_low"])} - {st._fmt(r["range_high"])}',
        )
        for r in reports
    ]
    return sf._table2(["Ticker", "Price", "Lean", "RSI(14)", "~1-sigma range"], rows)


def _html_page(reports: list, date_str: str, generated: str) -> str:
    overview_table = _overview_table(reports)

    sections = []
    for r in reports:
        rsi_text = st._fmt(r["rsi14"], digits=1)
        detail_table = sf._table2(["Metric", "Value"], [
            ("Price", st._fmt(r["price"])),
            ("As of", r.get("as_of") or "N/A"),
            ("50-day SMA", st._fmt(r["sma50"])),
            ("200-day SMA", st._fmt(r["sma200"])),
            ("14-day RSI", (rsi_text, sf._colored_span(rsi_text, sf._rsi_color(r["rsi14"])))),
            ("Support", st._fmt(r["support"])),
            ("Resistance", st._fmt(r["resistance"])),
            ("20-day realized daily volatility", st._fmt(r["daily_stdev_pct"], "%")),
            ("~1-sigma next-day range", f'{st._fmt(r["range_low"])} - {st._fmt(r["range_high"])}'),
        ])
        narrative_html = "".join(
            f"<p>{sf._esc(line.strip())}</p>" for line in r["narrative"].split("\n") if line.strip()
        ) or "<p>N/A</p>"
        sections.append(
            f'<section><h2>{sf._esc(r["ticker"])} -- {_badge(r["lean"], _lean_color(r["lean"]))}</h2>'
            f'{detail_table}{narrative_html}'
            f'<p class="cite">Fundamental context: {sf._esc(r["context"])}</p></section>'
        )

    return PAGE_TEMPLATE.format(
        date=sf._esc(date_str), generated=sf._esc(generated), caveat=sf._esc(CAVEAT),
        overview_table=overview_table, ticker_sections="".join(sections),
    )


def write_html(reports: list, now: dt.datetime) -> str:
    date_str = now.strftime("%Y-%m-%d")
    out_path = os.path.join(OUTPUT_DIR, f"{date_str}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_html_page(reports, date_str, now.strftime("%Y-%m-%d %H:%M")))
    return out_path


def run() -> int:
    dashboard = _load_dashboard_snapshot()
    now = dt.datetime.now(NZ_TZ)
    reports = []
    for ticker in WATCHLIST:
        try:
            report = build_ticker_report(ticker, dashboard)
            if report:
                reports.append(report)
                log.info("%s: lean=%s", ticker, report["lean"])
            else:
                log.warning("%s: no price data, skipped", ticker)
        except Exception as exc:
            log.error("%s: report failed: %s", ticker, exc)
    if not reports:
        log.error("no tickers produced a report; not writing HTML")
        return 0
    out_path = write_html(reports, now)
    log.info("wrote %d/%d tickers to %s", len(reports), len(WATCHLIST), out_path)
    return len(reports)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=None, help="comma-separated ticker list")
    args = parser.parse_args()
    if args.tickers:
        global WATCHLIST
        WATCHLIST = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    try:
        written = run()
        print(f"wrote {written}/{len(WATCHLIST)} tickers")
    except Exception as exc:
        log.error("run failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
