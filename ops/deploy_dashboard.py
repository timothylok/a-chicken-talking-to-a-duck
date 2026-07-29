"""Generate the Category 6 risk dashboard, then auto-publish dashboard/ to
Vercel production if generation actually produced usable data.

Run by the "VoiceOS Risk Dashboard Daily" scheduled task in place of
stock_risk_dashboard.py directly. Deploy is skipped -- not silently
attempted -- if zero tickers generated successfully, since
poll_and_generate() always overwrites dashboard/data/latest.json
(including with an empty list on a fully-failed run, e.g. SEC/Yahoo both
down); auto-publishing that would take a working dashboard offline
unattended. A partial run (some tickers failed, at least one succeeded)
still deploys -- same "show what's available" principle the KPI coverage
string already uses, not all-or-nothing.

Run manually: python ops/deploy_dashboard.py
"""

import json
import logging
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_risk_dashboard as srd  # noqa: E402

DASHBOARD_DIR = os.path.join(ROOT, "dashboard")
LOG_PATH = os.path.join(ROOT, "asr", "logs", "deploy_dashboard.log")

log = logging.getLogger("deploy_dashboard")
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)


def main() -> None:
    written = srd.poll_and_generate()
    if written == 0:
        log.warning("0/%d tickers generated -- skipping deploy, dashboard stays on last good publish",
                    len(srd.WATCHLIST))
        return

    vercel = shutil.which("vercel")
    if not vercel:
        log.error("vercel CLI not found on PATH -- data written locally but not published")
        sys.exit(1)

    log.info("%d/%d tickers generated, deploying to Vercel production", written, len(srd.WATCHLIST))
    result = subprocess.run(
        [vercel, "--prod", "--yes"],
        cwd=DASHBOARD_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        log.error("vercel deploy failed (exit %d): %s", result.returncode, result.stderr.strip()[-2000:])
        sys.exit(1)
    try:
        url = json.loads(result.stdout)["deployment"]["url"]
    except (json.JSONDecodeError, KeyError):
        url = "(url not parsed -- see raw output)"
    log.info("deployed: https://%s", url)


if __name__ == "__main__":
    main()
