"""AI bubble gauges -> dashboard/data/ai-bubble.json -> /dashboard/bubble.

Five gauges in the Exponential View style (economic strain, industry strain,
revenue momentum, valuation heat, funding quality), each the mean of a few
public, deterministic metrics, plus an equal-weighted 0-100 composite. Zero
LLM. Run once a day from the 11:10 Risk Dashboard job
(stock_risk_dashboard.poll_and_generate), so the same `vercel --prod`
ships it; the SEC companyfacts it needs for MSFT/GOOGL/AMZN/META/NVDA are
already in sf.urlopen_retry's per-process cache by then.

Sources: SEC XBRL (hyperscaler capex/revenue/OCF, Mag 7 net income and
diluted shares, BDC NAV/share), Yahoo chart (prices), BEA GDP via DBnomics,
SSGA's daily SPY holdings xlsx, Google News RSS. FRED's CSV endpoints were
tried first and time out from this machine (2026-10-20), so GDP comes from
DBnomics' BEA mirror and the credit gauge uses HYG vs IEF instead of the
ICE high-yield OAS, which has no other free source.

Run manually: python ops/ai_bubble.py [--dry]
"""

import argparse
import datetime as dt
import io
import json
import logging
import os
import re
import statistics
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_digest  # noqa: E402  (_google_news)
import stock_fundamentals as sf  # noqa: E402
import stock_technicals as st  # noqa: E402
import stock_valuation as sv  # noqa: E402

OUT_PATH = os.path.join(ROOT, "dashboard", "data", "ai-bubble.json")
VOLATILITY_PATH = os.path.join(ROOT, "dashboard", "data", "volatility.json")
LOG_PATH = os.path.join(ROOT, "asr", "logs", f"ai_bubble-{dt.date.today():%Y-%m-%d}.log")
NZ_TZ = ZoneInfo("Pacific/Auckland")
UA = {"User-Agent": "Mozilla/5.0"}

HYPERSCALERS = ["MSFT", "GOOGL", "AMZN", "META"]
MAG7 = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"]
BDCS = ["ARCC", "FSK", "OBDC", "BXSL"]  # the four largest listed BDCs, all private-credit lenders
# Fixed AI-exposed universe for the breadth read: chips, memory, networking,
# servers, power/cooling, model platforms. Edit here; all trade on US exchanges.
AI_UNIVERSE = ["NVDA", "AMD", "AVGO", "TSM", "MU", "ARM", "ANET", "SMCI", "DELL", "VRT",
               "ORCL", "MSFT", "GOOGL", "AMZN", "META", "PLTR", "CRWV", "SNOW"]
GDP_URL = "https://api.db.nomics.world/v22/series/BEA/NIPA-T10105/A191RC-Q?observations=1&format=json"
SPY_HOLDINGS_URL = ("https://www.ssga.com/library-content/products/fund-data/etfs/us/"
                    "holdings-daily-us-en-spy.xlsx")
HEADLINE_QUERY = '"AI bubble"'
RETURN_BARS = 63  # ~3 months of trading days, same window as KPI 9

