"""Assertions over the dependency-free logic in router.py and pricewatch.py.

Run: python tests/pure_logic.py

Plain asserts, no pytest: the repo has no test framework and this needs nothing
beyond the standard library. Both modules import stdlib-only at module level,
so this runs on a bare interpreter with no venv, no torch and no CUDA -- which
is what lets CI run it at all. Nothing here touches the network, Ollama, the
skill CLIs or any state file.

Scope is deliberately narrow: the pure text/comparison logic that has actually
regressed before. Everything else in this project fails at runtime against live
data, and no unit test would have caught it.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "asr"))
sys.path.insert(0, os.path.join(ROOT, "ops"))

import pricewatch as pw  # noqa: E402
import router as r  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def check_true(label, value):
    if not value:
        failures.append(label)


# --- command table -----------------------------------------------------------
# Guards the pre-commit hook's contract: the homepage and CLAUDE.md are
# generated from COMMANDS, so a silent shrink means both drift.
check_true("COMMANDS should hold at least 27 entries", len(r.COMMANDS) >= 27)


# --- TTS pauses --------------------------------------------------------------
# Two rules, and the distinction is the whole point: _pause_english splits
# BETWEEN roman words and mangles multi-word place names (hence
# pause_english=False on news/briefing/quakes), while _pause_before_chinese
# only breaks a roman->Chinese boundary and is therefore safe everywhere.
check("roman->Chinese boundary gets a pause",
      r._pause_before_chinese("Facebook頁"), "Facebook，頁")
check("multi-word name stays intact at the boundary",
      r._pause_before_chinese("Lyttelton Port事發"),
      "Lyttelton Port，事發")
check("boundary rule must not split a place name",
      r._pause_before_chinese("Tokomaru Bay發生地震"),
      "Tokomaru Bay，發生地震")
check("between-words rule does split (why news opts out)",
      r._pause_english("Tokomaru Bay"), "Tokomaru，Bay")

# Maori macrons count as roman letters in BOTH rules -- leaving Latin Extended
# out silently exempted exactly the names that most need the pause.
check("macron-ending name gets a boundary pause",
      r._pause_before_chinese("Taupō今日落雨"),
      "Taupō，今日落雨")
check("macron-ending name splits between words too",
      r._pause_english("Taupō Moana"), "Taupō，Moana")

# No roman text -> untouched, and applying twice changes nothing.
plain = "奧克蘭而家15度，多雲"
check("pure Chinese is left alone", r._pause_before_chinese(plain), plain)
once = r._pause_before_chinese("Facebook頁")
check("boundary rule is idempotent", r._pause_before_chinese(once), once)


# --- translated-headline script guard ---------------------------------------
# gemma3:4b returns a correct translation in the wrong language when the prompt
# bans English and its Chinese for a word is weak. These are the real tokens
# observed in history.jsonl.
for label, bad in [
    ("Bengali (turbine)", "New Southland風力 টার্বাইন"),
    ("Korean (shrimp)", "攞 새우 俷旅遊體驗"),
    ("Arabic-Indic digits", "賺咗１٧٦ million"),
    ("Japanese kana", "ロボット開始工作"),
]:
    check_true(f"_FOREIGN_SCRIPT should catch {label}", r._FOREIGN_SCRIPT.search(bad))

# ...while everything legitimate passes: macrons, placeholders, fullwidth
# digits, CJK punctuation and leaked plain English (a separate, tolerated issue).
for label, ok in [
    ("Maori macrons", "Whangārei今日落大雨"),
    ("placeholders", "【1】喺【2】贏咗選舉"),
    ("fullwidth digits", "賺咗１７６ million"),
    ("CJK punctuation", "Dunne嘅「蟲」幫過佢 — 但係…"),
]:
    check_true(f"_FOREIGN_SCRIPT must not flag {label}", not r._FOREIGN_SCRIPT.search(ok))


# --- price-watch listing identity -------------------------------------------
# A Trade Me search's cheapest match is a different auction most days, so a
# fall between two observations is only a price cut when it's the same listing.
def alert_for(prev, price, identity):
    sent = []

    def notify(title, line, priority=3):
        sent.append(line)
        return True

    state = {"k": dict(prev)} if prev else {}
    pw._check_and_alert(notify, state, "2026-09-11", "k", "T", price, "u", identity=identity)
    return sent, state.get("k")


DAY = "2026-09-10"
same = {"date": DAY, "price": 859.0, "title": "T", "identity": "111"}

sent, _ = alert_for(same, 800.0, "111")
check_true("same listing + real fall -> price-cut wording",
           len(sent) == 1 and "平咗" in sent[0])

sent, st = alert_for(same, 840.0, "222")
check_true("different listing + cheaper -> 'cheaper listing' wording, not a cut",
           len(sent) == 1 and "有新平嘅盤" in sent[0])
check("a changed listing re-baselines onto the new identity",
      st["identity"], "222")

sent, _ = alert_for(same, 900.0, "222")
check_true("different listing but dearer -> silent", not sent)

# Legacy state written before identities were recorded: re-baseline once,
# quietly, rather than trusting a baseline whose item is unknown.
sent, st = alert_for({"date": DAY, "price": 859.0, "title": "T"}, 800.0, "111")
check_true("no recorded identity -> silent re-baseline", not sent)
check("...and the identity is recorded for next time", st["identity"], "111")

# PriceSpy ids and retailer URLs name one item, so they pass no identity and
# must behave exactly as before.
sent, _ = alert_for({"date": DAY, "price": 849.0, "title": "T"}, 800.0, None)
check_true("identity-free watch still reports a real drop",
           len(sent) == 1 and "平咗" in sent[0])

sent, _ = alert_for({"date": DAY, "price": 470.35, "title": "T"}, 470.01, None)
check_true("sub-threshold move stays silent", not sent)

# A same-day rerun (manual test, retry) must not overwrite tomorrow's baseline.
sent, st = alert_for({"date": "2026-09-11", "price": 840.0, "title": "T", "identity": "111"},
                     700.0, "222")
check_true("same-day rerun sends nothing", not sent)
check("same-day rerun leaves the baseline alone", st["price"], 840.0)


# --- report ------------------------------------------------------------------
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("pure-logic checks passed")
