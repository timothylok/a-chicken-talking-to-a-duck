"""Recall over voice/Slack history (記唔記得…): deterministic retrieval only.

The router's _recall() calls these, then has local Ollama phrase an answer from
the hits (reply-only). history.jsonl is small (hundreds of KB), so it is read
whole per query: no index to build or keep in sync with the nightly prune.

Chinese has no word boundaries, so matching is on character bigrams of the
query; English words match whole. Stdlib only.
"""

import datetime as dt
import json
import math
import re

PREFIXES = ("你記唔記得", "記唔記得", "记唔记得", "記不記得", "记不记得", "搵返", "揾返", "recall")

# Never worth recalling: restarts, failed/blocked turns, earlier recalls, and the
# long morning briefing and command list whose text would match almost any query.
SKIP_COMMANDS = {"RESTART_ASR", "MORNING_BRIEFING", "LIST_COMMANDS"}
SKIP_STATUSES = {"no_match", "busy", "recall", "cancelled", "chat_error", "needs_confirmation"}

_FILLER = sorted([
    "我哋", "上次", "之前", "最近", "嗰次", "幾時", "几时", "有冇", "有沒有", "講過", "讲过",
    "話過", "话过", "提過", "提过", "關於", "关于", "乜嘢", "邊個", "边个", "點樣", "点样",
    "做咗", "做過", "做过", "做乜", "做", "一次", "係咪", "系咪", "我", "你", "係", "系", "咩", "嘅", "呀", "啊", "嗎",
    "吗", "呢", "啦", "喇", "嘢", "咗",
], key=len, reverse=True)
_ZH = {"零": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _num(s: str) -> "int | None":
    if s.isdigit():
        return int(s)
    if not s or any(c not in _ZH for c in s):
        return None
    if "十" not in s:
        return _ZH[s] if len(s) == 1 else None
    head, _, tail = s.partition("十")
    return (_ZH[head] if head else 1) * 10 + (_ZH[tail] if tail else 0)


_N = r"(\d+|[零一二兩两三四五六七八九十]+)"


def _date_range(q: str, today: dt.date) -> "tuple[str, dt.date | None, dt.date | None]":
    """Strip one date expression from q; return (rest, start, end) inclusive."""
    week = today - dt.timedelta(days=today.weekday())
    fixed = [
        (r"今日|今天|今朝", today, today),
        (r"琴日|尋日|寻日|昨天|昨日", today - dt.timedelta(days=1), today - dt.timedelta(days=1)),
        (r"前日|前天", today - dt.timedelta(days=2), today - dt.timedelta(days=2)),
        (r"今個禮拜|今个礼拜|今個星期|呢個禮拜|this week", week, today),
        (r"上個禮拜|上个礼拜|上個星期|上星期|last week", week - dt.timedelta(days=7), week - dt.timedelta(days=1)),
        (r"今個月|今个月|呢個月|this month", today.replace(day=1), today),
    ]
    for pat, start, end in fixed:
        m = re.search(pat, q, re.I)
        if m:
            return q[:m.start()] + q[m.end():], start, end
    m = re.search(r"上個月|上个月|last month", q, re.I)
    if m:
        end = today.replace(day=1) - dt.timedelta(days=1)
        return q[:m.start()] + q[m.end():], end.replace(day=1), end
    m = re.search(_N + r"(?:日|天)前", q)
    if m and _num(m.group(1)) is not None:
        d = today - dt.timedelta(days=_num(m.group(1)))
        return q[:m.start()] + q[m.end():], d, d
    m = re.search(_N + r"月" + _N + r"(?:號|号|日)", q)
    if m and _num(m.group(1)) and _num(m.group(2)):
        try:
            d = dt.date(today.year, _num(m.group(1)), _num(m.group(2)))
        except ValueError:
            return q, None, None
        if d > today:
            d = d.replace(year=d.year - 1)
        return q[:m.start()] + q[m.end():], d, d
    return q, None, None


def parse_query(text: str, now: dt.datetime) -> "tuple[str, dt.date | None, dt.date | None]":
    """(search terms, start date, end date); terms may be '' for a date-only query."""
    q = re.sub(r"[^\w一-鿿 ]+", "", text.lower()).strip()
    for p in PREFIXES:
        if q.replace(" ", "").startswith(p):
            q = q.replace(" ", "")[len(p):] if p != "recall" else q[len("recall"):]
            break
    q, start, end = _date_range(q, now.date())
    for f in _FILLER:
        q = q.replace(f, " ")
    return re.sub(r"\s+", " ", q).strip(), start, end


def _features(s: str) -> "tuple[set, set]":
    s = s.lower()
    grams = set()
    for run in re.findall(r"[一-鿿]+", s):
        grams |= {run[i:i + 2] for i in range(len(run) - 1)} if len(run) > 1 else {run}
    return grams, set(re.findall(r"[a-z0-9]{2,}", s))


def load_entries(path: str) -> list:
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue  # corrupt or half-written line
    except OSError:
        pass
    return entries


def search(entries: list, terms: str, start=None, end=None, limit: int = 8) -> list:
    """Best-matching entries, oldest first. Date-only queries return the latest in range."""
    q_grams, q_words = _features(terms)
    need = max(1, math.ceil(0.5 * (len(q_grams) + len(q_words))))
    scored = []
    for e in entries:
        if e.get("command") in SKIP_COMMANDS or e.get("status") in SKIP_STATUSES:
            continue
        try:
            day = dt.datetime.fromisoformat(e["ts"]).date()
        except (KeyError, ValueError, TypeError):
            continue
        if (start and day < start) or (end and day > end):
            continue
        if not (q_grams or q_words):
            if start:
                scored.append((0, e["ts"], e))
            continue
        g, w = _features(f"{e.get('text') or ''} {e.get('reply') or ''}")
        score = len(q_grams & g) + len(q_words & w)
        if score >= need:
            scored.append((score, e["ts"], e))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return sorted((e for _, _, e in scored[:limit]), key=lambda e: e["ts"])


def context_lines(hits: list) -> str:
    """Dated, truncated lines for the LLM prompt."""
    out = []
    for e in hits:
        ts = e["ts"][:16].replace("T", " ")
        tag = e.get("command") or "傾偈"
        line = f"{ts} [{tag}] 主人講：{(e.get('text') or '')[:120]}"
        # A chat reply is the persona's improvised nonsense, not a fact; fed back
        # in, the model credited it to the owner ("你仲話要將啲餸斬到一萬嚿", 2026-10-12).
        if e.get("status") != "chat":
            line += f"；系統回覆：{(e.get('reply') or '')[:160]}"
        out.append(line)
    return "\n".join(out)