# (lo, hi): a metric scores 0 at lo and 1 at hi, linear between, clamped.
# lo > hi means lower values are the risky ones. 1 = bubble-like.
THRESHOLDS = {
    # Hyperscaler TTM capex / US nominal GDP. Pre-AI (2019) the four ran
    # ~0.5%; US telecom capex peaked near 1.5% of GDP in 2000.
    "capex_gdp": (0.5, 2.0),
    # TTM capex YoY. Flat spending is no strain; the 2024-26 run was 40-80%.
    "capex_growth": (0.0, 60.0),
    # TTM capex / TTM revenue. ~10-12% was the pre-2023 norm for these four.
    "capex_revenue": (10.0, 35.0),
    # TTM capex / TTM operating cash flow: matches KPI 5's ends (50% healthy,
    # 100% means all operating cash goes back into capex).
    "capex_ocf": (40.0, 100.0),
    # NVDA TTM revenue YoY: the AI demand signal. 60%+ is spending still
    # converting into sales; 0% is the purchase cycle stalling.
    "nvda_revenue_growth": (60.0, 0.0),
    # Hyperscaler TTM revenue YoY: are the spenders' sales keeping up?
    "hyperscaler_revenue_growth": (20.0, 5.0),
    # Mag 7 aggregate trailing P/E. ~20 is the S&P long-run norm; dot-com
    # leaders traded at 50+.
    "mag7_pe": (20.0, 45.0),
    # Median AI-universe 3m return minus QQQ's (pp). Negative = a few
    # giants carry the index while the typical AI name lags.
    "ai_breadth": (5.0, -15.0),
    # RSP (equal-weight S&P) minus SPY 3m return (pp): market-wide breadth.
    "rsp_vs_spy": (2.0, -6.0),
    # HYG minus IEF 3m return (pp): junk lagging Treasuries = credit stress.
    "credit_hyg_ief": (2.0, -5.0),
    # Median BDC NAV/share YoY change (%): private-credit marks.
    "bdc_nav": (0.0, -10.0),
}
GREEN_MAX = 0.33
RED_MIN = 0.67

GAUGES = [
    ("economic", "Economic strain", ["capex_gdp", "capex_growth"]),
    ("industry", "Industry strain", ["capex_revenue", "capex_ocf"]),
    ("revenue", "Revenue momentum", ["nvda_revenue_growth", "hyperscaler_revenue_growth"]),
    ("valuation", "Valuation heat", ["mag7_pe", "ai_breadth", "rsp_vs_spy"]),
    ("funding", "Funding quality", ["credit_hyg_ief", "bdc_nav"]),
]

log = logging.getLogger("ai_bubble")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)


# ---------------------------------------------------------------------------
# Pure helpers (tested in tests/pure_logic.py)
# ---------------------------------------------------------------------------

def score(value: "float | None", lo: float, hi: float) -> "float | None":
    if value is None:
        return None
    return round(min(1.0, max(0.0, (value - lo) / (hi - lo))), 3)


def light(s: "float | None") -> "str | None":
    if s is None:
        return None
    return "Green" if s <= GREEN_MAX else "Red" if s >= RED_MIN else "Amber"


def _days(item: dict) -> int:
    return (dt.date.fromisoformat(item["end"]) - dt.date.fromisoformat(item["start"])).days


def discrete_quarters(facts_arr: list) -> list:
    """Standalone quarters from one tag's duration facts.

    Income-statement tags carry 3-month facts, but cash-flow tags (capex,
    OCF) are reported only year-to-date in 10-Qs: 3, 6, 9 months, then the
    10-K's 12. sv._quarterly_qtd_series only sees the first quarter of each
    year for those, so capex TTM came back None for META/NVDA/AAPL. Here a
    quarter is either a direct ~3-month fact or the difference between two
    YTD facts sharing a start date whose spans differ by ~3 months.
    """
    by_period = {}
    for item in facts_arr:
        if not item.get("start") or item.get("form") not in ("10-Q", "10-K", "10-Q/A", "10-K/A"):
            continue
        key = (item["start"], item["end"])
        if key not in by_period or item.get("filed", "") >= by_period[key].get("filed", ""):
            by_period[key] = item
    out = {}
    for item in by_period.values():
        if 80 <= _days(item) <= 100:
            out[item["end"]] = {"end": item["end"], "val": item["val"]}
    by_start = {}
    for item in by_period.values():
        if 80 <= _days(item) <= 380:
            by_start.setdefault(item["start"], []).append(item)
    for group in by_start.values():
        group.sort(key=lambda i: i["end"])
        for prev, cur in zip(group, group[1:]):
            if cur["end"] not in out and 80 <= _days(cur) - _days(prev) <= 100:
                out[cur["end"]] = {"end": cur["end"], "val": cur["val"] - prev["val"]}
    return sorted(out.values(), key=lambda i: i["end"])


