"""
US large-cap gap scanner.

GitHub Actions cron jitter can delay a job's *start* by 20+ minutes; it can't
delay what the job does once running. So instead of demanding an exact start
hour, the job starts early and sleeps to a precise wall-clock target before
doing any work. The commit lands at the same moment every trading day
regardless of when Actions got around to starting us. Several crons per mode
plus a same-day idempotency check mean whichever one starts first does the
work; the others see the committed file and exit.

RUN_MODE selects one of two independent captures of the same pipeline
(universe, screening, thresholds, earnings join are shared):
  fast  (default) - sleeps to 09:32:30 ET, ~2 minutes of opening-range bars.
                     Writes data/latest.json. or_move/or_range_pct are
                     near-zero at this point — expected, since the gap
                     (09:30 open vs prior close) is the primary signal and
                     is fully known within a minute of the open.
  range            - sleeps to 10:02:00 ET, full 09:30-10:00 opening range.
                     Writes data/range.json, with the window additionally
                     split at 09:45 into early/late segments so reversals
                     are detectable.

Windows measured (all America/New_York):
  prior session close
  04:00 - 09:30   pre-market
  09:30 - 09:50   opening range (fast mode's or_move/or_range_pct window)
  09:30 - 10:00   opening range (range mode's window; split at 09:45)

fast trades completeness for speed: a symbol needs at least one IEX print
between 09:30 and the capture instant to appear at all (analyse() returns
None otherwise). Thin/low-liquidity names that gap but haven't printed yet
by 09:32:30 are silently absent from data/latest.json, not just missing
segment data — verified directly (2026-08-17 session: 8 fast movers vs. 11
range movers; 3 names present only in range — first prints at 09:37,
09:42, 09:50 per IEX). Moving the target to 09:35:00 was tried and
reverted: it caught none of them (closest miss was still 2 minutes), so
it bought nothing for the extra 2.5 minutes of latency. This is accepted,
not a bug: range mode, 30 minutes later, is the backstop that catches
them.

Output: data/latest.json / data/range.json, each plus a dated archive copy.
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

# RUN_MODE: two independent captures of the same pipeline.
#   fast  - sleeps to 09:32:30 ET, ~2 minutes of opening-range bars. Primary
#           signal (the gap) is fully known by then. (09:35:00 was tried and
#           reverted: it caught none of the thin names 09:32:30 missed —
#           see the module docstring.)
#   range - sleeps to 10:02:00 ET, full 09:30-10:00 opening range. Adds the
#           09:45 early/late segment split so reversals are detectable.
SLEEP_TARGET_TIME = dtime(9, 32, 30)   # fast
RANGE_TARGET_TIME = dtime(10, 2, 0)    # range
MODE_TARGET_TIME = {"fast": SLEEP_TARGET_TIME, "range": RANGE_TARGET_TIME}
# 100 min, not 60: GitHub Actions fires every job on every schedule entry —
# there's no per-job cron filtering — so the range job (target 10:02:00)
# also receives the fast-oriented and wrong-DST-season firings, not just its
# own. Worst real case: the "range primary (EDT)" cron fires at 08:35 ET
# during EST season, 87 minutes before the range target. That's a routine
# recurring event each winter, not a broken-logic edge case, so the cap
# has to clear it with margin. It only needs to catch genuinely broken time
# logic (multi-hour waits), not a legitimate cross-job or wrong-DST firing.
MAX_SLEEP_SECONDS = 100 * 60

MODE_OUTPUT_FILENAMES = {"fast": "latest.json", "range": "range.json"}
MODE_OR_WINDOW_LABEL = {
    "fast": "09:30-09:32 ET (fast capture)",
    "range": "09:30-10:00 ET",
}
MODE_OR_CLOSE_TIME = {"fast": dtime(9, 50), "range": dtime(10, 0)}

# Named so build_windows() (and analyse()'s gap/open calculation) derive
# from the same constants -- a literal duplicated in a label string is
# exactly how a stale "09:35" window reference could reach the JSON.
MARKET_OPEN_TIME = dtime(9, 30)
PRIOR_CLOSE_TIME = dtime(16, 0)

# range mode only: two splits of the opening range.
#   09:32:30 - the fast-mode capture instant ("the alert"). price_0932 /
#              move_since_alert measure whether that alert still holds by
#              the close of the range window. It must match SLEEP_TARGET_TIME
#              exactly — this is measuring the move from the same instant
#              the fast alert actually captured, not an approximation of it.
#   09:45    - splits the remainder into early/late segments (move_early,
#              move_late). Informational only — kept in the JSON for
#              future threshold tuning, but no longer drive the flag.
# The reversal flag prefers move_since_alert; move_late is a fallback for
# names with no print before 09:32:30 (see analyse()). Opposite sign to the
# gap, past REVERSAL_THRESHOLD. Tune the threshold here.
ALERT_SNAPSHOT_TIME = SLEEP_TARGET_TIME  # same instant as fast mode's capture
SEGMENT_SPLIT_TIME = dtime(9, 45)
REVERSAL_THRESHOLD = 0.02

# Wide guard window: this only exists to catch the DST-mismatched cron, not
# to enforce precision. The sleep-to-target above handles precision.
GUARD_WINDOW_START = dtime(8, 30)
GUARD_WINDOW_END = dtime(14, 0)

# US market holidays. Extend annually — this is deliberately explicit rather
# than a dependency, since the list is short and the failure mode of a stale
# holiday is a harmless empty report.
HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


# ---------------------------------------------------------------- scheduling

def mode_output_path(mode: str) -> Path:
    return DATA_DIR / MODE_OUTPUT_FILENAMES[mode]


def already_ran_today(now_et: datetime, mode: str = "fast") -> bool:
    """True if this mode's output file already covers today's ET session.

    Read from the checked-out repo, so this only sees a prior run's output
    once it has been committed and pushed. That's what makes running
    multiple crons per mode safe: whichever lands first does the work; the
    others see the pushed file and exit here. If two land within the same
    minute, the workflow's per-mode concurrency group serialises them
    instead. The check is per-mode (separate files) so fast and range never
    block each other.
    """
    path = mode_output_path(mode)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("session_date") == now_et.strftime("%Y-%m-%d")


def should_run(now_et: datetime, warnings: list[str], mode: str = "fast") -> bool:
    """Multiple UTC cron entries exist per mode, straddling DST plus an
    early backup — any of them can fire on any given morning."""
    if now_et.weekday() >= 5:
        log.info("Weekend. Exiting.")
        return False
    if now_et.strftime("%Y-%m-%d") in HOLIDAYS_2026:
        log.info("Market holiday. Exiting.")
        return False
    if not (GUARD_WINDOW_START <= now_et.time() < GUARD_WINDOW_END):
        log.info("ET time is %s, outside the %s-%s guard window — likely "
                 "the DST-mismatched cron. Exiting.", now_et.strftime("%H:%M"),
                 GUARD_WINDOW_START, GUARD_WINDOW_END)
        return False
    if already_ran_today(now_et, mode):
        log.info("already have today's data, exiting")
        return False
    return True


def target_datetime(now_et: datetime, target_time: dtime = SLEEP_TARGET_TIME) -> datetime:
    """Today's sleep-to-target instant for the given mode's target time."""
    return datetime.combine(now_et.date(), target_time, ET)


def seconds_until_target(now_et: datetime,
                         target_time: dtime = SLEEP_TARGET_TIME) -> float:
    """Seconds from now_et to today's target. Negative if already past it."""
    return (target_datetime(now_et, target_time) - now_et).total_seconds()


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

