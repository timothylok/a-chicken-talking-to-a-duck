"""Category 4: technical analysis (weekly/daily chart reads, volume &
accumulation, relative strength vs SPY, historical earnings-day
volatility), generated daily for the watchlist and saved to Notion.

Runs daily via the "VoiceOS Technicals Daily" scheduled task, 09:30 NZT --
replaces the older, much thinner `ops/stock_daily.py` (price/SMA/RSI/AI-take
only), which this file's prompt 24 section supersedes. Unlike Category
1-3 (`stock_fundamentals.py`/`stock_earnings.py`/`stock_valuation.py`),
this report is NOT SEC-gated: prompts 23/24/27/28 need only Yahoo Finance
price data, so `SPCX` stays in the watchlist here (unlike
`sf.EXCLUDE_NO_SEC_FILINGS`) -- only prompt 29 (needs 8-K filings) N/As
per-ticker when no SEC filings exist, rather than excluding the ticker.

Scope decisions made with the user before building, and why:
  - Prompts 25/26 (Pine Script code generation from a free-form
    description) don't fit a fixed-watchlist daily report at all -- they
    became a separate Slack/text-only on-demand command in `asr/router.py`
    (`PINE_INDICATOR`/`PINE_STRATEGY`), not part of this file.
  - Prompt 29 originally called for an implied move from an at-the-money
    options straddle. Live-tested 2026-07-28:
    `query1/query2.finance.yahoo.com/v7/finance/options/{ticker}` returns
    401 Unauthorized unauthenticated (Yahoo gates this endpoint behind a
    crumb/cookie handshake the chart endpoint doesn't need) -- confirmed
    on both query hosts and with a dummy crumb param. Ships
    historical-only instead: the average realized earnings-day price move
    over however many real quarterly 8-Ks (item 2.02) are actually found,
    never assuming a fixed "8 quarters" window not backed by real data
    (same lesson as stock_valuation.py's 5-year-multiples coverage).
  - `SPCX` has only ~8 weekly / ~30 daily bars of real history (confirmed
    live 2026-07-28) -- EMA50w/200w, weekly RSI/S&R, daily SMA50/200, and
    both relative-strength windows all legitimately N/A for it. Only
    current price, daily RSI14, OBV/volume-MA, and Yahoo's own
    fiftyTwoWeekHigh/Low (computed by Yahoo regardless of bar count)
    survive. This is a real data gap, not a bug -- every helper below
    returns None on insufficient data rather than a guessed number.

Setup:
  1. Requires ops/notion.json already configured with api_key +
     database_id (see notion_sync.py's own --setup).
  2. python ops/stock_technicals.py --setup
     (creates the "股票技術分析 Technical Analysis" database under the
     same parent page as the existing Voice History database, adds
     technicals_database_id to the config)

Until technicals_database_id exists in the config, runs are silent no-ops.
"""

import datetime as dt
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_fundamentals as sf  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "asr"))
from router import _sma, _rsi  # noqa: E402

# Not sf.OLLAMA_MODEL (lfm2.5) or a blind default -- same reasoning as
# stock_earnings.py/stock_valuation.py: structured trend/entry judgment the
# user may act on, not spoken-Cantonese brevity.
TECHNICALS_MODEL = "qwen3:8b"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "asr", "logs", "stock_technicals.log")
CONFIG = os.path.join(ROOT, "ops", "notion.json")
NOTION_VERSION = "2022-06-28"
NZ_TZ = ZoneInfo("Pacific/Auckland")

# Vendored (Apache-2.0), not npm-installed, so the generated HTML report stays
# a single self-contained file with no CDN/runtime dependency -- same
# rationale as everything else in this file being stdlib-only.
CHART_LIB_PATH = os.path.join(ROOT, "ops", "vendor", "lightweight-charts.standalone.production.js")