def ttm_pair(facts: dict, tags: list) -> "tuple[float | None, float | None, str | None]":
    """(latest TTM, year-ago TTM, latest quarter end) for the freshest tag.

    Tag preference is by recency, as in sf._xbrl_annual_history: filers
    abandon tags (GOOGL's first capex tag stops in 2021, NVDA's first
    revenue tag in 2020).
    """
    best = []
    for tag in tags:
        node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
        quarters = discrete_quarters((node or {}).get("units", {}).get("USD", []))
        if quarters and (not best or quarters[-1]["end"] > best[-1]["end"]):
            best = quarters
    windows = sv._consecutive_ttm_windows(best)
    if not windows:
        return None, None, None
    return sv._latest_ttm(best), sv._year_ago_ttm(best), windows[-1][-1]["end"]


def hhi(weights_pct: list) -> float:
    """Herfindahl-Hirschman index (0-10,000) from percentage weights."""
    return round(sum(w * w for w in weights_pct), 1)


def three_month_return(closes: list) -> "float | None":
    if len(closes) <= RETURN_BARS:
        return None
    return (closes[-1] / closes[-1 - RETURN_BARS] - 1) * 100


# ---------------------------------------------------------------------------
# Fetchers -- each returns metric dicts; any failure logs and yields N/A
# ---------------------------------------------------------------------------

def _metric(value, display, as_of, source, note="") -> dict:
    return {"value": value, "display": display, "asOf": as_of, "source": source, "note": note}


def _facts(ticker: str) -> dict:
    return sf._company_facts(sf._cik_for_ticker(ticker))


def _pct(v: "float | None", digits: int = 1, signed: bool = False) -> str:
    if v is None:
        return "N/A"
    return f"{v:+.{digits}f}%" if signed else f"{v:.{digits}f}%"


def hyperscaler_metrics() -> dict:
    sums = {k: [0.0, 0.0] for k in ("capex", "revenue", "ocf")}
    ends = []
    for t in HYPERSCALERS:
        f = _facts(t)
        for key, tags in (("capex", sf.CAPEX_TAGS), ("revenue", sf.REVENUE_TAGS), ("ocf", sf.OCF_TAGS)):
            cur, prior, end = ttm_pair(f, tags)
            if cur is None or prior is None:
                raise ValueError(f"{t}: no TTM {key}")
            sums[key][0] += cur
            sums[key][1] += prior
            ends.append(end)
    capex, capex_prior = sums["capex"]
    revenue, revenue_prior = sums["revenue"]
    as_of = min(ends)  # the stalest filer bounds the aggregate
    src = "SEC XBRL 10-Q/10-K (MSFT, GOOGL, AMZN, META)"
    out = {
        "_capex": capex,
        "capex_growth": _metric(round((capex / capex_prior - 1) * 100, 1),
                                f"{_pct((capex / capex_prior - 1) * 100, signed=True)} "
                                f"(TTM {sf._fmt_usd(capex)})", as_of, src),
        "capex_revenue": _metric(round(capex / revenue * 100, 1), _pct(capex / revenue * 100), as_of, src),
        "capex_ocf": _metric(round(capex / sums["ocf"][0] * 100, 1), _pct(capex / sums["ocf"][0] * 100),
                             as_of, src),
        "hyperscaler_revenue_growth": _metric(
            round((revenue / revenue_prior - 1) * 100, 1),
            f"{_pct((revenue / revenue_prior - 1) * 100, signed=True)} (TTM {sf._fmt_usd(revenue)})",
            as_of, src, "Total revenue, not AI revenue: no filer discloses that separately."),
    }
    return out


def gdp_metric(capex: float) -> dict:
    payload = json.loads(sf.urlopen_retry(urllib.request.Request(GDP_URL, headers=UA), timeout=30))
    doc = payload["series"]["docs"][0]
    period, value = next((p, v) for p, v in zip(reversed(doc["period"]), reversed(doc["value"]))
                         if isinstance(v, (int, float)))
    gdp = value * 1e6  # BEA reports SAAR in millions of dollars
    ratio = capex / gdp * 100
    return _metric(round(ratio, 2), f"{ratio:.2f}% of GDP", period,
                   "SEC XBRL capex; BEA nominal GDP (SAAR) via DBnomics",
                   "Capex from the four hyperscalers only, against annualised US GDP.")