def _bar_timestamp(bar: dict) -> datetime:
    return datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).astimezone(ET)


def bars_in_window(bars: list[dict], start: datetime, end: datetime) -> list[dict]:
    return [b for b in bars if start <= _bar_timestamp(b) < end]


def is_reversal(move: float | None, gap_pct: float) -> bool:
    """range mode only. True when move (move_since_alert, or the move_late
    fallback) moves the opposite direction from the gap, by at least
    REVERSAL_THRESHOLD. The floor exists because small opposite-sign
    wiggles are just noise on a thin IEX book, not a real reversal."""
    if move is None:
        return False
    if abs(move) < REVERSAL_THRESHOLD:
        return False
    return (move > 0) != (gap_pct > 0)


def analyse(ticker_meta: dict, bars: list[dict], session: date,
            prior: date, mode: str = "fast") -> dict | None:
    open_930 = datetime.combine(session, MARKET_OPEN_TIME, ET)
    or_close_at = datetime.combine(session, MODE_OR_CLOSE_TIME[mode], ET)
    premkt_start = datetime.combine(session, dtime(4, 0), ET)
    prior_close_start = datetime.combine(prior, dtime(15, 50), ET)
    prior_close_end = datetime.combine(prior, PRIOR_CLOSE_TIME, ET)

    prior_bars = bars_in_window(bars, prior_close_start, prior_close_end)
    premkt_bars = bars_in_window(bars, premkt_start, open_930)
    or_bars = bars_in_window(bars, open_930, or_close_at)

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

    row = {
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

    if mode == "range":
        alert_at = datetime.combine(session, ALERT_SNAPSHOT_TIME, ET)
        alert_bars = bars_in_window(bars, open_930, alert_at)
        if alert_bars:
            price_0932 = alert_bars[-1]["c"]
            move_since_alert = (closing / price_0932) - 1
        else:
            price_0932 = move_since_alert = None

        split_at = datetime.combine(session, SEGMENT_SPLIT_TIME, ET)
        early_bars = bars_in_window(bars, open_930, split_at)
        if early_bars:
            price_0945 = early_bars[-1]["c"]
            move_early = (price_0945 / opening) - 1
            move_late = (closing / price_0945) - 1
        else:
            price_0945 = move_early = move_late = None

        row["price_0932"] = round(price_0932, 4) if price_0932 is not None else None
        row["move_since_alert"] = (round(move_since_alert, 5)
                                   if move_since_alert is not None else None)
        row["price_0945"] = round(price_0945, 4) if price_0945 is not None else None
        row["move_early"] = round(move_early, 5) if move_early is not None else None
        row["move_late"] = round(move_late, 5) if move_late is not None else None

        # Prefer move_since_alert; thin names with no print before 09:32:30
        # fall back to move_late so they aren't silently excluded from
        # reversal detection. reversal_basis records which measure was
        # actually used, regardless of whether it fired, for later tuning.
        if price_0932 is not None:
            reversal_basis = "since_alert"
            reversal = is_reversal(move_since_alert, gap_pct)
        elif price_0945 is not None:
            reversal_basis = "late"
            reversal = is_reversal(move_late, gap_pct)
        else:
            reversal_basis = None
            reversal = False
        row["reversal_basis"] = reversal_basis
        row["reversal"] = reversal

    return row


def universe_for_mode(mode: str, warnings: list[str]) -> tuple[list[dict], str]:
    """Single-writer ownership of universe.json: fast mode is the only mode
    that ever rebuilds and saves it (refresh=True, the pre-existing
    behaviour). Range mode is read-only (refresh=False) -- it reads
    whatever fast last wrote, even if stale, and never writes the file
    itself. This works because fast (09:32:30 target) always precedes
    range (10:02:00 target); if fast failed to run that morning, range
    falls back to an older cache -- degraded but correct, and get_universe
    appends a warning so it's visible in the output JSON rather than
    silently masked.
    """
    return get_universe(MIN_MARKET_CAP, refresh=(mode == "fast"), warnings=warnings)


def _fmt_time(t: dtime) -> str:
    return t.strftime("%H:%M")


def build_windows(mode: str) -> dict[str, str]:
    """Explicit, human-readable window labels for the output JSON, derived
    from the same time constants the pipeline actually computes with --
    never a separate hardcoded string that can silently drift from them.
    A downstream consumer (e.g. the email task) should read this dict
    rather than infer windows from field names or comments.

    Fast mode gets only the "gap" key -- it has no segment/reversal data
    to describe.
    """
    windows = {
        "gap": (f"prior close {_fmt_time(PRIOR_CLOSE_TIME)} ET → "
                f"{_fmt_time(MARKET_OPEN_TIME)} open"),
    }
    if mode == "range":
        range_close = MODE_OR_CLOSE_TIME["range"]
        windows["early"] = f"{_fmt_time(MARKET_OPEN_TIME)} → {_fmt_time(SEGMENT_SPLIT_TIME)} ET"
        windows["late"] = f"{_fmt_time(SEGMENT_SPLIT_TIME)} → {_fmt_time(range_close)} ET"
        windows["reversal"] = f"{_fmt_time(ALERT_SNAPSHOT_TIME)} → {_fmt_time(range_close)} ET"
    return windows


def sort_movers(movers: list[dict]) -> list[dict]:
    """Movers must be sorted by abs(gap_pct) descending -- largest gaps
    first, regardless of sign. This is a guarantee of the data layer, not
    an instruction left for a downstream consumer to infer or enforce."""
    movers.sort(key=lambda r: abs(r["gap_pct"]), reverse=True)
    return movers


# ---------------------------------------------------------------------- main

def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    mode = os.environ.get("RUN_MODE", "fast")
    if mode not in MODE_TARGET_TIME:
        log.error("RUN_MODE must be 'fast' or 'range', got %r", mode)
        return 1

    warnings: list[str] = []
    now_et = datetime.now(ET)
    force = os.environ.get("FORCE_RUN") == "1"

    if not force and not should_run(now_et, warnings, mode):
        return 0

    session = now_et.date()
    prior = prior_trading_day(session)
    log.info("Session %s (prior %s) mode=%s", session, prior, mode)

    universe, universe_source = universe_for_mode(mode, warnings)
    meta_by_ticker = {u["ticker"]: u for u in universe}
    symbols = sorted(meta_by_ticker)

    # Sleep to a precise wall-clock target so the commit lands at the same
    # moment every day regardless of Actions' start-time jitter. Everything
    # above this point (guard, universe fetch) is done first so the
    # post-sleep path is just bars -> compute -> write, and stays fast.
    # now_et is refreshed right before the wait is computed (not reused from
    # above) so time spent in the universe fetch doesn't silently push the
    # wake time later than the target.
    if not force:
        target_time = MODE_TARGET_TIME[mode]
        now_et = datetime.now(ET)
        wait = seconds_until_target(now_et, target_time)
        if wait > MAX_SLEEP_SECONDS:
            log.error("Computed wait of %.0fs exceeds the %ds safety cap — "
                      "the time logic looks wrong. Exiting.",
                      wait, MAX_SLEEP_SECONDS)
            return 1
        if wait > 0:
            mins, secs = divmod(int(round(wait)), 60)
            log.info("Waiting %dm %ds until %s ET", mins, secs,
                     target_time.strftime("%H:%M:%S"))
            time.sleep(wait)
        else:
            log.warning("Already %.0fs past the %s ET target "
                        "(started late); proceeding immediately.",
                        -wait, target_time.strftime("%H:%M:%S"))

    # Refresh unconditionally (not just after a real sleep) so bar_end/as_of
    # reflect the actual current time even under FORCE_RUN=1, where the
    # sleep block above is skipped entirely but the universe fetch above it
    # may still have taken a while.
    now_et = datetime.now(ET)

    bar_start = datetime.combine(prior, dtime(15, 45), ET)
    bar_end = now_et
    bars_by_symbol = fetch_bars(symbols, bar_start, bar_end)

    earnings = fetch_earnings(session, warnings)
    earnings.update({k: v for k, v in fetch_earnings(prior, warnings).items()
                     if k not in earnings})

    movers = []
    for symbol, bars in bars_by_symbol.items():
        meta = meta_by_ticker.get(symbol)
        if not meta:
            continue
        row = analyse(meta, bars, session, prior, mode=mode)
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

    movers = sort_movers(movers)

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
        "or_window": MODE_OR_WINDOW_LABEL[mode],
        "windows": build_windows(mode),
        "movers": movers,
    }
    if mode == "range":
        output["reversal_threshold"] = REVERSAL_THRESHOLD

    DATA_DIR.mkdir(exist_ok=True)
    payload = json.dumps(output, indent=1)
    mode_output_path(mode).write_text(payload)
    archive_name = (f"range-{session:%Y-%m-%d}.json" if mode == "range"
                    else f"{session:%Y-%m-%d}.json")
    (DATA_DIR / archive_name).write_text(payload)

    log.info("Done: %d movers from %d names. Warnings: %d",
             len(movers), len(symbols), len(warnings))
    for mover in movers[:10]:
        log.info("  %-6s %+6.2f%% gap  %+6.2f%% OR  earnings=%s",
                 mover["ticker"], mover["gap_pct"] * 100,
                 mover["or_move"] * 100, mover["earnings"]["reported"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
