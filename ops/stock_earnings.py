"""Category 2: quarterly earnings reports, generated automatically the day
after each watchlist ticker's earnings release, saved to Notion.

Runs daily via the "VoiceOS Earnings Watch" scheduled task (runs as the user,
same isolation as stock_daily.py/notion_sync.py -- the Notion key never
enters the VoiceASR service environment), intended for an early hour (e.g.
1:00 AM NZT) since the trigger is accession-dedupe, not time-of-day-
sensitive. A separate task, "VoiceOS Earnings Watch Notify"
(earnings_watch_notify.py), runs later the same morning (e.g. 10:00 AM) and
pushes one ntfy alert if this run generated anything -- deliberately split
into two tasks so the user isn't paged in the middle of the night.

Trigger: reactive, not a forward calendar. Each run fetches SEC EDGAR
submissions for every watchlist ticker (minus EXCLUDE_NO_SEC_FILINGS) and
looks for the latest 8-K with Item 2.02 ("Results of Operations") in its
items list -- that filing date IS the earnings date, ground-truth and free.
Dedup is accession-based (has this specific 8-K been processed yet?), not a
"filed == yesterday" date check, so a late/missed run still catches up
correctly instead of silently skipping a filing. A 30-day staleness guard
stops a ticker's first-ever run from firing on a months-old filing.

Scope, and why: of the user's original 7 prompts (8-14), 3 need data with no
free source and are deliberately NOT attempted here --
  - Prompt 10 (conference call quotes) / 12 (analyst day takeaways): call
    transcripts and investor-day summaries are paywalled everywhere, not
    SEC-filed.
  - Prompt 9 (beat/miss vs. consensus): only EPS consensus is free (Nasdaq's
    calendar API); no free revenue consensus was found (Yahoo's estimate
    endpoint now requires an auth crumb, unlike the plain chart endpoint
    asr/router.py already uses keylessly). Revenue is reported as YoY only,
    explicitly labeled "consensus not available" rather than guessed.
  - Prompt 14 (buyback/dividend "from the call"): approximated from the
    press release's own capital-return language instead, honestly labeled
    as sourced from the filing, not the call.
Implemented: prompts 8, 9 (EPS-only), 11, 13, 14.

Source data: an 8-K's own primary document is just a cover form -- the
actual earnings press release is a separate EX-99.1 exhibit in the same
accession, found by parsing the accession's -index-headers.html SGML
DOCUMENT/TYPE/FILENAME blocks (confirmed live 2026-07-28; the -index.json
directory listing's "type" field is a generic MIME category, not the
exhibit type, and would silently fail to find it). Revenue and diluted EPS
(current + prior-year quarter) are regex-extracted directly from the press
release's own comparative income-statement table -- available same-day,
unlike XBRL company-facts which lags behind the 8-K until the 10-Q files
(often a day or more later). Prompt 13 (segment trends) is the one section
that needs multiple filings: the last 4 10-Qs' segment-discussion MD&A text.

Setup:
  1. Requires ops/notion.json already configured with api_key + database_id
     (see notion_sync.py's own --setup -- this reuses the same integration).
  2. python ops/stock_earnings.py --setup
     (creates the "股票季度業績 Quarterly Earnings" database under the same
     parent page as the existing Voice History database, adds
     earnings_database_id to the config)

Until earnings_database_id exists in the config, runs are silent no-ops.
"""

import datetime as dt
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_fundamentals as sf  # noqa: E402