def nvda_metric() -> dict:
    cur, prior, end = ttm_pair(_facts("NVDA"), sf.REVENUE_TAGS)
    if cur is None or prior is None:
        raise ValueError("NVDA: no TTM revenue")
    g = (cur / prior - 1) * 100
    return _metric(round(g, 1), f"{_pct(g, signed=True)} (TTM {sf._fmt_usd(cur)})", end,
                   "SEC XBRL 10-Q/10-K (NVDA)", "Proxy for AI hardware demand.")


def mag7_pe_metric() -> dict:
    cap, income, ends = 0.0, 0.0, []
    for t in MAG7:
        f = _facts(t)
        ni, _, end = ttm_pair(f, sf.NET_INCOME_TAGS)
        _, shares = sv._xbrl_full_series(f, sf.DILUTED_SHARES_TAGS, unit="shares")
        quarterly = [s for s in shares if s.get("start") and 80 <= _days(s) <= 100]
        price = sf._current_price(t)
        if ni is None or not quarterly or price is None:
            raise ValueError(f"{t}: missing net income, shares or price")
        cap += price * quarterly[-1]["val"]
        income += ni
        ends.append(end)
    pe = cap / income
    return _metric(round(pe, 1), f"{pe:.1f}x", min(ends), "SEC XBRL net income + diluted shares; Yahoo price",
                   f"Σ market cap {sf._fmt_usd(cap)} ÷ Σ TTM net income {sf._fmt_usd(income)}.")


def _closes(ticker: str) -> list:
    return st._fetch_series(ticker, "6mo", "1d")["closes"]


def _relative(a: str, b: str) -> "tuple[float, str]":
    ra, rb = three_month_return(_closes(a)), three_month_return(_closes(b))
    if ra is None or rb is None:
        raise ValueError(f"not enough history for {a}/{b}")
    return ra - rb, f"{a} {ra:+.1f}% vs {b} {rb:+.1f}%"


def market_metrics() -> dict:
    today = dt.datetime.now(NZ_TZ).date().isoformat()
    out = {}
    for key, a, b, note in (
        ("rsp_vs_spy", "RSP", "SPY", "Equal-weight vs cap-weight S&P 500."),
        ("credit_hyg_ief", "HYG", "IEF", "High-yield bonds vs 7-10y Treasuries; a price proxy for credit spreads."),
    ):
        try:
            diff, detail = _relative(a, b)
            out[key] = _metric(round(diff, 1), f"{diff:+.1f} pp", today, "Yahoo chart, 3-month price return",
                               f"{detail}. {note}")
        except Exception as exc:
            log.warning("%s: %s", key, exc)
    try:
        qqq = three_month_return(_closes("QQQ"))
        rets = []
        for t in AI_UNIVERSE:
            try:
                r = three_month_return(_closes(t))
            except Exception as exc:
                log.warning("%s: price fetch failed: %s", t, exc)
                continue
            if r is not None:
                rets.append(r)
        if qqq is not None and len(rets) >= len(AI_UNIVERSE) // 2:
            med = statistics.median(rets)
            out["ai_breadth"] = _metric(
                round(med - qqq, 1), f"{med - qqq:+.1f} pp", today, "Yahoo chart, 3-month price return",
                f"Median of {len(rets)} AI names {med:+.1f}% vs QQQ {qqq:+.1f}%.")
    except Exception as exc:
        log.warning("ai_breadth: %s", exc)
    return out


