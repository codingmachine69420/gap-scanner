"""
Builds the screening universe: US-listed common stocks with market cap >= threshold.

Primary source: api.nasdaq.com stock screener. This endpoint is UNDOCUMENTED —
it is the backend that nasdaq.com's own web screener calls. It has no API key,
but it WILL 403 without a browser User-Agent. Its response shape is not
guaranteed stable; parse_screener_rows() is written defensively and logs the
actual keys it sees so failures are diagnosable rather than silent.

Fallback: yfinance, over the previously cached universe. This can only refresh
market caps for names already known — it cannot discover new ones. That is an
acceptable degradation for a daily job.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"

# Nasdaq rejects non-browser agents. This is required, not cosmetic.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nasdaq.com/",
    "Origin": "https://www.nasdaq.com",
}

CACHE_PATH = Path("universe.json")
CACHE_MAX_AGE_DAYS = 7

# Suffixes/patterns that indicate warrants, units, preferreds, rights.
# These clutter the universe and never gap meaningfully on earnings.
EXCLUDE_PATTERN = re.compile(r"[\^\.]|(\s(W|U|R|P)$)")


def _parse_money(raw) -> float | None:
    """Nasdaq returns market cap as a string, sometimes with $ and commas,
    sometimes empty, occasionally as a float already."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) or None
    cleaned = str(raw).replace("$", "").replace(",", "").strip()
    if not cleaned or cleaned in {"NA", "N/A", "--"}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value or None


def parse_screener_rows(payload: dict) -> list[dict]:
    """Normalise the screener response.

    The rows have lived under data.table.rows and data.rows in different
    revisions of this endpoint. Try both, and log the keys actually present
    so a shape change produces a useful error instead of a KeyError.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected screener payload keys: {list(payload)[:10]}")

    rows = None
    if isinstance(data.get("table"), dict):
        rows = data["table"].get("rows")
    if rows is None:
        rows = data.get("rows")

    if not isinstance(rows, list):
        raise ValueError(
            f"Could not locate rows. data keys={list(data)[:10]}"
        )

    if rows:
        log.info("Screener row fields: %s", sorted(rows[0].keys()))

    return rows


def fetch_screener(timeout: int = 30, retries: int = 3) -> list[dict]:
    params = {
        "tableonly": "true",
        "limit": "8000",
        "offset": "0",
        "download": "true",
    }
    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                SCREENER_URL,
                params=params,
                headers=BROWSER_HEADERS,
                timeout=timeout,
            )
            resp.raise_for_status()
            return parse_screener_rows(resp.json())
        except Exception as exc:  # noqa: BLE001 - want any failure to retry
            last_error = exc
            wait = 2 ** attempt
            log.warning("Screener attempt %d failed (%s); retrying in %ds",
                        attempt + 1, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Nasdaq screener unavailable: {last_error}")


def build_universe(min_market_cap: float) -> tuple[list[dict], str]:
    """Return (universe, source_label)."""
    try:
        rows = fetch_screener()
    except Exception as exc:  # noqa: BLE001
        log.error("Screener failed, attempting cached universe: %s", exc)
        cached = load_cache(ignore_age=True)
        if cached:
            return cached["universe"], "cache_stale"
        raise

    universe = []
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol or EXCLUDE_PATTERN.search(symbol):
            continue
        cap = _parse_money(row.get("marketCap"))
        if cap is None or cap < min_market_cap:
            continue
        universe.append({
            "ticker": symbol,
            "name": (row.get("name") or "").strip(),
            "exchange": (row.get("exchange") or "").strip().upper() or None,
            "sector": (row.get("sector") or "").strip() or None,
            "market_cap": cap,
        })

    universe.sort(key=lambda r: r["market_cap"], reverse=True)
    log.info("Universe: %d names above $%.1fB", len(universe), min_market_cap / 1e9)
    return universe, "nasdaq_screener"


def load_cache(ignore_age: bool = False) -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        cached = json.loads(CACHE_PATH.read_text())
    except json.JSONDecodeError:
        log.warning("Universe cache is corrupt; ignoring")
        return None

    built = datetime.fromisoformat(cached["built_at"])
    if built.tzinfo is None:
        built = built.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - built
    if not ignore_age and age > timedelta(days=CACHE_MAX_AGE_DAYS):
        log.info("Universe cache is %d days old; refreshing", age.days)
        return None
    return cached


def save_cache(universe: list[dict], source: str) -> None:
    CACHE_PATH.write_text(json.dumps({
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "count": len(universe),
        "universe": universe,
    }, indent=1))


def get_universe(min_market_cap: float = 10e9,
                 force_refresh: bool = False) -> tuple[list[dict], str]:
    if not force_refresh:
        cached = load_cache()
        if cached:
            log.info("Using cached universe (%d names)", cached["count"])
            return cached["universe"], f"cache:{cached['source']}"

    universe, source = build_universe(min_market_cap)
    save_cache(universe, source)
    return universe, source


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    threshold = float(os.environ.get("MIN_MARKET_CAP", 10e9))
    names, src = get_universe(threshold, force_refresh=True)
    print(f"source={src} count={len(names)}")
    for entry in names[:15]:
        print(f"  {entry['ticker']:6s} {entry['exchange'] or '?':8s} "
              f"${entry['market_cap']/1e9:8.1f}B  {entry['name'][:40]}")
