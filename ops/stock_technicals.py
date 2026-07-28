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
    closes, highs, lows, volumes, dates = [], [], [], [], []
    for i, c in enumerate(raw_closes):
        if c is None:
            continue
        closes.append(c)
        h = quote.get("high", [None] * len(raw_closes))[i]
        l = quote.get("low", [None] * len(raw_closes))[i]
        v = quote.get("volume", [None] * len(raw_closes))[i]
        highs.append(h if h is not None else c)
        lows.append(l if l is not None else c)
        volumes.append(v if v is not None else 0)
        dates.append(dt.date.fromtimestamp(timestamps[i]) if i < len(timestamps) else None)
    return {
        "closes": closes, "highs": highs, "lows": lows, "volumes": volumes, "dates": dates,
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
    }


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
    if not cfg or not cfg.get("api_key") or not cfg.get("technicals_database_id"):
        log.info("not configured (%s); skipping", CONFIG)
        return 0
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