def bdc_metric() -> dict:
    changes, ends, parts = [], [], []
    for t in BDCS:
        node = _facts(t)["facts"].get("us-gaap", {}).get("NetAssetValuePerShare", {})
        by_end = {}
        for item in node.get("units", {}).get("USD/shares", []):
            # Quarter-end marks from periodic reports only; prospectuses
            # (424B2) restate the same NAV on their own dates.
            if item.get("form") in ("10-Q", "10-K") and item["end"][5:] in ("03-31", "06-30", "09-30", "12-31"):
                by_end[item["end"]] = item["val"]
        ordered = sorted(by_end)
        if len(ordered) < 5:
            continue
        latest, year_ago = ordered[-1], ordered[-5]
        if (dt.date.fromisoformat(latest) - dt.date.fromisoformat(year_ago)).days > 380:
            continue
        chg = (by_end[latest] / by_end[year_ago] - 1) * 100
        changes.append(chg)
        ends.append(latest)
        parts.append(f"{t} {chg:+.1f}%")
    if len(changes) < 2:
        raise ValueError("fewer than 2 BDCs with a year of NAV history")
    med = statistics.median(changes)
    return _metric(round(med, 1), _pct(med, signed=True), max(ends), "SEC XBRL NetAssetValuePerShare",
                   "Median YoY NAV/share: " + ", ".join(parts) + ".")


# ---------------------------------------------------------------------------
# Context (shown, not scored)
# ---------------------------------------------------------------------------

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _xlsx_rows(data: bytes) -> list:
    """First sheet as lists of cell strings -- enough xlsx for SSGA's file."""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = ["".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t"))
              for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS)]
    rows = []
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(f"{{{_NS['m']}}}row"):
        cells = {}
        for c in row.findall("m:c", _NS):
            col = re.match(r"[A-Z]+", c.get("r")).group()
            v = c.findtext("m:v", default="", namespaces=_NS)
            cells[col] = shared[int(v)] if c.get("t") == "s" and v else v
        rows.append([cells.get(chr(65 + i), "") for i in range(12)])
    return rows


def spy_concentration() -> dict:
    rows = _xlsx_rows(sf.urlopen_retry(urllib.request.Request(SPY_HOLDINGS_URL, headers=UA), timeout=30))
    as_of = next((r[1] for r in rows[:4] if r[0].startswith("Holdings")), "")
    header = next(i for i, r in enumerate(rows) if "Ticker" in r and "Weight" in r)
    ti, wi = rows[header].index("Ticker"), rows[header].index("Weight")
    holdings = []
    for r in rows[header + 1:]:
        try:
            holdings.append((r[ti], float(r[wi])))
        except ValueError:
            break  # the disclaimer block follows the last holding
    weights = sorted((w for _, w in holdings), reverse=True)
    mag7 = sum(w for t, w in holdings if t in MAG7 or t == "GOOG")  # Alphabet lists both share classes
    return {"asOf": as_of.replace("As of ", ""), "mag7Weight": round(mag7, 1),
            "top10Weight": round(sum(weights[:10]), 1), "hhi": hhi(weights), "holdings": len(holdings),
            "source": "SSGA SPY daily holdings"}


def headline_heat(previous: dict) -> dict:
    today = dt.datetime.now(NZ_TZ).date().isoformat()
    count = len(ai_digest._google_news(HEADLINE_QUERY))
    history = [h for h in (previous.get("history") or []) if h["date"] != today]
    history = (history + [{"date": today, "count": count}])[-30:]
    last7 = [h["count"] for h in history[-7:]]
    return {"today": count, "avg7": round(sum(last7) / len(last7), 1), "history": history,
            "source": 'Google News RSS, "AI bubble", last 24 h'}


def vix_context() -> "dict | None":
    try:
        with open(VOLATILITY_PATH, encoding="utf-8") as f:
            v = json.load(f)
    except (OSError, ValueError):
        return None
    return {"level": v.get("vix"), "percentile10y": v.get("vixPercentile10y"), "regime": v.get("regime"),
            "asOf": v.get("generatedAt"), "source": "Yahoo ^VIX (volatility panel)"}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

