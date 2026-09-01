# Changelog

Tracks operational fixes and non-code changes that git history alone doesn't
capture — especially config that lives outside this repo (GitHub Actions
scheduling behavior, the Cowork routines that email the scan results). Code
changes are already tracked by commits; this file is for the "why" behind
infra/ops fixes and for anything that happened on claude.ai rather than in
the repo.

## 2026-09-02

**Universe (`universe.py`) — removed the `country == "United States"`
filter; foreign-domiciled large-cap ADRs were being silently excluded
regardless of listing exchange.**

- Symptom: user feedback reported NVS (Novartis) missing from a scan and
  hypothesized a Nasdaq-only screener excluding NYSE names. Verified that
  hypothesis was wrong first — JPM, JNJ, V, HD, KO, DIS, CAT, GE, BA (all
  NYSE) were already present in the cached universe. The screener endpoint
  (`api.nasdaq.com/api/screener/stocks`) aggregates all US exchanges; it has
  no exchange field at all.
- Root cause: `build_universe()` dropped every row where `country !=
  "United States"`. That's domicile, not exchange — it excluded NVS, TM,
  SAP, NVO, ASML, SHEL, BP, UL, TTE, and ~250 other large-cap foreign ADRs
  above $10B regardless of which US exchange they trade on. The filter was
  originally added deliberately to keep the universe near its expected
  600-900 name band.
- Fix: removed the country filter entirely, trading universe-size hygiene
  for coverage. Universe grew from 744 to 980 names (+~32%) on rebuild;
  confirmed NVS/TM/SAP/NVO/ASML/SHEL/BP/UL/TTE now present alongside
  existing domestic names. Flagged to the user that the larger universe
  means more per-run yfinance calls — worth watching scan runtime and rate
  limits on the next live run.

## 2026-09-01

**GitHub Actions (`scan.yml` AND `watchdog.yml`) — both independent
schedule crons silently dropped on the same day, defeating the 8/31
watchdog fix. 4th occurrence of the pattern first noted 2026-08-27/08-28.**