WATCHLIST = [
    t.strip().upper()
    for t in os.environ.get("STOCK_WATCHLIST", "AAPL,MSFT,NVDA,TSLA,GOOGL,AMZN,META,SPCX").split(",")
    if t.strip()
]
BENCHMARK_TICKER = "SPY"

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={range}&interval={interval}"

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
# logging.basicConfig() would be a silent no-op here -- importing
# stock_fundamentals above already configured the root logger via its own
# basicConfig() call (same gotcha stock_earnings.py/stock_valuation.py hit).
log = logging.getLogger("stock_technicals")
log.propagate = False
log.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def _fetch_series(ticker: str, range_: str, interval: str) -> dict:
    req = urllib.request.Request(
        CHART_URL.format(ticker=urllib.parse.quote(ticker, safe=""), range=range_, interval=interval),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read())
    result = (payload.get("chart") or {}).get("result")
    if not result:
        raise ValueError(f"no chart data for {ticker}")
    node = result[0]
    meta = node["meta"]
    quote = node["indicators"]["quote"][0]
    timestamps = node.get("timestamp") or []
    raw_closes = quote.get("close") or []
    closes, opens, highs, lows, volumes, dates = [], [], [], [], [], []
    for i, c in enumerate(raw_closes):
        if c is None:
            continue
        closes.append(c)
        o = quote.get("open", [None] * len(raw_closes))[i]
        h = quote.get("high", [None] * len(raw_closes))[i]
        l = quote.get("low", [None] * len(raw_closes))[i]
        v = quote.get("volume", [None] * len(raw_closes))[i]
        opens.append(o if o is not None else c)
        highs.append(h if h is not None else c)
        lows.append(l if l is not None else c)
        volumes.append(v if v is not None else 0)
        dates.append(dt.date.fromtimestamp(timestamps[i]) if i < len(timestamps) else None)
    return {
        "closes": closes, "opens": opens, "highs": highs, "lows": lows, "volumes": volumes, "dates": dates,
        "currency": meta.get("currency") or "",
        "price": meta.get("regularMarketPrice", closes[-1] if closes else None),
        "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
        "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
    }


# ---------------------------------------------------------------------------
# Pure-Python math helpers -- no new dependency, same style as asr/router.py's
# existing _sma/_rsi (reused here via import, not duplicated).
# ---------------------------------------------------------------------------

def _ema(closes: list, period: int) -> "float | None":
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    k = 2 / (period + 1)
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def _rolling_sma(closes: list, period: int) -> list:
    # Same math as _sma, but returns the full per-bar series (for chart
    # overlay lines) instead of only the latest value.
    out = [None] * len(closes)
    if len(closes) < period:
        return out
    window_sum = sum(closes[:period])
    out[period - 1] = window_sum / period
    for i in range(period, len(closes)):
        window_sum += closes[i] - closes[i - period]
        out[i] = window_sum / period
    return out


def _rolling_ema(closes: list, period: int) -> list:
    # Same seed/recursion as _ema, full per-bar series for chart overlays.
    out = [None] * len(closes)
    if len(closes) < period:
        return out
    ema = sum(closes[:period]) / period
    out[period - 1] = ema
    k = 2 / (period + 1)
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1 - k)
        out[i] = ema
    return out


def _support_resistance(highs: list, lows: list, price: float, k: int = 2) -> "tuple[float | None, float | None]":
    # Simple fractal swing-high/low detector: a bar is a pivot if it's the
    # extreme of the k bars on each side. Nearest pivot low below price =
    # support, nearest pivot high above price = resistance.
    n = len(highs)
    if n < 2 * k + 1:
        return None, None
    supports, resistances = [], []
    for i in range(k, n - k):
        window_h = highs[i - k:i + k + 1]
        if highs[i] == max(window_h):
            resistances.append(highs[i])
        window_l = lows[i - k:i + k + 1]
        if lows[i] == min(window_l):
            supports.append(lows[i])
    below = [s for s in supports if s < price]
    above = [r for r in resistances if r > price]
    return (max(below) if below else None), (min(above) if above else None)