METRIC_NAMES = {
    "capex_gdp": "Hyperscaler capex / GDP",
    "capex_growth": "Hyperscaler capex growth (TTM YoY)",
    "capex_revenue": "Capex / revenue",
    "capex_ocf": "Capex / operating cash flow",
    "nvda_revenue_growth": "NVIDIA revenue growth (TTM YoY)",
    "hyperscaler_revenue_growth": "Hyperscaler revenue growth (TTM YoY)",
    "mag7_pe": "Mag 7 aggregate P/E (trailing)",
    "ai_breadth": "AI breadth: median AI stock vs QQQ (3m)",
    "rsp_vs_spy": "Market breadth: RSP vs SPY (3m)",
    "credit_hyg_ief": "Credit: HYG vs IEF (3m)",
    "bdc_nav": "Private credit: BDC NAV/share (YoY)",
}


def _previous() -> dict:
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _collect() -> dict:
    metrics = {}
    capex = None
    try:
        hs = hyperscaler_metrics()
        capex = hs.pop("_capex")
        metrics.update(hs)
    except Exception as exc:
        log.warning("hyperscaler metrics failed: %s", exc)
    for key, fn in (("capex_gdp", lambda: gdp_metric(capex) if capex else None),
                    ("nvda_revenue_growth", nvda_metric),
                    ("mag7_pe", mag7_pe_metric),
                    ("bdc_nav", bdc_metric)):
        try:
            m = fn()
            if m:
                metrics[key] = m
        except Exception as exc:
            log.warning("%s failed: %s", key, exc)
    metrics.update(market_metrics())
    return metrics


def build() -> dict:
    previous = _previous()
    prev_metrics = {m["id"]: m for g in previous.get("gauges", []) for m in g["metrics"]}
    fresh = _collect()
    gauges, gauge_scores = [], []
    for gid, gname, keys in GAUGES:
        rows = []
        for key in keys:
            lo, hi = THRESHOLDS[key]
            m = fresh.get(key)
            stale = False
            if m is None and prev_metrics.get(key, {}).get("value") is not None:
                m, stale = prev_metrics[key], True  # keep yesterday's value, flagged
            if m is None:
                m = _metric(None, "N/A", None, "", "No data this run.")
            s = score(m["value"], lo, hi)
            rows.append({"id": key, "name": METRIC_NAMES[key], "value": m["value"], "display": m["display"],
                         "asOf": m["asOf"], "source": m["source"], "note": m["note"], "stale": stale,
                         "lo": lo, "hi": hi, "score": s, "light": light(s)})
        scored = [r["score"] for r in rows if r["score"] is not None]
        gs = round(sum(scored) / len(scored), 3) if scored else None
        if gs is not None:
            gauge_scores.append(gs)
        gauges.append({"id": gid, "name": gname, "score": gs, "light": light(gs), "metrics": rows})

    context = {}
    for key, fn in (("spy", spy_concentration),
                    ("headlines", lambda: headline_heat((previous.get("context") or {}).get("headlines") or {}))):
        try:
            context[key] = fn()
        except Exception as exc:
            log.warning("context %s failed: %s", key, exc)
            if (previous.get("context") or {}).get(key):
                context[key] = {**previous["context"][key], "stale": True}
    context["vix"] = vix_context()

    composite = round(sum(gauge_scores) / len(gauge_scores) * 100) if gauge_scores else None
    return {
        "generatedAt": dt.datetime.now(NZ_TZ).strftime("%Y-%m-%d %H:%M"),
        "composite": composite,
        "compositeLight": light(composite / 100) if composite is not None else None,
        "coverage": f"{len(gauge_scores)}/{len(GAUGES)} gauges",
        "freshMetrics": len(fresh),
        "gauges": gauges,
        "context": context,
    }


def write_bubble_snapshot(snapshot: dict) -> None:
    if snapshot["freshMetrics"] == 0:
        log.warning("no metric fetched; keeping the previous ai-bubble.json")
        return
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    log.info("AI bubble composite %s (%s), %d fresh metrics", snapshot["composite"], snapshot["coverage"],
             snapshot["freshMetrics"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry", action="store_true", help="print the JSON without writing it")
    args = parser.parse_args()
    snapshot = build()
    if not args.dry:
        write_bubble_snapshot(snapshot)
    print(json.dumps(snapshot, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