# Deliberately NOT sf.OLLAMA_MODEL (lfm2.5 as of 2026-07-28): benchmarked
# lfm2.5 against qwen3:8b on these exact prompts using real fetched AAPL
# text (2026-07-28) -- lfm2.5 did well on 3 of 4 (summary, guidance,
# capital return) but returned completely empty content on the segment-
# trends prompt even at num_predict=1500 (same failure class Category 1
# saw before think:false was added for qwen3:8b -- lfm2.5's hidden
# reasoning doesn't reliably fit any fixed budget). This script isn't
# latency-sensitive (unattended daily batch, not live voice), so
# reliability wins over lfm2.5's speed edge; qwen3:8b succeeded cleanly on
# all 4, and its segment-percentage figures were verified correct against
# the real filing.
EARNINGS_MODEL = "qwen3:8b"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "asr", "logs", "stock_earnings.log")
CONFIG = os.path.join(ROOT, "ops", "notion.json")
WATCH_STATE = os.path.join(ROOT, "asr", "logs", "earnings_watch_state.json")
EARNINGS_HISTORY = os.path.join(ROOT, "asr", "logs", "earnings_history.json")
LAST_RUN = os.path.join(ROOT, "asr", "logs", "earnings_watch_last_run.json")
NOTION_VERSION = "2022-06-28"
NZ_TZ = ZoneInfo("Pacific/Auckland")
STALE_DAYS = 30  # don't fire on a filing older than this on a ticker's first-ever run

WATCHLIST = [
    t.strip().upper()
    for t in os.environ.get("STOCK_WATCHLIST", "AAPL,MSFT,NVDA,TSLA,GOOGL,AMZN,META,AON,SPCX").split(",")
    if t.strip()
]

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
# logging.basicConfig() is a no-op if the root logger already has a
# handler -- and importing stock_fundamentals above already configured one
# via its own basicConfig() call, which would otherwise silently redirect
# every log line here into stock_fundamentals.log instead (confirmed live
# 2026-07-28). Use an explicit handler on a dedicated logger instead, same
# pattern as asr/router.py's router.stock logger.
log = logging.getLogger("stock_earnings")
log.propagate = False
log.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)


# ---------------------------------------------------------------------------
# SEC EDGAR: earnings 8-K discovery + exhibit lookup
# ---------------------------------------------------------------------------

def _latest_earnings_8k(subs: dict) -> "dict | None":
    # _latest_filing() (stock_fundamentals.py) doesn't filter by item code --
    # a company files 8-Ks for many reasons (exec changes, shareholder
    # votes, ...); only Item 2.02 ("Results of Operations and Financial
    # Condition") means it's an earnings release.
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    items = recent.get("items", [])
    for i, f in enumerate(forms):
        if f == "8-K" and "2.02" in (items[i] if i < len(items) else "").split(","):
            return {
                "accn": recent["accessionNumber"][i],
                "doc": recent["primaryDocument"][i],
                "filed": recent["filingDate"][i],
            }
    return None


def _recent_filings(subs: dict, form: str, n: int) -> list:
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    out = []
    for i, f in enumerate(forms):
        if f == form:
            out.append({
                "accn": recent["accessionNumber"][i],
                "doc": recent["primaryDocument"][i],
                "filed": recent["filingDate"][i],
            })
            if len(out) == n:
                break
    return out


def _exhibit_doc(cik: str, accn: str, prefix: str = "EX-99") -> "str | None":
    # An 8-K's primaryDocument is just the cover form. The real content
    # (the earnings press release) is a separate exhibit, only discoverable
    # via the accession's SGML header block -- confirmed live against real
    # AAPL and MSFT 8-Ks (2026-07-28): -index.json's "type" field is a
    # generic MIME category ("text.gif"), NOT the exhibit type, and would
    # silently fail to find EX-99.1.
    accn_nodash = accn.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn_nodash}/{accn}-index-headers.html"
    try:
        raw = sf._sec_get(url).decode("utf-8", "replace")
    except Exception as exc:
        log.warning("exhibit index fetch failed for %s: %s", accn, exc)
        return None
    import html as html_lib
    raw = html_lib.unescape(raw)
    for block in re.findall(r"<DOCUMENT>(.*?)</DOCUMENT>", raw, re.S):
        m_type = re.search(r"<TYPE>([^\n<]+)", block)
        m_file = re.search(r"<FILENAME>([^\n<]+)", block)
        if m_type and m_file and m_type.group(1).strip().startswith(prefix):
            return m_file.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Deterministic figures from the press release's own comparative table
# ---------------------------------------------------------------------------

# Filers word their income-statement heading differently (AAPL: "Total net
# sales", MSFT: "Total revenue") and inconsistently repeat the $ sign per
# column -- both confirmed live on real AAPL/MSFT press releases
# (2026-07-28). Candidate patterns tried in order, same idiom as
# stock_fundamentals.py's REVENUE_TAGS.
_REVENUE_LINE_PATTERNS = [r"Total net sales", r"Total revenues?", r"Net revenues?"]
_NUM = r"\$?\s*([\d,]+(?:\.\d+)?)"


