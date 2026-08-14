"""
US large-cap gap scanner.

Runs once per trading day at ~09:52 ET. Everything it measures is already
history by then, so a late run produces identical output to an on-time one.
This is deliberate: GitHub Actions cron can drift 5-30 minutes under load,
and the design must be indifferent to that.

Windows measured (all America/New_York):
  prior session close
  04:00 - 09:30   pre-market
  09:30 - 09:50   opening range

Output: data/latest.json plus a dated archive copy.
An empty movers list is a VALID result, not an error.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from universe import get_universe

log = logging.getLogger("scan")

ET = ZoneInfo("America/New_York")
DATA_DIR = Path("data")

ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nasdaq.com/",
}

# Tunables
MIN_MARKET_CAP = float(os.environ.get("MIN_MARKET_CAP", 10e9))
MIN_GAP = float(os.environ.get("MIN_GAP", 0.03))
BATCH_SIZE = 200
EXPECTED_RUN_HOUR_ET = 9  # guard: only the 9:xx ET invocation proceeds

# US market holidays. Extend annually — this is deliberately explicit rather
# than a dependency, since the list is short and the failure mode of a stale
# holiday is a harmless empty report.
HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


# ---------------------------------------------------------------- scheduling

def should_run(now_et: datetime, warnings: list[str]) -> bool:
    """Two UTC cron entries exist to straddle DST; only one is correct today."""
    if now_et.hour != EXPECTED_RUN_HOUR_ET:
        log.info("ET hour is %d, not %d — this is the DST-mismatched cron. Exiting.",
                 now_et.hour, EXPECTED_RUN_HOUR_ET)
        return False
    if now_et.weekday() >= 5:
        log.info("Weekend. Exiting.")
        return False
    if now_et.strftime("%Y-%m-%d") in HOLIDAYS_2026:
        log.info("Market holiday. Exiting.")
        return False
    return True


def prior_trading_day(session: date) -> date:
    day = session - timedelta(days=1)
    while day.weekday() >= 5 or day.strftime("%Y-%m-%d") in HOLIDAYS_2026:
        day -= timedelta(days=1)
    return day


# -------------------------------------------------------------------- alpaca

def alpaca_headers() -> dict:
    key = os.environ.get("ALPACA_KEY_ID")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set. "
                 "Set them as GitHub Secrets, or in a local .env for testing.")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def fetch_bars(symbols: list[str], start: datetime, end: datetime,
               timeframe: str = "1Min") -> dict[str, list[dict]]:
    """Fetch bars for many symbols, following pagination.

    Free tier is the IEX feed only (~5-10% of consolidated volume). Prices are
    representative for large caps; volume figures are indicative at best.
    """
    headers = alpaca_headers()
    out: dict[str, list[dict]] = {}

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        # Alpaca uses dot notation for share classes (BRK.B); our universe
        # carries Nasdaq's slash notation (BRK/B). A literal "/" in the
        # symbols param 400s the ENTIRE batch, not just that one symbol, so
        # a single dual-class ticker sharing a batch with 199 others used to
        # take the whole batch down. Translate for the request only and
        # translate back on the way out — safe because EXCLUDE_PATTERN
        # already keeps any "." out of every ticker in the universe.
        api_batch = [s.replace("/", ".") for s in batch]
        page_token = None
        while True:
            params = {
                "symbols": ",".join(api_batch),
                "timeframe": timeframe,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 10000,
                "adjustment": "raw",
                "feed": "iex",
            }
            if page_token:
                params["page_token"] = page_token

            payload = _get_with_retry(ALPACA_BARS_URL, params, headers)
            for symbol, bars in (payload.get("bars") or {}).items():
                out.setdefault(symbol.replace(".", "/"), []).extend(bars)

            page_token = payload.get("next_page_token")
            if not page_token:
                break

        log.info("Bars: %d/%d symbols fetched", min(i + BATCH_SIZE, len(symbols)),
                 len(symbols))
    return out


def _get_with_retry(url: str, params: dict, headers: dict,
                    retries: int = 3, timeout: int = 45) -> dict:
    last = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=timeout)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 5))
                log.warning("Rate limited; sleeping %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = 2 ** attempt
            log.warning("Request failed (%s); retry in %ds", exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Request failed after {retries} attempts: {last}")


# ------------------------------------------------------------------ earnings

def fetch_earnings(day: date, warnings: list[str]) -> dict[str, dict]:
    """Undocumented Nasdaq earnings calendar. Failure here degrades the report
    (movers lose their earnings tag) but must not kill the run."""
    try:
        payload = _get_with_retry(
            NASDAQ_EARNINGS_URL,
            {"date": day.strftime("%Y-%m-%d")},
            BROWSER_HEADERS,
            retries=2,
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Earnings calendar unavailable for {day}: {exc}"
        log.warning(msg)
        warnings.append(msg)
        return {}

    data = payload.get("data") or {}
    rows = data.get("rows")
    if not isinstance(rows, list):
        warnings.append(f"Unexpected earnings payload shape for {day}")
        return {}

    if rows:
        log.info("Earnings row fields: %s", sorted(rows[0].keys()))

    result = {}
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if symbol:
            result[symbol] = {
                "date": day.strftime("%Y-%m-%d"),
                "time_raw": row.get("time"),
                "eps_actual": row.get("eps"),
                "eps_estimate": row.get("epsForecast"),
                "surprise_pct": row.get("surprise"),
            }
    return result


def classify_timing(time_raw: str | None) -> str | None:
    if not time_raw:
        return None
    lowered = time_raw.lower()
    if "pre" in lowered or "before" in lowered:
        return "BMO"
    if "after" in lowered or "post" in lowered:
        return "AMC"
    return "UNKNOWN"


# ------------------------------------------------------------------ analysis

def bars_in_window(bars: list[dict], start: datetime, end: datetime) -> list[dict]:
    window = []
    for bar in bars:
        stamp = datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).astimezone(ET)
        if start <= stamp < end:
            window.append(bar)
    return window


def analyse(ticker_meta: dict, bars: list[dict], session: date,
            prior: date) -> dict | None:
    open_930 = datetime.combine(session, dtime(9, 30), ET)
    or_close = datetime.combine(session, dtime(9, 50), ET)
    premkt_start = datetime.combine(session, dtime(4, 0), ET)
    prior_close_start = datetime.combine(prior, dtime(15, 50), ET)
    prior_close_end = datetime.combine(prior, dtime(16, 0), ET)

    prior_bars = bars_in_window(bars, prior_close_start, prior_close_end)
    premkt_bars = bars_in_window(bars, premkt_start, open_930)
    or_bars = bars_in_window(bars, open_930, or_close)

    if not prior_bars or not or_bars:
        return None

    prior_close = prior_bars[-1]["c"]
    if not prior_close:
        return None

    opening = or_bars[0]["o"]
    closing = or_bars[-1]["c"]
    highs = max(b["h"] for b in or_bars)
    lows = min(b["l"] for b in or_bars)

    gap_pct = (opening / prior_close) - 1

    return {
        **ticker_meta,
        "prior_close": round(prior_close, 4),
        "premkt_last": round(premkt_bars[-1]["c"], 4) if premkt_bars else None,
        "premkt_move": round((premkt_bars[-1]["c"] / prior_close) - 1, 5)
                       if premkt_bars else None,
        "premkt_bar_count": len(premkt_bars),
        "open_930": round(opening, 4),
        "or_close": round(closing, 4),
        "gap_pct": round(gap_pct, 5),
        "or_move": round((closing / opening) - 1, 5),
        "or_range_pct": round((highs - lows) / opening, 5),
        "or_high": round(highs, 4),
        "or_low": round(lows, 4),
        "volume_or": sum(b.get("v", 0) for b in or_bars),
    }


# ---------------------------------------------------------------------- main

def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    warnings: list[str] = []
    now_et = datetime.now(ET)
    force = os.environ.get("FORCE_RUN") == "1"

    if not force and not should_run(now_et, warnings):
        return 0

    session = now_et.date()
    prior = prior_trading_day(session)
    log.info("Session %s (prior %s)", session, prior)

    universe, universe_source = get_universe(MIN_MARKET_CAP)
    meta_by_ticker = {u["ticker"]: u for u in universe}
    symbols = sorted(meta_by_ticker)

    bar_start = datetime.combine(prior, dtime(15, 45), ET)
    bar_end = datetime.combine(session, dtime(9, 52), ET)
    bars_by_symbol = fetch_bars(symbols, bar_start, bar_end)

    earnings = fetch_earnings(session, warnings)
    earnings.update({k: v for k, v in fetch_earnings(prior, warnings).items()
                     if k not in earnings})

    movers = []
    for symbol, bars in bars_by_symbol.items():
        meta = meta_by_ticker.get(symbol)
        if not meta:
            continue
        row = analyse(meta, bars, session, prior)
        if not row or abs(row["gap_pct"]) < MIN_GAP:
            continue

        report = earnings.get(symbol)
        row["earnings"] = {
            "reported": bool(report),
            "timing": classify_timing(report.get("time_raw")) if report else None,
            "date": report.get("date") if report else None,
            "eps_actual": report.get("eps_actual") if report else None,
            "eps_estimate": report.get("eps_estimate") if report else None,
            "surprise_pct": report.get("surprise_pct") if report else None,
        }
        movers.append(row)

    movers.sort(key=lambda r: abs(r["gap_pct"]), reverse=True)

    covered = len(bars_by_symbol)
    if covered < len(symbols) * 0.5:
        warnings.append(
            f"Only {covered}/{len(symbols)} symbols returned bars — "
            "IEX coverage may be degraded."
        )

    output = {
        "as_of": now_et.isoformat(),
        "session_date": session.strftime("%Y-%m-%d"),
        "prior_session_date": prior.strftime("%Y-%m-%d"),
        "universe_size": len(symbols),
        "symbols_with_bars": covered,
        "thresholds": {"min_market_cap": MIN_MARKET_CAP, "min_gap": MIN_GAP},
        "data_sources": {
            "universe": universe_source,
            "bars": "alpaca_iex",
            "earnings": "nasdaq_calendar",
        },
        "data_caveat": (
            "Bars are Alpaca free-tier IEX feed (~5-10% of consolidated "
            "volume). Prices are representative for large caps; volume is "
            "indicative only."
        ),
        "warnings": warnings,
        "or_window": "09:30-09:50 ET",
        "movers": movers,
    }

    DATA_DIR.mkdir(exist_ok=True)
    payload = json.dumps(output, indent=1)
    (DATA_DIR / "latest.json").write_text(payload)
    (DATA_DIR / f"{session:%Y-%m-%d}.json").write_text(payload)

    log.info("Done: %d movers from %d names. Warnings: %d",
             len(movers), len(symbols), len(warnings))
    for mover in movers[:10]:
        log.info("  %-6s %+6.2f%% gap  %+6.2f%% OR  earnings=%s",
                 mover["ticker"], mover["gap_pct"] * 100,
                 mover["or_move"] * 100, mover["earnings"]["reported"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