def _obv(closes: list, volumes: list) -> list:
    if len(closes) < 2:
        return []
    series = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            series.append(series[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            series.append(series[-1] - volumes[i])
        else:
            series.append(series[-1])
    return series


def _pct_return(closes: list, lookback: int) -> "float | None":
    if len(closes) <= lookback:
        return None
    start = closes[-1 - lookback]
    if not start:
        return None
    return (closes[-1] - start) / start * 100


def _fmt(val, suffix: str = "", digits: int = 2) -> str:
    if val is None:
        return "N/A"
    return f"{val:,.{digits}f}{suffix}"


# ---------------------------------------------------------------------------
# LLM narrative -- "compute first, narrate second": every prompt below only
# ever explains numbers already computed deterministically above.
# ---------------------------------------------------------------------------

def _generate(prompt: str, num_predict: int = 500) -> str:
    payload = json.dumps({
        "model": TECHNICALS_MODEL,
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


WEEKLY_PROMPT = (
    "Act as a senior technical analyst. {ticker}'s weekly data: close {close}, "
    "50-week EMA {ema50}, 200-week EMA {ema200}, 14-period weekly RSI {rsi}, "
    "52-week range {low52}-{high52}, nearest weekly support {support}, "
    "nearest weekly resistance {resistance}. Give a 4-section read: trend "
    "state, momentum, key support and resistance, and a 1-3 month swing "
    "thesis with an explicit invalidation level. Any value listed as N/A "
    "wasn't available -- say so plainly, don't guess a number for it. "
    "Under 250 words."
)

DAILY_PROMPT = (
    "Act as a technical analyst evaluating a swing-trade setup. {ticker}'s "
    "daily data: price {price}, 14-period daily RSI {rsi}, 50-day SMA "
    "{sma50}, 200-day SMA {sma200}, nearest daily support {support}, "
    "nearest daily resistance {resistance}. Identify whether the setup "
    "favors a long entry, short entry, or wait-and-watch -- state your "
    "verdict as exactly one of the words 'Long', 'Short', or 'Wait' at the "
    "very start of your reply. Any value listed as N/A wasn't available -- "
    "say so plainly, don't guess a number for it. Under 200 words."
)

VOLUME_PROMPT = (
    "{ticker}'s volume data over the last ~30 trading days: On-Balance "
    "Volume trend is {obv_trend}, current volume vs its 20-day average is "
    "{vol_vs_avg}, price changed {price_chg}% over the same window. "
    "Identify whether volume is confirming or diverging from price, and "
    "comment on accumulation vs distribution patterns. Under 200 words."
)


# ---------------------------------------------------------------------------
# Section 23: Weekly Chart Read
# ---------------------------------------------------------------------------

def _section_weekly(ticker: str, weekly: dict) -> dict:
    closes, highs, lows = weekly["closes"], weekly["highs"], weekly["lows"]
    close = closes[-1] if closes else None
    ema50, ema200 = _ema(closes, 50), _ema(closes, 200)
    rsi = _rsi(closes, 14)
    window = 104  # ~2 years of weekly bars
    support, resistance = _support_resistance(highs[-window:], lows[-window:], close, k=2) if close else (None, None)
    prompt = WEEKLY_PROMPT.format(
        ticker=ticker, close=_fmt(close), ema50=_fmt(ema50), ema200=_fmt(ema200),
        rsi=_fmt(rsi, digits=1), low52=_fmt(weekly.get("fifty_two_week_low")),
        high52=_fmt(weekly.get("fifty_two_week_high")), support=_fmt(support), resistance=_fmt(resistance),
    )
    try:
        read = _generate(prompt, num_predict=500)
    except Exception as exc:
        log.warning("%s: weekly read generation failed: %s", ticker, exc)
        read = "N/A -- narrative generation failed."
    return {
        "close": close, "ema50w": ema50, "ema200w": ema200, "rsi14w": rsi,
        "high52w": weekly.get("fifty_two_week_high"), "low52w": weekly.get("fifty_two_week_low"),
        "support": support, "resistance": resistance, "read": read,
    }


# ---------------------------------------------------------------------------
# Section 24: Daily Chart Read for Swing Entries
# ---------------------------------------------------------------------------

_SETUP_RE = re.compile(r"\b(Long|Short|Wait)\b", re.IGNORECASE)


def _section_daily(ticker: str, daily: dict) -> dict:
    closes, highs, lows = daily["closes"], daily["highs"], daily["lows"]
    price = daily.get("price")
    sma50, sma200 = _sma(closes, 50), _sma(closes, 200)
    rsi = _rsi(closes, 14)
    window = 90
    support, resistance = _support_resistance(highs[-window:], lows[-window:], price, k=2) if price else (None, None)
    prompt = DAILY_PROMPT.format(
        ticker=ticker, price=_fmt(price), rsi=_fmt(rsi, digits=1), sma50=_fmt(sma50),
        sma200=_fmt(sma200), support=_fmt(support), resistance=_fmt(resistance),
    )
    try:
        read = _generate(prompt, num_predict=400)
    except Exception as exc:
        log.warning("%s: daily read generation failed: %s", ticker, exc)
        read = "N/A -- narrative generation failed."
    match = _SETUP_RE.search(read)
    setup = match.group(1).capitalize() if match else "Wait"
    return {
        "price": price, "sma50": sma50, "sma200": sma200, "rsi14": rsi,
        "support": support, "resistance": resistance, "setup": setup, "read": read,
    }


# ---------------------------------------------------------------------------
# Section 27: Volume & Accumulation
# ---------------------------------------------------------------------------

def _section_volume(ticker: str, daily: dict) -> dict:
    closes, volumes = daily["closes"], daily["volumes"]
    obv = _obv(closes, volumes)
    obv_trend = None
    if len(obv) > 20:
        delta = obv[-1] - obv[-21]
        obv_trend = "Rising" if delta > 0 else ("Falling" if delta < 0 else "Flat")
    vol_ma20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else None
    price_chg = _pct_return(closes, 20)
    vol_vs_avg_pct = ((volumes[-1] - vol_ma20) / vol_ma20 * 100) if vol_ma20 else None
    signal = None
    if price_chg is not None and len(obv) > 20:
        obv_chg = obv[-1] - obv[-21]
        signal = "Confirming" if (price_chg > 0) == (obv_chg > 0) else "Diverging"
    prompt = VOLUME_PROMPT.format(
        ticker=ticker, obv_trend=obv_trend or "N/A",
        vol_vs_avg=(f"{vol_vs_avg_pct:+.0f}% vs average" if vol_vs_avg_pct is not None else "N/A"),
        price_chg=_fmt(price_chg, digits=1),
    )
    try:
        notes = _generate(prompt, num_predict=400)
    except Exception as exc:
        log.warning("%s: volume notes generation failed: %s", ticker, exc)
        notes = "N/A -- narrative generation failed."
    return {"obv_trend": obv_trend, "vol_ma20": vol_ma20, "signal": signal, "notes": notes}


# ---------------------------------------------------------------------------
# Section 28: Relative Strength vs SPY -- fully deterministic, no LLM.
# ---------------------------------------------------------------------------

def _section_relative_strength(ticker: str, daily: dict, spy_daily: dict) -> dict:
    rs3 = _pct_return(daily["closes"], 63)
    rs6 = _pct_return(daily["closes"], 126)
    spy3 = _pct_return(spy_daily["closes"], 63)
    spy6 = _pct_return(spy_daily["closes"], 126)
    diff3 = (rs3 - spy3) if rs3 is not None and spy3 is not None else None
    diff6 = (rs6 - spy6) if rs6 is not None and spy6 is not None else None
    verdict = None
    if diff3 is not None:
        verdict = "Outperforming" if diff3 > 0 else "Underperforming"
    if diff3 is None and diff6 is None:
        notes = f"{ticker}: insufficient price history for a 3-month or 6-month relative-strength comparison against SPY."
    else:
        notes = (
            f"{ticker} vs SPY -- 3-month: {_fmt(diff3, suffix=' pp')}, "
            f"6-month: {_fmt(diff6, suffix=' pp')} (percentage-point return differential). "
            f"{ticker} is {(verdict or 'N/A').lower()} SPY over the shorter window."
        )
    return {"rs_3m_diff": diff3, "rs_6m_diff": diff6, "verdict": verdict, "notes": notes}


# ---------------------------------------------------------------------------
# Section 29: Earnings Volatility Expectation -- historical-only (Yahoo's
# options endpoint is unauthenticated-401, see module docstring). N/A per
# ticker when no CIK/SEC filings exist, not an exclusion from the report.
# ---------------------------------------------------------------------------

def _recent_earnings_8ks(subs: dict, n: int = 8) -> list:
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    items = recent.get("items", [])
    out = []
    for i, f in enumerate(forms):
        if f == "8-K" and "2.02" in (items[i] if i < len(items) else "").split(","):
            out.append({"accn": recent["accessionNumber"][i], "filed": recent["filingDate"][i]})
            if len(out) == n:
                break
    return out


def _earnings_day_move(daily: dict, filed_date_str: str) -> "float | None":
    dates, closes = daily["dates"], daily["closes"]
    try:
        filed = dt.date.fromisoformat(filed_date_str)
    except ValueError:
        return None
    idx = next((i for i, d in enumerate(dates) if d and d >= filed), None)
    if idx is None or idx == 0 or idx + 1 >= len(closes):
        return None
    before, after = closes[idx - 1], closes[idx + 1]
    if not before:
        return None
    return abs(after / before - 1) * 100


def _section_earnings_volatility(ticker: str, daily: dict) -> dict:
    if ticker in sf.EXCLUDE_NO_SEC_FILINGS:
        return {"avg_move_pct": None, "basis": f"{ticker} has no SEC filings -- N/A."}
    try:
        cik = sf._cik_for_ticker(ticker)
        if not cik:
            return {"avg_move_pct": None, "basis": "no CIK found in SEC EDGAR -- N/A."}
        subs = sf._submissions(cik)
        hits = _recent_earnings_8ks(subs, n=8)
    except Exception as exc:
        log.warning("%s: earnings-8K lookup failed: %s", ticker, exc)
        return {"avg_move_pct": None, "basis": "SEC EDGAR lookup failed -- N/A."}
    if not hits:
        return {"avg_move_pct": None, "basis": "no item 2.02 8-K filings found -- N/A."}
    moves = []
    for hit in hits:
        move = _earnings_day_move(daily, hit["filed"])
        if move is not None:
            moves.append((hit["filed"], move))
    if not moves:
        return {"avg_move_pct": None, "basis": "8-K filing dates found but outside the 2-year daily price window -- N/A."}
    avg_move = sum(m for _, m in moves) / len(moves)
    dates_used = sorted(d for d, _ in moves)
    basis = (
        f"Historical-only (Yahoo's options endpoint is unauthenticated-401, no implied "
        f"volatility available). Based on {len(moves)} real quarterly earnings 8-K(s), "
        f"{dates_used[0]} to {dates_used[-1]}."
    )
    return {"avg_move_pct": avg_move, "basis": basis}


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_report(ticker: str, spy_daily: dict) -> "dict | None":
    try:
        weekly = _fetch_series(ticker, "10y", "1wk")
        daily = _fetch_series(ticker, "2y", "1d")
    except Exception as exc:
        log.error("%s: price fetch failed: %s", ticker, exc)
        return None
    return {
        "ticker": ticker,
        "currency": daily.get("currency") or weekly.get("currency") or "",
        "weekly": _section_weekly(ticker, weekly),
        "daily": _section_daily(ticker, daily),
        "volume": _section_volume(ticker, daily),
        "rs": _section_relative_strength(ticker, daily, spy_daily),
        "earnings": _section_earnings_volatility(ticker, daily),
        # Raw OHLCV kept only for the HTML chart section below -- not
        # written to Notion (_page_properties only reads the sub-dicts
        # above).
        "daily_raw": daily,
        "weekly_raw": weekly,
    }


# ---------------------------------------------------------------------------
# HTML report -- same self-contained-file pattern as stock_fundamentals.py's
# Category 1 (content/<category>/<ticker>.html), which this script lacked
# until now (it previously only wrote to Notion).
# ---------------------------------------------------------------------------

OUTPUT_DIR = os.path.join(ROOT, "content", "stock-technicals")
os.makedirs(OUTPUT_DIR, exist_ok=True)

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{ticker} -- Category 4 Technical Analysis</title>
<style>
  :root {{ --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --line: #e0e0e0; --accent: #b45309; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #16181d; --fg: #e8e8e8; --muted: #9a9a9a; --line: #333; --accent: #f59e0b; }}
  }}
  body {{ margin: 0 auto; max-width: 56rem; padding: 2rem 1.25rem 4rem;
         background: var(--bg); color: var(--fg);
         font-family: -apple-system, "Segoe UI", sans-serif; line-height: 1.6; }}
  h1 {{ font-size: 1.6rem; margin-bottom: 0.25rem; }}
  h2 {{ font-size: 1.15rem; margin-top: 2.5rem; border-bottom: 1px solid var(--line);
       padding-bottom: 0.35rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 0.75rem; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line);
           vertical-align: top; }}
  th {{ font-size: 0.85rem; color: var(--muted); font-weight: 600; }}
  p.cite {{ color: var(--muted); font-size: 0.8rem; margin-top: 0.6rem; }}
  .meta {{ color: var(--muted); font-size: 0.9rem; }}
  .badge {{ display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px;
           font-weight: 700; font-size: 0.85rem; background: var(--line); }}
  pre {{ background: var(--line); padding: 0.75rem 1rem; border-radius: 0.4rem;
        overflow-x: auto; font-size: 0.85rem; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
  .chart {{ width: 100%; margin-top: 0.75rem; border: 1px solid var(--line); border-radius: 0.4rem; }}
  h3 {{ font-size: 1rem; color: var(--muted); margin-top: 1.25rem; margin-bottom: 0.25rem; }}
</style>
<script>{chart_lib}</script>
</head>
<body>
<h1>{ticker} -- Category 4 Technical Analysis</h1>
<p class="meta">Yahoo Finance price data. Report generated {generated} NZT.</p>
{sections}
</body>
</html>
"""

SECTION_TITLES = [
    "Weekly Chart Read",
    "Daily Chart Read for Swing Entries",
    "Volume & Accumulation",
    "Relative Strength vs SPY",
    "Earnings Volatility Expectation",
    "Interactive Chart",
]

_chart_lib_cache = None


def _chart_library_js() -> str:
    global _chart_lib_cache
    if _chart_lib_cache is None:
        with open(CHART_LIB_PATH, encoding="utf-8") as f:
            # Defensive only -- the vendored minified build doesn't contain
            # this sequence, but a page must never let embedded text close
            # the surrounding <script> tag early.
            _chart_lib_cache = f.read().replace("</script", "<\\/script")
    return _chart_lib_cache


def _bar_series(dates: list, opens: list, highs: list, lows: list, closes: list) -> list:
    return [
        {"time": d.isoformat(), "open": round(o, 4), "high": round(h, 4), "low": round(l, 4), "close": round(c, 4)}
        for d, o, h, l, c in zip(dates, opens, highs, lows, closes) if d is not None
    ]


def _volume_series(dates: list, closes: list, volumes: list) -> list:
    out, prev = [], None
    for d, c, v in zip(dates, closes, volumes):
        if d is None:
            continue
        out.append({"time": d.isoformat(), "value": v, "color": "#26a69a" if (prev is None or c >= prev) else "#ef5350"})
        prev = c
    return out


def _line_series(dates: list, values: list) -> list:
    return [
        {"time": d.isoformat(), "value": round(v, 4)}
        for d, v in zip(dates, values) if d is not None and v is not None
    ]


def _chart_block(elem_id: str, height: int, bars: list, volumes: "list | None",
                  lines: list, support: "float | None", resistance: "float | None") -> str:
    # Rendered with TradingView's own Lightweight Charts (vendored, see
    # CHART_LIB_PATH) -- candles/volume are the real OHLCV already fetched
    # above; the SMA/EMA/support/resistance overlays are the exact values
    # computed in the sections above, not re-derived independently.
    lines_js = "".join(
        f'  chart.addLineSeries({{color:"{color}", lineWidth:1, title:"{sf._esc(label)}"}}).setData({json.dumps(data)});\n'
        for label, color, data in lines
    )
    vol_js = ""
    if volumes:
        vol_js = (
            '  chart.addHistogramSeries({priceFormat:{type:"volume"}, priceScaleId:"vol", '
            'scaleMargins:{top:0.8, bottom:0}}).setData(' + json.dumps(volumes) + ");\n"
        )
    price_lines_js = ""
    if support is not None:
        price_lines_js += f'  candleSeries.createPriceLine({{price:{support}, color:"#16a34a", lineWidth:1, lineStyle:2, title:"Support"}});\n'
    if resistance is not None:
        price_lines_js += f'  candleSeries.createPriceLine({{price:{resistance}, color:"#dc2626", lineWidth:1, lineStyle:2, title:"Resistance"}});\n'
    return f"""<div id="{elem_id}" class="chart"></div>
<script>
(function() {{
  var el = document.getElementById("{elem_id}");
  var style = getComputedStyle(document.documentElement);
  var chart = LightweightCharts.createChart(el, {{
    layout: {{ background: {{ color: "transparent" }}, textColor: (style.getPropertyValue("--fg") || "#1a1a1a").trim() }},
    grid: {{ vertLines: {{ color: "rgba(128,128,128,0.15)" }}, horzLines: {{ color: "rgba(128,128,128,0.15)" }} }},
    rightPriceScale: {{ borderVisible: false }},
    timeScale: {{ borderVisible: false }},
    width: el.clientWidth,
    height: {height},
  }});
  var candleSeries = chart.addCandlestickSeries({{upColor:"#26a69a", downColor:"#ef5350", borderVisible:false, wickUpColor:"#26a69a", wickDownColor:"#ef5350"}});
  candleSeries.setData({json.dumps(bars)});
{vol_js}{lines_js}{price_lines_js}  chart.timeScale().fitContent();
  window.addEventListener("resize", function() {{ chart.applyOptions({{width: el.clientWidth}}); }});
}})();
</script>
"""


def _text_html(text: str) -> str:
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    return "".join(f"<p>{sf._esc(l)}</p>" for l in lines) or "<p>N/A</p>"


def _html_page(report: dict) -> str:
    ticker = report["ticker"]
    currency = f" {report['currency']}" if report.get("currency") else ""
    w, d, v, rs, e = report["weekly"], report["daily"], report["volume"], report["rs"], report["earnings"]

    sec1 = sf._table(["Metric", "Value"], [
        ("Weekly Close", _fmt(w["close"], currency)),
        ("50-Week EMA", _fmt(w["ema50w"], currency)),
        ("200-Week EMA", _fmt(w["ema200w"], currency)),
        ("14-Period Weekly RSI", _fmt(w["rsi14w"], digits=1)),
        ("52-Week High", _fmt(w["high52w"], currency)),
        ("52-Week Low", _fmt(w["low52w"], currency)),
        ("Weekly Support", _fmt(w["support"], currency)),
        ("Weekly Resistance", _fmt(w["resistance"], currency)),
    ]) + _text_html(w["read"])

    sec2 = (
        f'<p><span class="badge">{sf._esc(d["setup"])}</span></p>'
        + sf._table(["Metric", "Value"], [
            ("Price", _fmt(d["price"], currency)),
            ("14-Period Daily RSI", _fmt(d["rsi14"], digits=1)),
            ("50-Day SMA", _fmt(d["sma50"], currency)),
            ("200-Day SMA", _fmt(d["sma200"], currency)),
            ("Daily Support", _fmt(d["support"], currency)),
            ("Daily Resistance", _fmt(d["resistance"], currency)),
        ])
        + _text_html(d["read"])
    )

    sec3 = sf._table(["Metric", "Value"], [
        ("OBV Trend", v["obv_trend"] or "N/A"),
        ("20-Day Volume MA", _fmt(v["vol_ma20"], digits=0)),
        ("Volume Signal", v["signal"] or "N/A"),
    ]) + _text_html(v["notes"])

    sec4 = sf._table(["Metric", "Value"], [
        ("3-Month RS vs SPY", _fmt(rs["rs_3m_diff"], " pp")),
        ("6-Month RS vs SPY", _fmt(rs["rs_6m_diff"], " pp")),
        ("Verdict", rs["verdict"] or "N/A"),
    ]) + _text_html(rs["notes"])

    sec5 = sf._table(["Metric", "Value"], [
        ("Average Earnings-Day Move", _fmt(e["avg_move_pct"], "%")),
    ]) + _text_html(e["basis"])

    generated = dt.datetime.now(NZ_TZ).strftime("%Y-%m-%d %H:%M")

    daily_raw, weekly_raw = report["daily_raw"], report["weekly_raw"]
    daily_chart = _chart_block(
        f"chart-daily-{ticker}", 400,
        _bar_series(daily_raw["dates"], daily_raw["opens"], daily_raw["highs"], daily_raw["lows"], daily_raw["closes"]),
        _volume_series(daily_raw["dates"], daily_raw["closes"], daily_raw["volumes"]),
        [
            ("SMA 50", "#2563eb", _line_series(daily_raw["dates"], _rolling_sma(daily_raw["closes"], 50))),
            ("SMA 200", "#dc2626", _line_series(daily_raw["dates"], _rolling_sma(daily_raw["closes"], 200))),
        ],
        d["support"], d["resistance"],
    )
    weekly_chart = _chart_block(
        f"chart-weekly-{ticker}", 320,
        _bar_series(weekly_raw["dates"], weekly_raw["opens"], weekly_raw["highs"], weekly_raw["lows"], weekly_raw["closes"]),
        None,
        [
            ("EMA 50w", "#2563eb", _line_series(weekly_raw["dates"], _rolling_ema(weekly_raw["closes"], 50))),
            ("EMA 200w", "#dc2626", _line_series(weekly_raw["dates"], _rolling_ema(weekly_raw["closes"], 200))),
        ],
        w["support"], w["resistance"],
    )
    sec6 = (
        "<h3>Daily (2y, swing-entry view)</h3>" + daily_chart
        + "<h3>Weekly (10y, trend view)</h3>" + weekly_chart
        + '<p class="cite">Candles and volume are the real OHLCV fetched above; SMA/EMA and '
        "support/resistance lines are the same values computed in sections 1-2. "
        "Rendered with TradingView Lightweight Charts (vendored, Apache-2.0) -- pan and "
        "zoom with the mouse/trackpad, hover for a crosshair readout.</p>"
    )

    sections_html = "".join(
        f"<section><h2>{i}. {sf._esc(title)}</h2>{body}</section>"
        for i, (title, body) in enumerate(
            zip(SECTION_TITLES, [sec1, sec2, sec3, sec4, sec5, sec6]), start=1
        )
    )
    return PAGE_TEMPLATE.format(
        ticker=sf._esc(ticker), generated=sf._esc(generated),
        sections=sections_html, chart_lib=_chart_library_js(),
    )


def write_html(report: dict) -> str:
    out_path = os.path.join(OUTPUT_DIR, f"{report['ticker']}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_html_page(report))
    return out_path


# ---------------------------------------------------------------------------
# Notion
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
        "title": [{"type": "text", "text": {"content": "股票技術分析 Technical Analysis"}}],
        "properties": {
            "Ticker": {"title": {}},
            "Date": {"date": {}},
            "Currency": {"select": {}},
            "Weekly Close": {"number": {"format": "number"}},
            "EMA50w": {"number": {"format": "number"}},
            "EMA200w": {"number": {"format": "number"}},
            "RSI14w": {"number": {"format": "number"}},
            "52W High": {"number": {"format": "number"}},
            "52W Low": {"number": {"format": "number"}},
            "Weekly Support": {"number": {"format": "number"}},
            "Weekly Resistance": {"number": {"format": "number"}},
            "Weekly Read": {"rich_text": {}},
            "Price": {"number": {"format": "number"}},
            "SMA50": {"number": {"format": "number"}},
            "SMA200": {"number": {"format": "number"}},
            "RSI14": {"number": {"format": "number"}},
            "Daily Support": {"number": {"format": "number"}},
            "Daily Resistance": {"number": {"format": "number"}},
            "Setup": {"select": {"options": [{"name": "Long"}, {"name": "Short"}, {"name": "Wait"}]}},
            "Daily Read": {"rich_text": {}},
            "OBV Trend": {"select": {"options": [{"name": "Rising"}, {"name": "Falling"}, {"name": "Flat"}]}},
            "20D Volume MA": {"number": {"format": "number"}},
            "Volume Signal": {"select": {"options": [{"name": "Confirming"}, {"name": "Diverging"}]}},
            "Volume Notes": {"rich_text": {}},
            "RS 3M pp": {"number": {"format": "number"}},
            "RS 6M pp": {"number": {"format": "number"}},
            "RS vs SPY": {"select": {"options": [{"name": "Outperforming"}, {"name": "Underperforming"}]}},
            "RS Notes": {"rich_text": {}},
            "Avg Earnings Move %": {"number": {"format": "percent"}},
            "Earnings Move Basis": {"rich_text": {}},
        },
    }, api_key)
    return result["id"]


def _page_properties(report: dict, when: dt.datetime) -> dict:
    w, d, v, rs, e = report["weekly"], report["daily"], report["volume"], report["rs"], report["earnings"]
    props = {
        "Ticker": {"title": _rich(report["ticker"])},
        "Date": {"date": {"start": when.date().isoformat()}},
        "Weekly Read": {"rich_text": _rich(w["read"])},
        "Daily Read": {"rich_text": _rich(d["read"])},
        "Setup": {"select": {"name": d["setup"]}},
        "Volume Notes": {"rich_text": _rich(v["notes"])},
        "RS Notes": {"rich_text": _rich(rs["notes"])},
        "Earnings Move Basis": {"rich_text": _rich(e["basis"])},
    }
    if report.get("currency"):
        props["Currency"] = {"select": {"name": report["currency"]}}
    numeric = {
        "Weekly Close": w["close"], "EMA50w": w["ema50w"], "EMA200w": w["ema200w"],
        "RSI14w": w["rsi14w"], "52W High": w["high52w"], "52W Low": w["low52w"],
        "Weekly Support": w["support"], "Weekly Resistance": w["resistance"],
        "Price": d["price"], "SMA50": d["sma50"], "SMA200": d["sma200"], "RSI14": d["rsi14"],
        "Daily Support": d["support"], "Daily Resistance": d["resistance"],
        "20D Volume MA": v["vol_ma20"],
        "RS 3M pp": rs["rs_3m_diff"], "RS 6M pp": rs["rs_6m_diff"],
    }
    for name, val in numeric.items():
        if val is not None:
            props[name] = {"number": round(val, 2)}
    if v.get("obv_trend"):
        props["OBV Trend"] = {"select": {"name": v["obv_trend"]}}
    if v.get("signal"):
        props["Volume Signal"] = {"select": {"name": v["signal"]}}
    if rs.get("verdict"):
        props["RS vs SPY"] = {"select": {"name": rs["verdict"]}}
    if e.get("avg_move_pct") is not None:
        props["Avg Earnings Move %"] = {"number": round(e["avg_move_pct"] / 100, 4)}
    return props


def run() -> int:
    cfg = load_config()
    notion_ready = bool(cfg and cfg.get("api_key") and cfg.get("technicals_database_id"))
    if not notion_ready:
        log.info("not configured (%s); Notion writes skipped, HTML still generated", CONFIG)
    try:
        spy_daily = _fetch_series(BENCHMARK_TICKER, "2y", "1d")
    except Exception as exc:
        log.error("SPY fetch failed, cannot compute relative strength: %s", exc)
        spy_daily = {"closes": []}
    now = dt.datetime.now(NZ_TZ)
    written = 0
    for ticker in WATCHLIST:
        try:
            report = build_report(ticker, spy_daily)
            if not report:
                continue
            html_path = write_html(report)
            log.info("%s: wrote HTML report to %s", ticker, html_path)
            if notion_ready:
                _notion("POST", "/pages", {
                    "parent": {"database_id": cfg["technicals_database_id"]},
                    "properties": _page_properties(report, now),
                }, cfg["api_key"])
                written += 1
                log.info("%s: wrote technical analysis report", ticker)
        except Exception as exc:
            log.error("%s: technicals report failed: %s", ticker, exc)
    log.info("wrote %d/%d tickers", written, len(WATCHLIST))
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
        cfg["technicals_database_id"] = db_id
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        print(f"database created and saved to config: {db_id}")
        return

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=None, help="comma-separated ticker list")
    args = parser.parse_args()
    if args.tickers:
        global WATCHLIST
        WATCHLIST = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    try:
        written = run()
        print(f"wrote {written}/{len(WATCHLIST)} reports")
    except Exception as exc:
        log.error("run failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
