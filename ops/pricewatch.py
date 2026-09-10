"""Push a ntfy alert when a watched PriceSpy product's price drops from the
last day it was checked.

Runs daily via the "VoiceOS Price Watch" scheduled task as the logged-in
user (same ntfy isolation as milk_watch.py -- the topic never enters the
VoiceASR service environment). Unlike milk_watch.py, this does NOT depend
on any upstream "price history" dataset for the day-over-day baseline --
each run records its own observed price to a local state file, and the
next day's run compares against that. This sidesteps the exact class of
bug found in milk_watch.py 2026-07-29 (an upstream history dataset that
silently stalled for days, causing the same stale "drop" to be re-reported
every morning) -- our own daily observation is always the source of truth.

Products: PRICEWATCH_PRODUCTS env var, comma-separated PriceSpy product ids
(default: 13101596 "Nintendo Switch 2" +
5848136 "Kingston Fury Beast Black DDR4 3200MHz 2x16GB" +
5241360 "G.Skill Ripjaws V Black DDR4 3600MHz 2x16GB" +
15436948 "Pokemon Pokopia (Switch 2)" +
14576211 "The Legend of Zelda: Tears of the Kingdom (Switch 2)",
https://pricespy.co.nz/product.php?p=13101596 /
https://pricespy.co.nz/product.php?p=5848136 /
https://pricespy.co.nz/product.php?p=5241360 /
https://pricespy.co.nz/product.php?p=15436948 /
https://pricespy.co.nz/product.php?p=14576211).

Uses the thecolab-ai nz-pricewatch skill's CLI directly (no login, no
account, public PriceSpy product pages only).

Also watches Trade Me marketplace search results: PRICEWATCH_TRADEME_SEARCHES
env var, comma-separated "term|min_price" pairs (default:
"Nintendo Switch 2|400"). Unlike PriceSpy's stable product pages, individual
Trade Me listings expire in days-to-weeks, so a fixed listing id isn't a
durable thing to watch -- each run re-searches the term fresh and tracks the
cheapest current buy-now listing's price, alerting when that cheapest price
drops day over day. A candidate listing must clear min_price AND contain
every word of the search term in its title: the floor alone kept picking a
different product entirely (2026-09-08, a Pokemon Eevee Switch 1 console
answering a "Nintendo Switch 2" watch). min_price is enforced client-side,
not passed as a server-side filter: Trade Me's search API silently ignores
price_min/price_max on this endpoint (confirmed live 2026-08-10), and without
a floor the "cheapest match" for a console search is reliably a stray
accessory (joystick, case, charger) miscategorized under the same category
path, not the console. Uses the thecolab-ai trademe-nz skill's CLI (no login,
public search only).

Also watches plain retailer product URLs: PRICEWATCH_URLS env var,
comma-separated (default: the same Aberlour 12YO at The Bottle-O Glenfield and
Super Liquor, so a drop at either shop pages). These are shops PriceSpy does not
index -- it covers electronics, not spirits -- so there is no skill CLI to lean
on and the page is read directly. Only schema.org product data is parsed, never
visible page text: both encodings of it (JSON-LD for The Bottle-O, microdata
<meta itemprop> tags for Super Liquor) are machine-readable contracts a shop
publishes for Google Shopping, so they carry name, price, currency *and*
availability, and they move far less often than the surrounding markup. A page
that stops publishing either is reported as a failure rather than guessed at.

A drop must be at least PRICEWATCH_MIN_DROP_PCT (default 1%) to alert.

Run manually: python ops/pricewatch.py
"""

import datetime as dt
import html
import json
import logging
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "asr", "logs", "pricewatch.log")
STATE_PATH = os.path.join(ROOT, "asr", "logs", "pricewatch_state.json")
CLI = "D:/ai/thecolab-skills/skills/nz-pricewatch/scripts/cli.py"
TRADEME_CLI = "D:/ai/thecolab-skills/skills/trademe-nz/scripts/cli.py"
PRODUCTS = [p.strip() for p in os.environ.get("PRICEWATCH_PRODUCTS", "13101596,5848136,5241360,15436948,14576211").split(",") if p.strip()]
NZ_TZ = ZoneInfo("Pacific/Auckland")