def _is_trailing_quarter_table(text: str, before_idx: int) -> bool:
    # Some filers (TSLA, confirmed live 2026-07-28) summarize revenue as a
    # 5-quarter trailing table ("Q2-2025 Q3-2025 Q4-2025 Q1-2026 Q2-2026
    # YoY") rather than a simple current-vs-prior-year pair -- a naive
    # "grab the first two numbers" match silently picked Q2-2025 and
    # Q3-2025 (two quarters that are neither "current" nor "same quarter
    # last year") and produced a nonsensical -19.9% "decline" when the
    # real YoY (printed on the same line) was +26%. Detected via the
    # distinctive "Q#-YYYY Q#-YYYY" header pattern; AAPL/MSFT's own
    # "Three/Six Months Ended" tables also have extra trailing numbers
    # (YTD columns) but never this header shape, so this check doesn't
    # false-positive on those.
    return bool(re.search(r"Q\d-20\d\d\s+Q\d-20\d\d", text[max(0, before_idx - 300):before_idx]))


def _column_order_current_first(text: str, before_idx: int) -> bool:
    # Column order (current-year-first vs. prior-year-first) varies by
    # filer -- confirmed live 2026-07-28: AAPL/MSFT both header their
    # comparative tables "... Ended <date>, YEAR_CUR ... YEAR_PRIOR"
    # (descending), but GOOGL headers its own "Quarter Ended <date>,
    # YEAR_PRIOR YEAR_CUR" (ascending) -- assuming current-first
    # unconditionally silently produced a nonsensical -19.5% "revenue
    # decline" for GOOGL when the real number was +24%. Detected from the
    # last two 4-digit years appearing before the match, which reflects
    # whichever comparative-period header actually governs this table.
    years = re.findall(r"\b(20\d\d)\b", text[max(0, before_idx - 600):before_idx])
    if len(years) < 2:
        return True  # unknown -- keep the (safe, more common) current-first default
    return years[-2] >= years[-1]


def _number_run(text: str, label_end: int) -> list:
    # Grabs every consecutive dollar/plain number right after a label, e.g.
    # "22,496 28,095 24,901 22,387 28,236" -- stops at the first "%" or word
    # (3+ letters), which is either the line's own YoY% or the next row's
    # label. Used only for trailing-quarter-style tables (see
    # _is_trailing_quarter_table), where the number of columns isn't fixed
    # at 2.
    window = text[label_end:label_end + 250]
    # \d+% as one unit, not just "%" alone -- otherwise the bare YoY number
    # itself (e.g. "26" in "26%") stays in the numeric_part and gets picked
    # up as a spurious extra column.
    stop = re.search(r"\d+%|[A-Za-z]{3,}", window)
    numeric_part = window[:stop.start()] if stop else window
    return [float(x.replace(",", "")) for x in re.findall(r"\$?\s*([\d,]+(?:\.\d+)?)", numeric_part)]


