# Polymarket spread filter

`ops/polywatch.py` only uses a market's odds when its order book is actively
quoted on both sides. Added 2026-10-06 after the first day-over-day run sent
four phantom alerts out of six.

## Why

Every Polymarket market has a **Yes order book**:

- **bid** — the best price someone will pay for Yes
- **ask** — the cheapest price someone will sell Yes at

The odds Polymarket lists (`outcomePrices`) are the **midpoint** of the two.
With a tight, two-sided book the midpoint is a fair crowd estimate. With one
side empty, or a wide gap, the midpoint is arithmetic — nobody is betting at
that price.

The case that prompted it (2026-10-06, META closed +1.9% at $741.90 the night before):

| Market | Bid / ask | Listed odds | Last real trade |
|---|---|---|---|
| META dips to $600 in October | none / 81¢ | 40.5% (halfway between 0 and 81) | 13% |
| META dips to $620 in October (busier) | 5¢ / 13¢ | 9% | 14% |

A dip to $600 can't be more likely than a dip to $620 — hitting $600 means
passing $620 first — so the thinner market was the wrong one. It had $101 of
lifetime volume and $7.88 of liquidity.

## Rule

A market is **tradeable** if it has both a bid and an ask, and
`ask − bid ≤ MAX_SPREAD` (0.10, i.e. 10 points). The difference is rounded to
4 dp so an exact 10-point spread isn't rejected by float error
(0.71 − 0.61 = 0.10000000000000003).

Why 10 points: the midpoint can then be off by about ±5, under every family's
alert threshold (5 / 10 / 15 / 10 points — see `MIN_MOVE_PTS`). The 5-point
`largest` family is the tightest fit, but its markets are by far the most liquid
(~$7.7M) and quote spreads of a cent or two.

## Where it lives

- **Skill** — `D:\ai\polymarket-skill\scripts\polymarket.py`, `normalize_market()`
  exposes `best_bid` / `best_ask` from Gamma's `bestBid` / `bestAsk`. Event
  listings already carry them, so the filter costs no extra API calls.
- **Watch** — `ops/polywatch.py`, `tradeable()`, checked in `_observations()`
  beside the existing zero-volume and missing-price skips.

## What happens to a skipped market

- It isn't observed, so the state file isn't updated and its **last good
  baseline is kept**. When the book tightens again, the move is measured
  against that reading, not a junk midpoint.
- A market untradeable for `STATE_KEEP_DAYS` (40) ages out of the state file.
- Rungs already hit (bid 0.999 / ask 1.00, spread 0.001) pass; they sit near
  100% and never move enough to alert.
- On rollout, the 41 baselines the 11:45 run had saved from untradeable books
  on 2026-10-06 were deleted from `asr/logs/polywatch_state.json`, so those
  markets start fresh at their next good reading instead of later firing a
  phantom move against a junk midpoint.

## Effect on 2026-10-06

Markets observed: 220 → 181.

| Family | Before → after |
|---|---|
| pricehit (monthly price ladder) | 97 → 80 |
| nzelect | 71 → 59 |
| mcap | 46 → 37 |
| largest | 6 → 5 |

| That day's alert | Bid / ask | Result |
|---|---|---|
| NVDA dips to $232 → 67% | 65¢ / 69¢ | kept |
| NVDA reaches $248 → 63% | 61¢ / 65¢ | kept |
| META dips to $600 → 40% | none / 81¢ | dropped |
| TSLA reaches $390 → 86% | 71¢ / 100¢ | dropped |
| TSLA reaches $420 → 48% | 39¢ / 57¢ | dropped |
| META market cap $1.75–2.00T (Oct) → 32% | 22¢ / 42¢ | dropped |

## Limits

- `bestBid` / `bestAsk` are Gamma's cached snapshot of the top of the book, so
  they can lag the live CLOB slightly.
- A thin market that genuinely moves is silent until it's quoted properly — the
  NZ election family loses 12 markets to this.

## Reading an alert by hand

Things that make a Polymarket move suspect, filtered or not:

1. Bid/ask gap wider than ~10 points.
2. No 24 h volume — the price moved because a quote was posted or pulled, not
   because anyone traded.
3. A ladder contradiction — a deeper dip priced more likely than a shallower one.
4. Odds that disagree with the share price's own move.

## Tests

- `tests/pure_logic.py` — six `tradeable()` cases from that day's real books,
  including the exact-10-point and already-hit edges.
- `D:\ai\polymarket-skill\tests\test_polymarket.py` — `best_bid` / `best_ask`
  normalisation, and `None` for an empty side.