- Symptom: `data/latest.json`/`data/range.json` on `main` still stuck at
  `session_date` 2026-08-31 as of 12:50 ET (well past both scan.yml's
  8:40-9:35 ET cron window and watchdog.yml's 12:47 ET check). The Gap Up
  Alert/Brief Cowork routines correctly detected the stale data (per their
  design) but, per their own instructions, sent a push notification
  instead of an email since the scan wasn't ready.
- Evidence: `gh run list --workflow=scan.yml` showed zero runs of any kind
  between 2026-08-31T20:19:50Z and manual intervention — all 8 of today's
  scheduled crons never fired. `gh run list --workflow=watchdog.yml` showed
  exactly one run ever: the manual `workflow_dispatch` smoke-test right
  after merge on 8/31. Its only scheduled cron (12:47 ET) never fired
  either, confirmed still absent several minutes after its target time.
  Both workflows are `active`, YAML is valid, no permissions/secrets issue.
- Root cause: same as prior occurrences — GitHub's `schedule` trigger is
  best-effort and can silently drop cron events with zero error surface —
  but this time it dropped **both** independently-scheduled workflows
  (`scan.yml`'s 8 crons and `watchdog.yml`'s 1 cron) on the same day. This
  is the exact correlated-failure scenario `watchdog.yml`'s own design
  comment flagged as a residual risk it could reduce but not eliminate,
  because both workflows still ultimately depend on the same underlying
  GitHub Actions scheduler.
- Immediate fix applied: manually dispatched `gh workflow run scan.yml -f
  mode=fast -f force=true` and `-f mode=range -f force=true` (run ids
  33534342712, 33534347514). Both succeeded; verified `session_date:
  2026-09-01` on both files via raw.githubusercontent.com with a
  cache-busting param.
- Durable fix for the GH Actions half: two independent attempts at "make
  GitHub's own scheduler more reliable by adding more of its crons"
  (scan.yml's backup crons, then a whole separate watchdog workflow) had
  now both failed the same way on the same day — per systematic-debugging
  practice, that's a signal to stop depending on GitHub Actions'
  `schedule:` trigger at all rather than add a third one on top. Raised
  with the user as an architecture decision; they chose an external
  trigger. Implemented same day: a new claude.ai scheduled routine, **Gap
  Scan External Watchdog** (`trig_01CioCPwoxjX65KaPfov3D3L`, cron `10 17
  * * 1-5` = 13:10 ET/EDT · 12:10 ET/EST, i.e. after both scan.yml's own
  window and watchdog.yml's 12:47 ET check). It runs on Anthropic's cloud
  scheduler, not GitHub Actions, so a GitHub-side scheduling gap can't
  take it out along with the other two. Each run: clones the repo fresh
  (avoids the raw.githubusercontent.com caching issue entirely — reads
  `data/*.json` from the git clone, not a fetched URL), checks
  `session_date` against today's ET date, and if stale/missing, dispatches
  `scan.yml` via GitHub's REST API using a fine-grained PAT scoped to only
  this repo's Actions permission (the user's deliberate choice over
  reusing this session's broad local `gh` token, to keep blast radius
  small if the routine's stored config or transcripts were ever exposed).
  Silent when data is already current; sends one push notification when
  it has to act. Untested live as of this writing — first real firing is
  the next weekday's 17:10 UTC.
- **Second, separate bug found on user pushback:** the user correctly
  suspected the "no email, push notification instead" behavior wasn't
  purely a GitHub-side issue. Pulled both routines' actual run logs via
  `RemoteTrigger get_run_log` for their 9/1 runs: both correctly detected
  the stale `session_date` (their ET-date check via `TZ=America/New_York
  date` was correct in both — not a timezone bug), but **Gap Up Brief**
  sent the stale-notice via Gmail while **Gap Up Alert** sent it via
  `PushNotification` instead (misfiring once first on a bad `status`
  field before retrying). Root cause: both routine prompts state "Send
  via Gmail to ansonpychan@gmail.com" only once, at the very end, after
  the full success-path email-formatting spec — the early-exit "scan
  wasn't ready"/"empty movers" branches say only "send one line" with no
  channel, so the model has to infer the channel on those paths. Same
  ambiguity in both prompts; different, inconsistent behavior across
  runs (8/31: both correctly used Gmail; 9/1: Alert didn't). Fix applied
  same day: updated both routines via `RemoteTrigger update` to say
  "send one line via Gmail to ansonpychan@gmail.com" explicitly in each
  early-exit branch, not just once at the end. This is unrelated to the
  GitHub Actions scheduling issue above — two independent bugs surfaced
  by the same incident.

## 2026-08-31

**GitHub Actions (`scan.yml`) — scheduled cron didn't fire all day, third
occurrence of the pattern first noted 2026-08-27/08-28.**

- Symptom: `data/range.json` and `data/fast.json` on `main` stuck at
  `session_date` 2026-08-28. `gh run list --workflow=scan.yml` showed zero
  runs of any kind (not even a failed/cancelled attempt) between
  2026-08-28T23:38:42Z and manual intervention at 2026-08-31T17:44Z — i.e.
  all 8 scheduled crons for Monday 8/31 (a normal trading day) silently
  never fired.
- Ruled out: workflow state was `active`, repo Actions permissions were
  `enabled`, no secrets/auth issue (manual `workflow_dispatch` of both
  `fast` and `range` modes succeeded immediately with no code changes), and
  the githubstatus.com incident history shows nothing covering Actions/
  scheduling for 8/31 (or for 8/27/8/28) — only unrelated, already-resolved
  incidents on other days (8/24, 8/26) that *did* produce visible
  failed/cancelled run records, unlike this one.
- Root cause: GitHub's `schedule` trigger is best-effort and, per GitHub's
  own docs, can delay or silently drop cron events under load with no
  error surfaced anywhere in the repo — there's no failed run to alert on
  because no run object is ever created. This is the same failure mode
  logged 2026-08-28 for 8/27 and 8/28, just recurring.
- Why the 8/27–08/28 "fix" didn't prevent this: it wasn't actually a fix,
  it was a manual workaround (a human noticing the "scan not ready" email
  and running `gh workflow run scan.yml -f mode=... -f force=true` by
  hand) noted at the time as "not root-caused... worth a GitHub Support
  ticket if it happens again." Nothing was added to catch or self-heal the
  next occurrence automatically, so it silently recurred for 3 full days
  (8/29 weekend aside) until someone checked the raw file by hand.
- Confirmed the Cowork alert routines *did* work as designed this time —
  they correctly detected the stale `session_date` and sent "scan not
  ready" emails at 13:43 and 14:34 UTC on 8/31 (subjects "Gap Alert — scan
  not ready" / "Daily Gap Ups — scan not ready"). The gap isn't detection,
  it's that detection only produces an easy-to-miss email and nothing
  closes the loop automatically.
- Fix applied: manually dispatched `gh workflow run scan.yml -f mode=fast
  -f force=true` and `-f mode=range -f force=true` (run ids 33421179310,
  33421183682). Both succeeded; `data/fast.json` and `data/range.json` are
  back to `session_date` 2026-08-31, confirmed via the raw
  `raw.githubusercontent.com` URL with a cache-busting param.
- Durable fix implemented: `.github/workflows/watchdog.yml`, a small,
  separately-scheduled workflow (single cron at 16:47 UTC, a couple hours
  after `scan.yml`'s own 8-cron window) that checks whether today's
  session data landed (via the stdlib-only `.github/scripts/
  watchdog_check.py`, deliberately not importing `scan.py` so the watchdog
  can't be taken down by the same bug it exists to catch) and, if not,
  auto-dispatches `scan.yml` for the missing mode(s) and opens/updates a
  `pipeline-watchdog`-labeled GitHub issue, self-closing once fresh data
  lands. A second, differently-timed cron won't eliminate GitHub's
  scheduler flakiness (see root cause above), but makes correlated failure
  (both crons dropped the same day) far less likely, and turns the
  recovery step from "someone reads an email and runs a command by hand"
  into automatic.

## 2026-08-28

**Cowork routines (`Gap Up Alert`, `Gap Up Brief`) — WebFetch serving stale
cached JSON, sometimes weeks old.**

- Symptom: both routines fetch `data/latest.json` / `data/range.json` from
  `raw.githubusercontent.com/.../main/...` every weekday and email a
  "scan wasn't ready" note if `session_date` doesn't match today. They kept
  sending that note even when the actual file on `main` was current — one
  fetch returned a `session_date` two weeks old, six minutes after a fresh
  commit had landed.
- Root cause: the WebFetch tool (or a proxy in front of it) was caching the
  URL far longer than GitHub's own `max-age=300`, so the routines were never
  seeing live content.
- Constraint: both routines' system prompt explicitly forbids falling back to
  `curl`/`bash`/any HTTP library when a fetch looks stale — so the fix could
  not swap WebFetch for a raw HTTP call.
- Fix: appended a cache-busting query parameter (built from the date/time the
  routine already checks for the weekend guard, e.g. `?nocache=20260828-1216`)
  to the fetch URL, still via WebFetch. A changing query string is a new cache
  key, so it can't serve a stale hit.
- Verified: manually ran both routines same-day; both fetched fresh
  `session_date` data and sent real (non-"not ready") emails.
- Where this lives: routine prompts on claude.ai, not in this repo. Routine
  IDs for reference: `trig_01KrMjVeo4EJsDB2uZY2j6zA` (Gap Up Alert, fires
  13:42 UTC), `trig_017Kta6GBhfZAPZfoey47gok` (Gap Up Brief, fires 14:30 UTC).

**Separate, unrelated issue hit during the verification run:** the Brief
routine composed its report correctly but sent the email with a literal
`PLACEHOLDER` body instead of the real content (its own mistake, not a
caching issue). It tried to self-correct but its Gmail connector's OAuth
token expired at that exact moment, blocking both the trash-and-retry and
the resend. It fell back to a push notification with the report content.
Resolved same day: Gmail connector turned out to already be connected (the
expired-token error was transient/session-specific, not an account-level
disconnect — no reauthorization was actually needed). Re-ran the Brief
routine; it fetched the same fresh data, redid its research, and sent a
correct report (`Daily Gap Ups — 2026-08-28 — 4 movers`, real content, not a
placeholder).

**GitHub Actions (`scan.yml`) — scheduled cron not firing.**

- Symptom: zero scheduled runs on 2026-08-27 and again on 2026-08-28 during
  the capture window, despite the workflow being enabled and no active
  GitHub incident covering the second occurrence.
- Workaround applied both days: manually dispatched `mode=fast` and
  `mode=range` via `gh workflow run scan.yml -f mode=<mode> -f force=true`
  to backfill the day's data.
- Not root-caused — recurring "zero attempts, clean workflow state" pattern
  is worth a GitHub Support ticket if it happens again.
