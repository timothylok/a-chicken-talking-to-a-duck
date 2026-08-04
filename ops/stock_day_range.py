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

Every run appends today's snapshot (price, lean, range) per ticker to
asr/logs/stock_day_range_history.json. Each day's report then opens with
an "Accuracy Check" section scoring the *previous* tracked day's lean and
range against today's actual close -- lean direction correct or not, and
whether the close landed inside the predicted range -- plus a cumulative
hit-rate across the whole history so drift/inaccuracy in the deterministic
lean rule becomes visible over time instead of silently assumed. Scoring
is retrospective and stateless: it re-derives from consecutive same-ticker
history entries every run rather than storing outcomes separately, same
"one source of truth" pattern as history.jsonl elsewhere in this project.

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
HISTORY_PATH = os.path.join(ROOT, "asr", "logs", "stock_day_range_history.json")
NZ_TZ = ZoneInfo("Pacific/Auckland")
EXCHANGE_TZ = ZoneInfo("America/New_York")

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


def _prev_business_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= dt.timedelta(days=1)
    return d


def _expected_latest_session(now_exchange: dt.datetime) -> dt.date:
    # The most recent US trading day whose closing bar should already be
    # published, given the current wall-clock time in the exchange's own
    # timezone -- today's session counts only once it has actually closed
    # (4pm ET); a weekend/holiday-adjacent run rolls back to the prior
    # weekday. No holiday calendar here (see the tolerance in _is_stale).
    d = now_exchange.date()
    if now_exchange.weekday() < 5 and now_exchange.time() >= dt.time(16, 0):
        return d
    return _prev_business_day(d)


def _is_stale(as_of: "str | None", now_exchange: dt.datetime) -> bool:
    # Real gap seen 2026-08-04: Yahoo's daily-bar array can lag its own
    # regularMarketTime by many hours, so a run can still see the *prior*
    # trading day's bar as "latest". One trading day of slack tolerates that
    # plus unrecognized market holidays; two or more means the data is
    # genuinely too old to report a same-day lean/range against.
    if not as_of:
        return True
    tolerated = _prev_business_day(_expected_latest_session(now_exchange))
    return dt.date.fromisoformat(as_of) < tolerated


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
# Accuracy history -- append-only log of daily snapshots, scored
# retrospectively against each other (no separate "outcome" bookkeeping).
# ---------------------------------------------------------------------------

def _load_history() -> list:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s: %s", HISTORY_PATH, exc)
        return []


def _save_history(history: list) -> None:
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def _update_history(history: list, reports: list) -> list:
    # Drop any existing same-(ticker,date) entry first so a same-day rerun
    # overwrites rather than duplicates -- same idempotency rationale as
    # write_html() overwriting the day's HTML file.
    kept = [
        h for h in history
        if not any(h["ticker"] == r["ticker"] and h["date"] == r["as_of"] for r in reports)
    ]
    new_entries = [
        {
            "date": r["as_of"], "ticker": r["ticker"], "price": r["price"], "lean": r["lean"],
            "range_low": r["range_low"], "range_high": r["range_high"], "rsi14": r["rsi14"],
        }
        for r in reports if r.get("as_of")
    ]
    return kept + new_entries


def _lean_direction(lean: str) -> "str | None":
    if lean.startswith("Bullish"):
        return "up"
    if lean.startswith("Bearish"):
        return "down"
    return None  # plain/insufficient-history Neutral leans aren't directionally falsifiable


def _score(prev: dict, actual_price: float) -> dict:
    direction = _lean_direction(prev["lean"])
    actual_direction = "up" if actual_price > prev["price"] else ("down" if actual_price < prev["price"] else "flat")
    lean_correct = (direction == actual_direction) if direction else None
    in_range = (
        None if prev.get("range_low") is None or prev.get("range_high") is None
        else prev["range_low"] <= actual_price <= prev["range_high"]
    )
    pct_move = (actual_price / prev["price"] - 1) * 100 if prev.get("price") else None
    return {
        "ticker": prev["ticker"], "predicted_date": prev["date"], "predicted_lean": prev["lean"],
        "range_low": prev.get("range_low"), "range_high": prev.get("range_high"),
        "baseline_price": prev["price"], "actual_price": actual_price,
        "actual_pct_move": pct_move, "lean_correct": lean_correct, "in_range": in_range,
    }


def _todays_accuracy(reports: list, history: list) -> list:
    # For each ticker in today's reports, score the most recent PRIOR
    # history entry (if any) against today's actual close.
    rows = []
    for r in reports:
        prior = [h for h in history if h["ticker"] == r["ticker"] and h["date"] < r["as_of"]]
        if not prior:
            continue
        latest_prior = max(prior, key=lambda h: h["date"])
        rows.append(_score(latest_prior, r["price"]))
    return rows


