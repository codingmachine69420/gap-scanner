"""Freshness check for the Gap Scan watchdog workflow (.github/workflows/watchdog.yml).

Deliberately does NOT `import scan` even though scan.py already has this
exact logic (already_ran_today, HOLIDAYS_2026): the watchdog's entire job is
to notice when the main pipeline is broken, so it must not depend on
importing the main pipeline's module (or its requirements.txt) -- if scan.py
or universe.py ever failed to import, a watchdog built on top of it would go
blind at exactly the moment it's needed. This file is intentionally a small,
stdlib-only, self-contained duplicate.

Keep HOLIDAYS_2026 in sync with scan.py's copy by hand -- it's short and
changes once a year.

Prints GITHUB_OUTPUT-format `key=value` lines to stdout.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DATA_DIR = Path("data")

# Keep in sync with scan.py's HOLIDAYS_2026.
HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}

MODE_OUTPUT_FILENAMES = {"fast": "latest.json", "range": "range.json"}


def has_todays_data(today: str, mode: str) -> bool:
    path = DATA_DIR / MODE_OUTPUT_FILENAMES[mode]
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("session_date") == today


def main() -> None:
    now_et = datetime.now(ET)
    today = now_et.strftime("%Y-%m-%d")

    if now_et.weekday() >= 5 or today in HOLIDAYS_2026:
        print("stale=false")
        return

    fast_ok = has_todays_data(today, "fast")
    range_ok = has_todays_data(today, "range")

    print(f"stale={'false' if (fast_ok and range_ok) else 'true'}")
    print(f"missing_fast={'true' if not fast_ok else 'false'}")
    print(f"missing_range={'true' if not range_ok else 'false'}")
    print(f"today={today}")


if __name__ == "__main__":
    main()
