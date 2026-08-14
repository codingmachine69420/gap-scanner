# Claude Code handoff — setup + verification

Paste everything below the line into Claude Code, with this folder open.

---

You are setting up and then debugging a US large-cap gap scanner. The code in
this folder was written in a sandbox with **no network access to the APIs it
calls**, so all response-parsing logic is educated guesswork and is expected to
be wrong in places. Your job is repo setup, then verification and repair
against live endpoints. Do not redesign the architecture.

There are three points where you must STOP and hand back to me. They are marked
**[STOP]**. Do not attempt to work around them.

## Context

- I am on a VPN from Hong Kong. GitHub Actions runners are US-based. The
  Nasdaq endpoint may respond differently to each — assume nothing that works
  locally works in CI until proven.
- Alpaca free tier is the IEX feed only (~5-10% of consolidated volume).
- The repo is intended to be **public** (free unlimited Actions minutes, and
  the output JSON must be readable at raw.githubusercontent.com without auth).

## Phase 1 — Repo setup

1. Confirm `.gitignore` exists and contains `.env` BEFORE anything else. If it
   doesn't, stop and tell me — do not create any other file first.

2. Initialise, and make the first commit contain only `.gitignore`:
   ```
   git init
   git add .gitignore && git commit -m "gitignore"
   git add . && git commit -m "gap scanner scaffold"
   git branch -M main
   ```

3. Create the remote. Check `gh auth status` first.
   - If authenticated: `gh repo create gap-scanner --public --source=. --push`
   - If not: **[STOP]** Tell me to create it in the browser, and give me the
     exact `git remote add` / `git push` commands to run.

4. Create a `.env` file containing only the two variable names with empty
   values. Do NOT ask me for the key values, and do not read them back to me
   after I fill them in.
   ```
   ALPACA_KEY_ID=
   ALPACA_SECRET_KEY=
   ```

5. **[STOP]** Tell me to do these three things myself, then wait:
   - Paste my Alpaca paper keys into `.env`
   - Add the same two values as GitHub repo secrets (Settings → Secrets and
     variables → Actions)
   - Set Settings → Actions → General → Workflow permissions to **Read and
     write**

6. When I confirm, verify the guard held: `git status --short` must not list
   `.env`, and `git check-ignore -v .env` must print a matching rule. If `.env`
   is tracked, stop immediately and tell me to rotate the keys.

## Phase 2 — Live API verification

Set up Python 3.12 and `pip install -r requirements.txt`. Load `.env` for local
runs. Create `raw_dumps/` for raw API responses (already gitignored).

Work in this order. After each step, report what you found before continuing.

**1. Nasdaq screener — highest risk.** Run `python universe.py`.
`parse_screener_rows()` guesses at both the row location (`data.table.rows` vs
`data.rows`) and the field names (`symbol`, `marketCap`, `exchange`, `sector`,
`name`). Dump the raw JSON, read the actual shape, fix the parser. Sanity
check: roughly 600-900 US-listed names above $10B. If you get 50 or 5000,
something is wrong — investigate rather than accepting it. Note whether the
browser User-Agent was required, and whether it rate-limited.

**2. Alpaca bars.** Test `fetch_bars()` on ~5 known large caps over a recent
trading day. Verify: the `bars` dict is keyed by symbol; each bar has
`t`/`o`/`h`/`l`/`c`/`v`; timestamps are UTC ending in `Z`; `next_page_token`
pagination terminates rather than looping.

Then answer this explicitly, because it may kill half the project's spec:
**does the free IEX feed return any pre-market bars (04:00-09:30 ET) for
typical large caps?** If it returns nothing or near-nothing, `premkt_move` will
be null across the board. Report the actual bar counts you see. Do not paper
over this.

**3. Earnings calendar.** Run `fetch_earnings()` for a date with known
reporters. Verify field names, and specifically what the `time` field contains
as a raw string — then confirm `classify_timing()` maps it correctly to
BMO/AMC.

**4. End to end.** `FORCE_RUN=1 python scan.py` against a recent trading day.
Confirm `data/latest.json` matches the schema in the `scan.py` docstring, and
that a zero-mover day writes a valid file with an empty array rather than
erroring. An empty result is a valid result.

**5. CI — do this even if local passes.** Push, then trigger via
`gh workflow run "Gap Scan"` or the Actions tab. Confirm the run succeeds from
a US-based runner and commits `data/latest.json`. If it fails where local
succeeded, it is almost certainly geographic — report the difference, don't
silently patch around it.

## Constraints

- Never write key values into code, logs, commits, or your replies to me.
- Every external call keeps its timeout and retry/backoff. A dead endpoint
  should append to `warnings[]` in the output, not crash the run.
- Don't add dependencies without telling me why.
- Don't change the 10:05 ET schedule or the dual-cron DST guard.

## Report back

When done, give me: which field names were actually wrong, whether IEX carries
usable pre-market data, the real universe count, and the raw.githubusercontent
URL for `data/latest.json`.