def _parse_trademe_searches(raw: str) -> list[tuple[str, float]]:
    searches = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        term, _, min_price = entry.partition("|")
        searches.append((term.strip(), float(min_price) if min_price.strip() else 0.0))
    return searches


TRADEME_SEARCHES = _parse_trademe_searches(os.environ.get("PRICEWATCH_TRADEME_SEARCHES", "Nintendo Switch 2|400"))
DEFAULT_URLS = (
    "https://glenfield.shop.thebottleo.co.nz/lines/aberlour-12-year-old-double-cask-matured-700ml,"
    "https://www.superliquor.co.nz/aberlour-12yo-double-cask-matured-single-malt-700ml"
)
URLS = [u.strip() for u in os.environ.get("PRICEWATCH_URLS", DEFAULT_URLS).split(",") if u.strip()]
# Minimum day-over-day fall, in percent, before an alert is worth sending.
MIN_DROP_PCT = float(os.environ.get("PRICEWATCH_MIN_DROP_PCT", "1"))
sys.path.insert(0, os.path.join(ROOT, "ops"))

logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
log = logging.getLogger("pricewatch")


def _run_skill(cli: str, *args: str, timeout: int = 30) -> dict:
    # PYTHONIOENCODING forces the child's stdout to UTF-8 -- under a
    # scheduled task it can default to cp1252, same trap asr/router.py's
    # own _run_skill() guards against for the voice-command skill CLIs.
    out = subprocess.run(
        [sys.executable, cli, *args],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    # Surface what the CLI actually said. Left unchecked, a non-zero exit
    # reached json.loads("") and logged 15 lines of JSONDecodeError traceback
    # that never mentioned the real cause (2026-09-06, a delisted product id).
    stderr = (out.stderr or "").strip()[-400:]
    if out.returncode != 0:
        raise RuntimeError(f"{os.path.basename(cli)} exited {out.returncode}: {stderr}")
    try:
        return json.loads(out.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{os.path.basename(cli)} gave unparseable output ({exc}); stderr: {stderr}") from exc


def _lowest_available_price(data: dict) -> float | None:
    # PriceSpy's own current_lowest_price counts out-of-stock offers, so a
    # dead listing that keeps flickering on and off the page reads as a
    # repeated price drop (Kingston Fury 2026-09-06: a $299 OutOfStock offer
    # vanished for a day and came back, alerting a $470.35 -> $299 "drop"
    # that never existed). Only OutOfStock is excluded, not everything that
    # isn't InStock -- some merchants report Unknown and are still buyable.
    prices = [o["price"] for o in data.get("offers", [])
              if o.get("price") is not None and o.get("stock_status") != "OutOfStock"]
    return min(prices) if prices else None


def _schema_nodes(doc):
    # schema.org data nests freely -- a bare object, a list, or wrapped in
    # "@graph" -- so walk the whole document rather than guessing a shape.
    if isinstance(doc, dict):
        yield doc
        for value in doc.values():
            yield from _schema_nodes(value)
    elif isinstance(doc, list):
        for value in doc:
            yield from _schema_nodes(value)


def _first_meta(page: str, prop: str) -> str | None:
    # First match wins: Super Liquor emits itemprop="name" twice, the product
    # first and the brand ("Aberlour") second, so taking the last would name
    # every watch after its distillery.
    m = re.search(
        rf'<meta[^>]+itemprop=["\']{prop}["\'][^>]+content=["\']([^"\']*)["\']',
        page, re.I)
    return html.unescape(m.group(1)).strip() if m else None


def _scrape_schema_offer(url: str, timeout: int = 30) -> tuple[str, float, bool]:
    """(title, price, in_stock) from a product page's schema.org data."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        page = resp.read().decode("utf-8", "replace")

    title = price = availability = None
    for block in re.findall(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            doc = json.loads(block.strip())
        except ValueError:
            continue  # one malformed block must not sink the others
        for node in _schema_nodes(doc):
            if not isinstance(node, dict) or node.get("@type") != "Product":
                continue
            title = title or node.get("name")
            for offer in _schema_nodes(node.get("offers")):
                if isinstance(offer, dict) and offer.get("price") is not None:
                    price = float(offer["price"])
                    availability = str(offer.get("availability", ""))
                    break

    if price is None:  # microdata fallback (Super Liquor publishes no JSON-LD)
        raw = _first_meta(page, "price")
        if raw is not None:
            price = float(raw)
            title = title or _first_meta(page, "name")
            availability = _first_meta(page, "availability") or ""

    if price is None:
        raise RuntimeError("no schema.org price on page (markup changed?)")
    # Availability arrives as a URL, http or https, sometimes bare. Treat only
    # an explicit OutOfStock as unavailable, matching _lowest_available_price:
    # some shops publish Unknown or nothing at all and are still buyable.
    in_stock = availability.rstrip("/").rsplit("/", 1)[-1].lower() != "outofstock"
    return html.unescape(title or url), price, in_stock


def _matches_query(title: str, query: str) -> bool:
    # The min_price floor alone was letting the wrong product through: on
    # 2026-09-08 the cheapest "Nintendo Switch 2" listing over $400 was a
    # Pokemon Let's Go Eevee *Switch 1* console at $623.99, and the day
    # before it was a Switch Lite -- two unrelated listings compared to each
    # other as if the difference were a price move. Require every word of the
    # search term in the listing title. The floor still does the accessory
    # filtering, since cases and screen protectors name the console too.
    words = set(re.findall(r"\w+", title.lower()))
    return all(word in words for word in re.findall(r"\w+", query.lower()))


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    # Write-then-rename. A half-written file is invalid JSON, which
    # _load_state's ValueError guard swallows into an empty dict -- every
    # baseline silently resets to "first run" and a day of alerts is lost
    # with nothing but INFO lines to show for it.
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _check_and_alert(notify, state: dict, today: str, key: str, title: str, price: float, url: str,
                     identity: "str | None" = None) -> None:
    prev = state.get(key)
    prev_price = prev.get("price") if prev else None
    prev_date = prev.get("date") if prev else None
    prev_identity = prev.get("identity") if prev else None

    # A PriceSpy product id or a retailer URL names one item, so yesterday's
    # price is the same item's price and a fall really is a price drop. A Trade
    # Me *search* doesn't: its cheapest match is usually a different auction
    # each day, so the two prices belong to unrelated items. Comparing them
    # reported "平咗" for a fall nobody's price took -- $859.00 -> $840.00 on
    # 2026-09-10 was "Nintendo Switch 2 Console + AfterPay" against a different
    # listing entirely. Callers that can't guarantee identity pass one, and a
    # changed identity is still worth knowing about (a cheaper listing appeared)
    # but is reported as that, not as a price cut. A watch with no recorded
    # identity yet re-baselines once, silently, rather than trusting a baseline
    # whose item is unknown.
    same_item = identity is None or identity == prev_identity

    if prev_price is not None and prev_date != today and price <= prev_price * (1 - MIN_DROP_PCT / 100):
        # A minimum drop keeps rounding noise off the phone -- any strictly-lower
        # price used to page, so Kingston moving $470.35 -> $470.01 was an alert.
        # Tradeoff: the baseline moves every day, so a slow drift down in
        # sub-threshold steps never trips it. Worth it against daily cent-level
        # noise on half the watchlist.
        if same_item:
            line = f"{title} 平咗：${prev_price:.2f} → ${price:.2f}\n{url}"
            sent = notify("價錢監察", line, priority=3)
            log.info("%s: drop $%.2f -> $%.2f, alert %s", title, prev_price, price,
                      "sent" if sent else "NOT sent")
        elif prev_identity is not None:
            line = f"{title} 有新平嘅盤：${prev_price:.2f} → ${price:.2f}\n{url}"
            sent = notify("價錢監察", line, priority=3)
            log.info("%s: cheaper listing %s -> %s, $%.2f -> $%.2f, alert %s", title,
                      prev_identity, identity, prev_price, price, "sent" if sent else "NOT sent")
        else:
            log.info("%s: current $%.2f, last recorded $%.2f but no listing identity -- "
                     "re-baselined, no alert", title, price, prev_price)
    else:
        log.info("%s: current $%.2f, last recorded $%s -- no alert", title, price,
                  f"{prev_price:.2f}" if prev_price is not None else "n/a (first run)")

    # Only record once per day so a same-day rerun (manual test, retry)
    # doesn't overwrite tomorrow's "yesterday" baseline with today's price.
    if prev_date != today:
        entry = {"date": today, "price": price, "title": title}
        if identity is not None:
            entry["identity"] = identity
        state[key] = entry


def main() -> None:
    from notify import notify

    state = _load_state()
    today = dt.datetime.now(NZ_TZ).date().isoformat()

    # Every observation is persisted as it is made. Saving once at the end
    # meant a malformed payload or a crash in a later item discarded the whole
    # run's baselines *after* its alerts had already gone out, so the next day
    # re-alerted the same drop against a stale baseline -- precisely the
    # milk_watch failure this module was built to avoid.
    for product_id in PRODUCTS:
        try:
            data = _run_skill(CLI, "product", product_id, "--json")
            price = _lowest_available_price(data)
            title = data.get("title", product_id)
            if price is None:
                log.warning("%s: no in-stock offer in response", product_id)
                continue
            _check_and_alert(notify, state, today, product_id, title, price, data.get("url", ""))
        except Exception:
            log.exception("%s: check failed", product_id)
        finally:
            _save_state(state)

    for query, min_price in TRADEME_SEARCHES:
        key = f"trademe:{query}"
        try:
            # 100, not 20: the API returns its own relevance order, so the
            # cheapest of the first 20 was never the cheapest match -- and the
            # set reshuffled daily as new listings posted, moving the baseline
            # on its own. Sorting by price instead doesn't help; the cheapest
            # results are all $2 screen protectors.
            data = _run_skill(TRADEME_CLI, "search", query, "--type", "marketplace", "--limit", "100", "--json", timeout=60)
            listings = [l for l in data.get("listings", [])
                        if l.get("buy_now_price") is not None
                        and l["buy_now_price"] >= min_price
                        and _matches_query(str(l.get("title", "")), query)]
            if not listings:
                log.warning("trademe %s: no matching buy-now listings >= $%.2f found", query, min_price)
                continue
            cheapest = min(listings, key=lambda l: l["buy_now_price"])

            title = f"Trade Me：{query}（{cheapest['title']}）"
            _check_and_alert(notify, state, today, key, title, cheapest["buy_now_price"],
                             cheapest.get("url", ""), identity=str(cheapest.get("listing_id")))
        except Exception:
            log.exception("trademe %s: check failed", query)
        finally:
            _save_state(state)

    for url in URLS:
        try:
            title, price, in_stock = _scrape_schema_offer(url)
            if not in_stock:
                # Same rule as PriceSpy's OutOfStock offers: an unbuyable price
                # is not a price. Skipping also freezes the baseline, so coming
                # back in stock at the old price is not read as a drop.
                log.info("%s: out of stock, skipped", title)
                continue
            # Two shops can sell the same bottle at the same price, so the host
            # is part of the title -- otherwise the ntfy alert cannot say which
            # one moved.
            shop = urllib.parse.urlparse(url).hostname or url
            _check_and_alert(notify, state, today, url,
                             f"{title}（{shop}）", price, url)
        except Exception:
            log.exception("%s: check failed", url)
        finally:
            _save_state(state)


if __name__ == "__main__":
    main()
