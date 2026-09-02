# Changelog

Tracks operational fixes and non-code changes that git history alone doesn't
capture — especially config that lives outside this repo (GitHub Actions
scheduling behavior, the Cowork routines that email the scan results). Code
changes are already tracked by commits; this file is for the "why" behind
infra/ops fixes and for anything that happened on claude.ai rather than in
the repo.

## 2026-09-02

**GitHub Actions (`scan.yml`) — schedule cron silently dropped again, 5th
occurrence of the pattern (8/27, 8/28, 8/31, 9/1 x2, now 9/2). External
watchdog routine had not reached its scheduled check time yet when this
was caught; fixed by manual dispatch instead of waiting for it.**

- Symptom: `data/latest.json`/`data/range.json` on `main` still stuck at
  `session_date` 2026-09-01 as of 10:39 ET, well past all 4 of scan.yml's
  today-relevant crons (08:40/09:00 EDT fast, 09:15/09:35 EDT range — all
  had already passed). `gh run list --workflow=scan.yml` showed zero runs
  of any kind on 2026-09-02 before manual intervention, confirming another
  full correlated drop of the schedule trigger, not a partial/flaky one.
- Both Cowork routines correctly detected the stale data and, this time,
  both emailed the "not ready" notice via Gmail as designed (Gap Up Alert
  at 13:43 UTC, Gap Up Brief at 14:33 UTC) — the 9/1 fix naming Gmail
  explicitly in both routines' early-exit branches held up under a real
  recurrence.
- `watchdog.yml` (GH-hosted, 16:47 UTC check) and the new external
  claude.ai routine "Gap Scan External Watchdog" (17:10 UTC check,
  `trig_01CioCPwoxjX65KaPfov3D3L`) had not yet reached their scheduled
  check times when the user reported the stale-data email (~14:39 UTC) —
  today was to be the external watchdog's first live scheduled firing.
  Rather than wait ~2.5h to see whether either backstop would have caught
  it, fixed immediately via the same manual path used on 9/1: `gh workflow
  run scan.yml -f mode=fast -f force=true` and `-f mode=range
  -f force=true`. Both succeeded; confirmed `session_date: 2026-09-02` on
  both files after pulling `main`. Because the data was already fresh by
  the time watchdog.yml and the external watchdog reached their check
  windows, neither had anything to do — consistent with their "silent
  backstop" design, but it also means this occurrence didn't actually
  exercise either backstop end-to-end. Worth checking after the next
  correlated drop that isn't manually pre-empted.
- **Security note, unrelated to the scheduling bug:** fetching the
  external watchdog routine's config via `RemoteTrigger get` returns its
  full prompt, which embeds the fine-grained GitHub PAT in plaintext. That
  PAT is scoped to just this repo's Actions read/write permission, but it
  is now visible in this session's transcript (and would be in any future
  session that re-fetches the routine's config the same way). Flagged to
  the user; worth considering a secret-store mechanism if the routine
  platform offers one, rather than embedding the token directly in the
  prompt text, and rotating this PAT given it's now been printed to a
  transcript.

**Follow-up same day: found and fixed the actual reason this kept
recurring — the external watchdog (added 9/1) can never prevent the
"scan not ready" email by construction, because of a scheduling-order
bug in its own design.**

- Gap Up Alert checks at 13:42 UTC (9:42 ET) and Gap Up Brief at 14:30 UTC
  (10:30 ET). The external watchdog was scheduled to check at 17:10 UTC
  (13:10 ET) — deliberately placed *after* scan.yml's own cron window and
  watchdog.yml's check, but nobody accounted for Alert/Brief firing hours
  *before* that. So even on a day the external watchdog works perfectly,
  Alert/Brief will already have sent their stale-data notice by the time
  it runs. This is an architecture bug in the 9/1 fix, not a new instance
  of the GH Actions scheduling issue.
- User pushback ("this has been the issue all the time. fix!!") after a
  6th occurrence prompted re-examining the fix itself rather than treating
  it as more of the same known cron-drop pattern.
- Fix: gave Gap Up Alert (`trig_01KrMjVeo4EJsDB2uZY2j6zA`) and Gap Up Brief
  (`trig_017Kta6GBhfZAPZfoey47gok`) self-healing early-exit branches
  instead of depending on a separately-timed watchdog. Both previously
  only had WebFetch (no Bash) and, on stale `session_date`, went straight
  to the "not ready" email. Updated via `RemoteTrigger update` to add
  `Bash` to `allowed_tools` and rewrote each freshness-check branch so
  that on stale data it now: (1) dispatches `scan.yml` itself via `curl`
  + the same fine-grained PAT the external watchdog uses (`mode: fast`
  for Alert, `mode: range` for Brief), (2) waits ~120s, (3) re-fetches
  and rechecks `session_date`, and only falls back to the "not ready"
  email if it's still stale or the dispatch failed. This fixes it at the
  point of failure with no timing dependency on another routine.
- Verified the PAT itself is still live via a read-only authenticated
  `GET .../actions/workflows/scan.yml` (200) rather than spending another
  real scan dispatch to test it; did not end-to-end test the new Bash
  code path inside Alert/Brief's actual cloud sandbox (would require
  artificially re-staling already-fresh data, judged not worth the
  disruption) — first real test is whichever weekday next has scan.yml
  drop its cron again. If Bash/curl turns out to lack network egress in
  Alert/Brief's environment (`env_011111111111111111111117`, the
  platform's shared default — untested for outbound API calls), the
  dispatch attempt fails closed into the existing "not ready" email, i.e.
  no worse than before.