def _extract_income_figures(text: str) -> dict:
    out = {}
    rev_end = 0
    is_trailing = False
    for pat in _REVENUE_LINE_PATTERNS:
        lm = re.search(pat + r"\s*(?:\(\d\))?", text)  # the label alone, to know where its numbers start
        if not lm:
            continue
        if _is_trailing_quarter_table(text, lm.start()):
            # A 5-quarter trailing table (TSLA, confirmed live): column
            # count isn't 2, and it's oldest-to-newest left-to-right (from
            # the "Q2-2025 ... Q2-2026" header itself) -- first/last of the
            # full run are same-quarter-last-year/current, not group(1)/(2).
            nums = _number_run(text, lm.end())
            if len(nums) < 2:
                continue
            out["revenue_prior"], out["revenue_cur"] = nums[0], nums[-1]
            is_trailing = True
            rev_end = lm.end()
            break
        m = re.match(_NUM + r"\s+" + _NUM, text[lm.end():])
        if not m:
            continue
        a, b = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
        cur, prior = (a, b) if _column_order_current_first(text, lm.start()) else (b, a)
        out["revenue_cur"], out["revenue_prior"] = cur, prior
        rev_end = lm.end()
        break
    # Scoped to start right after the revenue line, not searched from
    # document start: some filers (GOOGL, confirmed live) have an earlier,
    # unrelated "Diluted ... NUM NUM" match inside a GAAP/non-GAAP
    # reconciliation table -- a document-wide search silently grabbed a
    # reconciliation adjustment column as if it were prior-year EPS. The
    # real comparative EPS line reliably follows revenue within the same
    # income-statement table (confirmed within ~750 chars for AAPL/MSFT);
    # if a filer's layout doesn't match this, EPS is honestly left unset
    # rather than risking another wrong-column mismatch.
    if is_trailing:
        # TSLA's EPS "Diluted" line is a 5-column run too, but confirmed
        # live it does NOT repeat the "Q#-YYYY" header nearby (it's deep in
        # a separate STATEMENT OF OPERATIONS block) -- so this is inferred
        # from the filer already having shown the trailing-quarter
        # convention on revenue, not re-detected independently. Gated
        # behind is_trailing so AAPL/MSFT (whose own EPS lines also have
        # >2 trailing numbers -- their quarter pair plus a YTD pair) are
        # never affected; they never set is_trailing in the first place.
        dm = re.search(r"Diluted", text[rev_end:rev_end + 30000])
        if dm:
            nums = _number_run(text, rev_end + dm.end())
            if len(nums) >= 2:
                out["eps_prior"], out["eps_cur"] = nums[0], nums[-1]
    else:
        # GOOGL's real comparative EPS line ("Diluted net income per common
        # share $2.31 $9.11") sits in an earlier compact "financial
        # highlights" table, BEFORE the detailed segment table this script
        # extracts revenue from -- confirmed live 2026-07-28, so it's
        # invisible to a search scoped to start after revenue. Tried first,
        # unscoped: this exact phrase is specific enough (distinct from
        # MSFT's "Diluted earnings per share"/"Diluted Earnings per Share"
        # decoy-table wording) not to risk repeating the earlier reconciliation-
        # table false-positive that motivated scoping in the first place.
        m = re.search(r"Diluted\s+net\s+income\s+per\s+common\s+share\s+" + _NUM + r"\s+" + _NUM, text)
        if not m:
            m = re.search(r"Diluted\s+" + _NUM + r"\s+" + _NUM, text[rev_end:rev_end + 30000])
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            cur, prior = (a, b) if _column_order_current_first(text, m.start()) else (b, a)
            out["eps_cur"], out["eps_prior"] = cur, prior
    if out.get("revenue_cur") is not None and out.get("revenue_prior"):
        out["revenue_yoy"] = (out["revenue_cur"] - out["revenue_prior"]) / abs(out["revenue_prior"]) * 100
    if out.get("eps_cur") is not None and out.get("eps_prior"):
        out["eps_yoy"] = (out["eps_cur"] - out["eps_prior"]) / abs(out["eps_prior"]) * 100
    return out