def _cumulative_accuracy(history: list) -> dict:
    # Pair every consecutive same-ticker entry in the full history --
    # entry[i] is the "prediction", entry[i+1]'s price is the "actual".
    by_ticker: dict = {}
    for h in history:
        by_ticker.setdefault(h["ticker"], []).append(h)
    lean_total = lean_correct = range_total = range_hits = 0
    for entries in by_ticker.values():
        entries.sort(key=lambda h: h["date"])
        for prev, cur in zip(entries, entries[1:]):
            scored = _score(prev, cur["price"])
            if scored["lean_correct"] is not None:
                lean_total += 1
                lean_correct += int(scored["lean_correct"])
            if scored["in_range"] is not None:
                range_total += 1
                range_hits += int(scored["in_range"])
    return {
        "lean_total": lean_total, "lean_correct": lean_correct,
        "range_total": range_total, "range_hits": range_hits,
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


def _lean_icon(lean: str) -> str:
    if lean.startswith("Bullish"):
        return "↗"  # up-right arrow
    if lean.startswith("Bearish"):
        return "↘"  # down-right arrow
    if "insufficient" in lean.lower():
        return "•"  # bullet -- no directional read at all
    return "↔"  # left-right arrow, mixed trend


def _badge(text: str, color: str) -> str:
    return sf._pill(text, color, _lean_icon(text))


LEAN_LEGEND = [
    ("Bullish", "LEAN_BULLISH"),
    ("Bullish (pullback risk)", "LEAN_BULLISH_PULLBACK"),
    ("Neutral", "LEAN_NEUTRAL"),
    ("Bearish", "LEAN_BEARISH"),
]


def _legend_html() -> str:
    chips = "".join(
        f'<span class="inline-flex items-center gap-1.5 rounded-full border border-slate-200 '
        f'bg-white px-3 py-1.5 text-xs font-medium text-slate-600">'
        f'<span class="h-2 w-2 rounded-full" style="background:{sf.PALETTE[key]}"></span>{sf._esc(label)}</span>'
        for label, key in LEAN_LEGEND
    )
    return f'<div class="flex flex-wrap gap-2">{chips}</div>'


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
    now_exchange = dt.datetime.now(EXCHANGE_TZ)
    if _is_stale(snap.get("as_of"), now_exchange):
        log.warning(
            "%s: latest bar (%s) is more than 1 trading day behind the expected session -- "
            "skipping rather than reporting a stale price/lean", ticker, snap.get("as_of"),
        )
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
{tailwind_head}
</head>
{page_header}{sections}
{page_footer}"""


def _accuracy_section_html(accuracy_rows: list, cumulative: dict) -> str:
    if cumulative["lean_total"] == 0 and cumulative["range_total"] == 0:
        return "<p>No prior-day prediction on record yet -- accuracy tracking starts today.</p>"

    lean_pct = f'{cumulative["lean_correct"]}/{cumulative["lean_total"]} ({cumulative["lean_correct"] / cumulative["lean_total"] * 100:.0f}%)' if cumulative["lean_total"] else "N/A"
    range_pct = f'{cumulative["range_hits"]}/{cumulative["range_total"]} ({cumulative["range_hits"] / cumulative["range_total"] * 100:.0f}%)' if cumulative["range_total"] else "N/A"
    summary = (
        f"<p>Cumulative since tracking began: lean direction correct {sf._esc(lean_pct)} "
        f"(Neutral leans excluded, not directionally falsifiable); actual close landed inside "
        f"the predicted range {sf._esc(range_pct)} of tracked days. Small sample -- treat as "
        f"provisional until this covers several weeks.</p>"
    )
    if not accuracy_rows:
        return summary + "<p>No trackable prior-day prediction for today's tickers (first day tracked for each, or a gap in the log).</p>"

    rows = [
        (
            row["ticker"],
            row["predicted_lean"],
            f'{st._fmt(row["range_low"])} - {st._fmt(row["range_high"])}',
            st._fmt(row["actual_price"]),
            st._fmt(row["actual_pct_move"], "%"),
            "N/A" if row["lean_correct"] is None else ("Correct" if row["lean_correct"] else "Wrong"),
            "N/A" if row["in_range"] is None else ("In range" if row["in_range"] else "Outside range"),
        )
        for row in accuracy_rows
    ]
    table = sf._table(
        ["Ticker", "Predicted lean (prior day)", "Predicted range", "Actual close", "Actual move", "Direction", "Range"],
        rows,
    )
    return summary + table


def _overview_table(reports: list) -> str:
    # The watchlist table proper -- ticker + lean pill left-aligned, the
    # numeric columns right-aligned with tabular numerals, matching the
    # scan-speed-first layout of the watchlist mockup this report follows.
    thead = (
        '<th class="px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-slate-500">Ticker</th>'
        '<th class="px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-slate-500">Lean</th>'
        '<th class="px-4 py-2.5 text-right text-xs font-semibold uppercase tracking-wide text-slate-500">Price</th>'
        '<th class="px-4 py-2.5 text-right text-xs font-semibold uppercase tracking-wide text-slate-500">RSI (14)</th>'
        '<th class="px-4 py-2.5 text-right text-xs font-semibold uppercase tracking-wide text-slate-500">~1&sigma; range</th>'
    )
    row_html = []
    for r in reports:
        rsi_text = st._fmt(r["rsi14"], digits=1)
        range_text = f'{st._fmt(r["range_low"])} - {st._fmt(r["range_high"])}'
        row_html.append(
            '<tr class="hover:bg-slate-50">'
            f'<td class="px-4 py-3 font-semibold text-slate-900">{sf._esc(r["ticker"])}</td>'
            f'<td class="px-4 py-3">{_badge(r["lean"], _lean_color(r["lean"]))}</td>'
            f'<td class="px-4 py-3 text-right font-medium tabular-nums text-slate-900">{sf._esc(st._fmt(r["price"]))}</td>'
            f'<td class="px-4 py-3 text-right tabular-nums">{sf._colored_span(rsi_text, sf._rsi_color(r["rsi14"]))}</td>'
            f'<td class="px-4 py-3 text-right tabular-nums text-slate-600">{sf._esc(range_text)}</td>'
            '</tr>'
        )
    return (
        '<div class="overflow-x-auto"><table class="min-w-full divide-y divide-slate-200 text-sm">'
        f'<thead class="bg-slate-50/80">{thead}</thead>'
        f'<tbody class="divide-y divide-slate-100">{"".join(row_html)}</tbody>'
        '</table></div>'
    )


def _html_page(reports: list, date_str: str, generated: str, accuracy_rows: list, cumulative: dict) -> str:
    overview_table = _overview_table(reports)
    accuracy_section = _accuracy_section_html(accuracy_rows, cumulative)

    sections = [
        sf._card("Overview", overview_table),
        sf._card("Accuracy Check", accuracy_section),
    ]
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
        title_html = f'{sf._esc(r["ticker"])} -- {_badge(r["lean"], _lean_color(r["lean"]))}'
        body_html = (
            f'{detail_table}{narrative_html}'
            f'<p class="cite">Fundamental context: {sf._esc(r["context"])}</p>'
        )
        sections.append(sf._card(title_html, body_html))

    meta = (
        f'<p class="text-xs text-slate-500">Generated {sf._esc(generated)} NZT.</p>'
        f'<p class="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">{sf._esc(CAVEAT)}</p>'
        f'<div class="pt-1">{_legend_html()}</div>'
    )
    page_header = sf.PAGE_HEADER.format(
        eyebrow="Daily Synthesis", heading="Stock Day Range", meta=meta,
    )
    return PAGE_TEMPLATE.format(
        date=sf._esc(date_str), tailwind_head=sf.TAILWIND_HEAD,
        page_header=page_header, sections="".join(sections), page_footer=sf.PAGE_FOOTER,
    )


def write_html(reports: list, now: dt.datetime, accuracy_rows: list, cumulative: dict) -> str:
    date_str = now.strftime("%Y-%m-%d")
    out_path = os.path.join(OUTPUT_DIR, f"{date_str}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_html_page(reports, date_str, now.strftime("%Y-%m-%d %H:%M"), accuracy_rows, cumulative))
    return out_path


def run() -> int:
    dashboard = _load_dashboard_snapshot()
    history = _load_history()
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

    accuracy_rows = _todays_accuracy(reports, history)
    for row in accuracy_rows:
        log.info(
            "%s: prior lean=%s -> actual %s (%s), range %s",
            row["ticker"], row["predicted_lean"],
            st._fmt(row["actual_price"]), st._fmt(row["actual_pct_move"], "%"),
            "in range" if row["in_range"] else ("outside range" if row["in_range"] is not None else "N/A"),
        )
    history = _update_history(history, reports)
    _save_history(history)
    cumulative = _cumulative_accuracy(history)

    out_path = write_html(reports, now, accuracy_rows, cumulative)
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