- Left `watchdog.yml` and the external claude.ai watchdog running as-is;
  they're free extra backstops, just no longer the only ones and no
  longer load-bearing for stopping this specific email.
- Spreads the same GitHub PAT into 2 more routine prompts (3 total now).
  Reiterating: worth rotating given how many times it's now been printed
  into transcripts/API responses, and worth a real secret-store mechanism
  if the routine platform gets one.

**Follow-up #3 same day: the 9/2 self-heal edit silently broke BOTH
routines by dropping their `permission_mode: "auto"` event — caught only
because the user asked to run Alert manually. Both would have hung
forever on their next scheduled fire, sending nothing at all.**

- Symptom: a manual `RemoteTrigger run` of Gap Up Alert stalled
  indefinitely on its first `WebFetch`, parked on a permission prompt.
  Event count in `get_run_log` stayed frozen across repeated polls.
- Root cause: `RemoteTrigger action:"update"` does **not** deep-merge
  `job_config.ccr` — it replaces it wholesale, returning HTTP 200 with no
  warning about what was dropped. These routines carry TWO events: a
  `control_request` setting `permission_mode: "auto"`, then the user
  prompt. The self-heal update earlier that day sent only the prompt
  event, silently discarding the permission-mode event. Both routines
  fell back to interactive permissions, where an unattended cron run
  waits forever for an approval no human will give. Confirmed by diffing
  the current config against the pre-edit capture: `derived_state` had
  lost its `"permission_mode":"auto"` key entirely.
- Impact if unnoticed: the next scheduled Alert (13:42 UTC) and Brief
  (14:30 UTC) would each have hung and sent NOTHING — strictly worse than
  the stale-data problem the edit was meant to fix, and silent (no error,
  no email, just absence).
- Two wrong turns before the fix, recorded honestly: (1) first hypothesis
  was that adding `Bash` to `allowed_tools` had flipped it into
  explicit-allowlist mode, so `WebFetch` was added there too — did not
  help, the stall reproduced identically; (2) that same partial update
  then clobbered `events` entirely (`events: []`, `model: ""`), leaving
  both routines promptless for ~90 seconds until it was spotted and
  restored. No scheduled fire fell in that window, so no run was affected.
- Fix: re-sent the COMPLETE `job_config.ccr` for both routines —
  `environment_id`, `session_context` (`model`, `sources`,
  `allowed_tools` incl. `Bash` + `WebFetch`), and BOTH events with the
  original `request_id`/`uuid` for the control_request. Verified
  `derived_state.permission_mode` is `"auto"` again on both.
- Verified live, not just by reading config back: re-ran Alert, watched
  `WebFetch` execute with no permission prompt, fetch fresh
  `session_date: 2026-09-02`, and deliver "Gap Alert — 2026-09-02 — 3
  movers" (MDB −12.88%, CRDO, +1) to Gmail at 16:22 UTC. Note it sent two
  near-identical copies 9s apart in that one manual run — cosmetic, from
  the test only, not investigated further.
- **Lesson for any future routine edit:** always send the complete
  `job_config.ccr` including the control_request event, and always verify
  by actually running the routine, not by reading the config back. The
  config looked entirely plausible while being fatally broken. See also
  the standing memory note on this trap.
- Also worth noting: today's manual test exercised only the "data already
  fresh" happy path. The self-heal branch (curl → GitHub API → wait →
  recheck) still has never executed; whether `curl` has network egress to
  api.github.com from that sandbox remains unproven. `Bash` itself does
  work there (the run executed `TZ=America/New_York date` fine).

**Follow-up #2 same day: found and fixed the actual root cause of the
repeated cron drops (`scan.yml`) instead of adding another workaround
layer — GitHub's own documented scheduling congestion at round-minute
marks.**

- User pushed back on treating the 6th occurrence as "just add another
  failsafe" and asked for the underlying bug fixed instead.
- Investigated instead of re-patching: repo is public, not archived, not
  disabled (`gh api repos/.../…` confirmed); both `scan.yml` and
  `watchdog.yml` show `state: active` (not `disabled_inactivity`); Actions
  permissions are `allowed_actions: all`, `enabled: true`. Ruled out the
  usual causes (repo-disabled, workflow-disabled, quota, fork settings).
- `scan.yml` hasn't been edited since 2026-08-19, but 5 of the 6-7 trading
  days since 2026-08-27 had a full-day cron drop — far too high a rate for
  "occasional best-effort flakiness," and every failure was a complete
  drop (zero run objects for every cron in the file that day), not a
  partial or delayed one.
- Root cause, confirmed via GitHub's own documentation and community
  reports (see links in this session's chat): GitHub Actions' scheduled-
  workflow queue gets its worst congestion at round-number minutes,
  especially the top of the hour (`:00`) — and under high load the run can
  be **dropped with zero trace**, not merely delayed, which is exactly
  this repo's symptom. `scan.yml`'s 8 crons were all on round 5-minute
  marks (`:00`, `:15`, `:35`, `:40`), with 2 of the 8 sitting exactly on
  `:00`, the single worst spot.
- Fix: shifted every `scan.yml` cron minute off round marks (`0→7`,
  `40→33`, `35→38`, `15→18`), preserving the same relative timing and
  buffer before `scan.py`'s internal sleep-to-target. `watchdog.yml`'s
  cron (`:47`) was already off round marks, no change needed there.
- This is a genuine root-cause fix, not a backstop — first real
  confirmation is whichever weekday's crons fire next; if the drop rate
  drops to roughly zero after this, that confirms the theory, and if it
  recurs anyway, that rules it out and points back to something else
  (possibly GitHub-side and out of this repo's control).

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