def _generate(prompt: str, num_predict: int = 400) -> str:
    payload = json.dumps({
        "model": EARNINGS_MODEL,
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
        # A blank reply is a failure, not a legitimate "nothing to say" --
        # sections that legitimately have nothing to disclose return a real
        # "N/A -- ..." string, never empty content. Treat as an error so
        # callers retry rather than silently writing a blank field.
        raise RuntimeError("model returned empty content")
    return reply


def _fmt_usd_m(val: "float | None") -> str:
    # Press-release tables are in millions already (unlike XBRL's raw
    # dollars), so this doesn't reuse stock_fundamentals.py's _fmt_usd.
    if val is None:
        return "N/A"
    return f"${val / 1000:.1f}B" if val >= 1000 else f"${val:.0f}M"


def _pct(val: "float | None") -> str:
    return f"{val:+.1f}%" if val is not None else "N/A"


# ---------------------------------------------------------------------------
# Nasdaq consensus EPS (the only free consensus-estimate source found;
# revenue consensus isn't exposed by any free API -- Yahoo's estimate
# endpoint requires an auth crumb, unlike its plain chart endpoint)
# ---------------------------------------------------------------------------

def _nasdaq_eps_forecast(ticker: str, around_date: dt.date) -> "dict | None":
    for delta in (0, -1, -2):
        d = around_date + dt.timedelta(days=delta)
        url = f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read())
        except Exception as exc:
            log.warning("nasdaq calendar fetch failed for %s: %s", d, exc)
            continue
        for row in (payload.get("data") or {}).get("rows") or []:
            if row.get("symbol") == ticker and row.get("epsForecast"):
                try:
                    eps = float(row["epsForecast"].replace("$", "").replace(",", ""))
                except ValueError:
                    continue
                return {"eps_forecast": eps, "num_estimates": row.get("noOfEsts")}
    return None


# ---------------------------------------------------------------------------
# State (trigger dedupe) and history (substantive content, e.g. prior
# guidance) -- kept as two separate files deliberately: losing the state
# file just risks a re-generated report; the history file carries content
# later reports depend on comparing against.
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


def _prior_filing(history: dict, ticker: str) -> "dict | None":
    filings = history.get(ticker, {}).get("filings", [])
    return filings[-1] if filings else None


def _append_history(history: dict, ticker: str, entry: dict) -> None:
    history.setdefault(ticker, {}).setdefault("filings", []).append(entry)


# ---------------------------------------------------------------------------
# Report sections (prompts 8, 9, 11, 13, 14)
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = (
    "Act as an equity analyst. Below is {ticker}'s most recent quarterly earnings "
    "press release (SEC EDGAR accession {accn}, filed {filed}). Revenue and EPS "
    "figures have already been computed separately -- do not restate them or their "
    "YoY math. Using ONLY the text given, describe: primary segment performance, "
    "any guidance mentioned for next quarter or full year, and one risk to watch. "
    "If guidance is not given in writing (e.g. deferred to the earnings call), say "
    "so plainly rather than guessing a number. Do not invent facts not present in "
    "the excerpt. Under 180 words.\n\n---\n{excerpt}\n---"
)

GUIDANCE_PROMPT = (
    "Below is an excerpt from {ticker}'s most recent quarterly earnings press "
    "release (accession {accn}, filed {filed}). Using ONLY this text, report any "
    "forward-looking guidance for next quarter or full year (revenue, EPS, margin, "
    "or other targets). If the release states guidance is only given verbally on "
    "the earnings call rather than in writing, say so plainly. If no guidance "
    "language appears at all, say so -- do not guess or infer a number.\n\n"
    "{prior_context}"
    "Under 150 words.\n\n---\n{excerpt}\n---"
)

SEGMENTS_PROMPT = (
    "Below are segment-discussion excerpts from {ticker}'s last {n} 10-Q filings, "
    "one per quarter, oldest first (each labeled with its filing date). Using ONLY "
    "this text, identify which segment has accelerating growth, which is "
    "decelerating, and which (if any) is shrinking. If a quarter's excerpt doesn't "
    "disclose segment figures, say so for that quarter rather than guessing. Cite "
    "SEC EDGAR as the source. Under 250 words.\n\n---\n{excerpt}\n---"
)

CAPITAL_RETURN_PROMPT = (
    "Below is an excerpt from {ticker}'s most recent quarterly earnings press "
    "release (accession {accn}, filed {filed}). This is sourced from the earnings "
    "release, not the conference call transcript -- do not claim otherwise. Using "
    "ONLY this text, summarize any share buyback program size/pace and dividend "
    "updates (declared amount, increase, special distributions). If no such "
    "information appears, say so. Under 150 words.\n\n---\n{excerpt}\n---"
)


def _section8_summary(ticker: str, ex99_text: str, figures: dict, accn: str, filed: str, url: str) -> str:
    rev_line = (f"{_fmt_usd_m(figures['revenue_cur'])} ({_pct(figures.get('revenue_yoy'))} YoY)"
                if figures.get("revenue_cur") is not None else "N/A (could not extract from this release)")
    eps_line = (f"${figures['eps_cur']:.2f} ({_pct(figures.get('eps_yoy'))} YoY)"
                if figures.get("eps_cur") is not None else "N/A (could not extract from this release)")
    header = f"Revenue: {rev_line}. Diluted EPS: {eps_line}."
    if not ex99_text:
        return f"{header}\n\nN/A -- could not fetch the earnings press release text.\n\n*Source: {url}*"
    prompt = SUMMARY_PROMPT.format(ticker=ticker, accn=accn, filed=filed, excerpt=ex99_text[:6000])
    try:
        reply = _generate(prompt)
    except Exception as exc:
        log.warning("%s: section8 summary failed: %s", ticker, exc)
        return f"{header}\n\nN/A -- narrative summary failed ({exc}).\n\n*Source: {url}*"
    return f"{header}\n\n{reply}\n\n*Source: 8-K EX-99.1, accession {accn}, filed {filed} ({url})*"


def _section9_beat_miss(figures: dict, consensus: "dict | None") -> str:
    lines = []
    if figures.get("eps_cur") is not None and consensus and consensus.get("eps_forecast") is not None:
        diff = figures["eps_cur"] - consensus["eps_forecast"]
        pct = diff / abs(consensus["eps_forecast"]) * 100 if consensus["eps_forecast"] else None
        verdict = "beat" if diff > 0.005 else "miss" if diff < -0.005 else "in-line"
        lines.append(
            f"EPS {verdict}: actual ${figures['eps_cur']:.2f} vs. consensus "
            f"${consensus['eps_forecast']:.2f} ({diff:+.2f}, {_pct(pct)}), based on "
            f"{consensus.get('num_estimates') or '?'} analyst estimates (source: Nasdaq)."
        )
    elif figures.get("eps_cur") is not None:
        lines.append(f"EPS: actual ${figures['eps_cur']:.2f}. Beat/miss: N/A -- consensus estimate not available.")
    else:
        lines.append("EPS beat/miss: N/A -- could not extract actual EPS from this release.")
    if figures.get("revenue_yoy") is not None:
        lines.append(
            f"Revenue: {_fmt_usd_m(figures['revenue_cur'])}, {_pct(figures['revenue_yoy'])} YoY "
            "(consensus revenue estimate not available from any free source)."
        )
    else:
        lines.append("Revenue YoY: N/A -- could not extract figures from this release.")
    return " ".join(lines)


def _section11_guidance(ticker: str, ex99_text: str, prior: "dict | None", accn: str, filed: str) -> str:
    if not ex99_text:
        return "N/A -- could not fetch the earnings press release text."
    excerpt = (sf._extract_section(ex99_text, r"Business\s+Outlook", r"Webcast|Conference\s+Call|Non-GAAP\s+Financial", max_len=2000)
               or sf._keyword_window(ex99_text, r"guidance|outlook|expects?\s+(revenue|EPS)", before=200, after=1800))
    if not excerpt:
        return "N/A -- no guidance or outlook language found in this release."
    prior_context = (
        f"For context, the prior quarter's guidance section said: \"{prior['guidance_text'][:400]}\"\n\n"
        if prior and prior.get("guidance_text") else ""
    )
    prompt = GUIDANCE_PROMPT.format(ticker=ticker, accn=accn, filed=filed, prior_context=prior_context, excerpt=excerpt)
    try:
        return _generate(prompt)
    except Exception as exc:
        log.warning("%s: section11 guidance failed: %s", ticker, exc)
        return f"N/A -- guidance extraction failed ({exc})."


def _segment_excerpt(text: str) -> "str | None":
    # 10-Q segment-discussion headings vary a lot by filer -- confirmed live
    # 2026-07-28: AAPL uses a single umbrella MD&A heading ("Segment
    # Operating Performance"), TSLA uses separate per-segment headings
    # ("Automotive & Services and Other Segment", "Energy Generation and
    # Storage Segment") with no matching umbrella heading at all. Try the
    # umbrella style first, then fall back to a generic "Segment ... revenue
    # increased/decreased" keyword match that catches the per-segment style.
    return (
        sf._extract_section(text, r"Segment\s+Operating\s+Performance", r"Gross\s+Margin|Operating\s+Expenses", max_len=2500)
        or sf._keyword_window(text, r"\bSegment\b.{0,60}?(?:revenue|net sales)\s+(?:increased|decreased)", before=60, after=2000)
    )


def _section13_segments(ticker: str, cik: str, subs: dict) -> str:
    tenqs = _recent_filings(subs, "10-Q", 4)
    if not tenqs:
        return "N/A -- no 10-Q filings found for this ticker."
    parts = []
    for f in reversed(tenqs):  # oldest first
        try:
            text = sf._fetch_text(sf._filing_url(cik, f["accn"], f["doc"]))
        except Exception as exc:
            log.warning("%s: 10-Q fetch failed (%s): %s", ticker, f["accn"], exc)
            continue
        excerpt = _segment_excerpt(text)
        if excerpt:
            parts.append(f"[Filed {f['filed']}]\n{excerpt}")
    if not parts:
        return "N/A -- could not locate segment-discussion text in the last 4 10-Qs."
    prompt = SEGMENTS_PROMPT.format(ticker=ticker, n=len(parts), excerpt="\n\n".join(parts)[:7000])
    try:
        return _generate(prompt)
    except Exception as exc:
        log.warning("%s: section13 segments failed: %s", ticker, exc)
        return f"N/A -- segment analysis failed ({exc})."


def _section14_capital_return(ticker: str, ex99_text: str, accn: str, filed: str) -> str:
    if not ex99_text:
        return "N/A -- could not fetch the earnings press release text."
    excerpt = sf._keyword_window(ex99_text, r"repurchas\w*|dividend\w*", before=300, after=1500)
    if not excerpt:
        return "N/A -- no buyback or dividend language found in this release."
    prompt = CAPITAL_RETURN_PROMPT.format(ticker=ticker, accn=accn, filed=filed, excerpt=excerpt)
    try:
        return _generate(prompt)
    except Exception as exc:
        log.warning("%s: section14 capital return failed: %s", ticker, exc)
        return f"N/A -- capital return summary failed ({exc})."


# ---------------------------------------------------------------------------
# Report orchestration
# ---------------------------------------------------------------------------

def build_earnings_report(ticker: str, cik: str, subs: dict, trigger: dict, history: dict) -> "dict | None":
    ex99_doc = _exhibit_doc(cik, trigger["accn"])
    ex99_url = sf._filing_url(cik, trigger["accn"], ex99_doc) if ex99_doc else None
    ex99_text = sf._fetch_text(ex99_url) if ex99_url else ""
    if not ex99_text:
        # A total fetch failure (network error, or no EX-99.1 found at all)
        # should be retried next run, not silently marked done -- unlike a
        # section legitimately having nothing to disclose (which still
        # produces real N/A text further down and IS worth marking done).
        raise RuntimeError(f"could not find/fetch EX-99.1 for accession {trigger['accn']}")

    figures = _extract_income_figures(ex99_text) if ex99_text else {}
    consensus = _nasdaq_eps_forecast(ticker, dt.date.fromisoformat(trigger["filed"]))
    prior = _prior_filing(history, ticker)

    sec8 = _section8_summary(ticker, ex99_text, figures, trigger["accn"], trigger["filed"], ex99_url or "")
    sec9 = _section9_beat_miss(figures, consensus)
    sec11 = _section11_guidance(ticker, ex99_text, prior, trigger["accn"], trigger["filed"])
    sec13 = _section13_segments(ticker, cik, subs)
    sec14 = _section14_capital_return(ticker, ex99_text, trigger["accn"], trigger["filed"])

    guidance_excerpt = None
    if sec11 and not sec11.startswith("N/A"):
        guidance_excerpt = sec11
    _append_history(history, ticker, {
        "accn": trigger["accn"], "filed": trigger["filed"],
        "guidance_text": guidance_excerpt,
        "eps_actual": figures.get("eps_cur"), "eps_consensus": consensus.get("eps_forecast") if consensus else None,
    })

    return {
        "ticker": ticker, "accn": trigger["accn"], "filed": trigger["filed"],
        "eps_actual": figures.get("eps_cur"), "eps_consensus": consensus.get("eps_forecast") if consensus else None,
        "revenue_yoy": figures.get("revenue_yoy"),
        "results_summary": sec8, "beat_miss": sec9, "guidance": sec11,
        "segments": sec13, "capital_return": sec14,
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
    # Notion caps a text block at 2000 chars.
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
        "title": [{"type": "text", "text": {"content": "股票季度業績 Quarterly Earnings"}}],
        "properties": {
            "Ticker": {"title": {}},
            "Filed Date": {"date": {}},
            "Accession": {"rich_text": {}},
            "Actual EPS": {"number": {"format": "number"}},
            "Consensus EPS": {"number": {"format": "number"}},
            "Revenue YoY %": {"number": {"format": "percent"}},
            "Results Summary": {"rich_text": {}},
            "Beat/Miss": {"rich_text": {}},
            "Guidance Trajectory": {"rich_text": {}},
            "Segment Trends": {"rich_text": {}},
            "Capital Return": {"rich_text": {}},
        },
    }, api_key)
    return result["id"]


def _page_properties(report: dict, when: dt.datetime) -> dict:
    props = {
        "Ticker": {"title": _rich(report["ticker"])},
        "Filed Date": {"date": {"start": report["filed"]}},
        "Accession": {"rich_text": _rich(report["accn"])},
        "Results Summary": {"rich_text": _rich(report["results_summary"])},
        "Beat/Miss": {"rich_text": _rich(report["beat_miss"])},
        "Guidance Trajectory": {"rich_text": _rich(report["guidance"])},
        "Segment Trends": {"rich_text": _rich(report["segments"])},
        "Capital Return": {"rich_text": _rich(report["capital_return"])},
    }
    if report.get("eps_actual") is not None:
        props["Actual EPS"] = {"number": report["eps_actual"]}
    if report.get("eps_consensus") is not None:
        props["Consensus EPS"] = {"number": report["eps_consensus"]}
    if report.get("revenue_yoy") is not None:
        props["Revenue YoY %"] = {"number": round(report["revenue_yoy"] / 100, 4)}
    return props


# ---------------------------------------------------------------------------
# Trigger poll
# ---------------------------------------------------------------------------

def poll_and_generate() -> int:
    cfg = load_config()
    if not cfg or not cfg.get("api_key") or not cfg.get("earnings_database_id"):
        log.info("not configured (%s); skipping", CONFIG)
        return 0

    state = _load_json(WATCH_STATE, {})
    history = _load_json(EARNINGS_HISTORY, {})
    today = dt.datetime.now(NZ_TZ).date()
    written = 0
    generated = []

    for ticker in WATCHLIST:
        if ticker in sf.EXCLUDE_NO_SEC_FILINGS:
            continue
        try:
            cik = sf._cik_for_ticker(ticker)
            if not cik:
                log.warning("%s: no CIK found", ticker)
                continue
            subs = sf._submissions(cik)
            hit = _latest_earnings_8k(subs)
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

            report = build_earnings_report(ticker, cik, subs, hit, history)
            _notion("POST", "/pages", {
                "parent": {"database_id": cfg["earnings_database_id"]},
                "properties": _page_properties(report, dt.datetime.now(NZ_TZ)),
            }, cfg["api_key"])
            state.setdefault(ticker, {})["last_processed_accn"] = hit["accn"]
            state[ticker]["last_processed_filed"] = hit["filed"]
            written += 1
            generated.append(ticker)
            log.info("%s: generated earnings report for accession %s (filed %s)", ticker, hit["accn"], hit["filed"])
            if sf.regenerate(ticker):
                log.info("%s: regenerated Category 1 report after earnings update", ticker)
        except Exception as exc:
            log.error("%s: earnings report failed: %s", ticker, exc)

    _save_json(WATCH_STATE, state)
    _save_json(EARNINGS_HISTORY, history)
    # Always written, even when empty -- lets earnings_watch_notify.py (a
    # separate scheduled task, deliberately hours later so the user isn't
    # pinged in the middle of the night) tell "ran with nothing new" apart
    # from "didn't run at all".
    _save_json(LAST_RUN, {"date": today.isoformat(), "tickers": generated, "notified": False})
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
        cfg["earnings_database_id"] = db_id
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
