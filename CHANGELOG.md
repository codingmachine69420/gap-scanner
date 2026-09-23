# Changelog

Tracks operational fixes and non-code changes that git history alone doesn't
capture — especially config that lives outside this repo (GitHub Actions
scheduling behaviour, the claude.ai routines that email the scan results).
Code changes are already tracked by commits; this file is for the "why"
behind infra/ops fixes and for anything that happened on claude.ai rather
than in the repo.

Re-created 2026-09-19. The previous CHANGELOG was deleted by the 2026-09-17
revert (`81f08f3`). Much of what it asserted was wrong — see below.

## 2026-09-22

**Hourly "arm and sleep" schedule replaces the eight fixed crons. No external
clock, no credentials, all on GitHub.**

The delay got worse. The 9/21 crons arrived 18:18–19:40 UTC (14:18+ ET), past
the 14:00 guard, so **no data was written on 9/21 at all**. On 9/18 they
arrived 16:42–17:49 UTC and data landed at 12:42 ET. That's a 5–7 hour lag.

The fix: stop needing a run to land in a narrow slot. `scan.yml` now fires
`17 * * * *`, every hour of every day. `should_run()`'s guard window opens
`ARM_LEAD` (5h30m) before each mode's target: 04:02:30 ET for fast and
04:32:00 ET for range. The first run landing inside the window sleeps on the
runner until the target. The concurrency groups queue later arrivals, and
`already_ran_today()` turns them into no-ops. Runs outside the window exit in
about 15s. The repo is public, so runner minutes are free. `timeout-minutes`
went from 120 to 360, the hosted-runner maximum. `MAX_SLEEP_SECONDS` is
tied to `ARM_LEAD`. The DST-specific crons are gone because scan.py works in
ET.

Assumption to verify: this assumes GitHub keeps delivering hourly crons
under roughly the same lag. If the lag is 3–10 h, at least one arrival lands
in each 5h30m window. It cannot help on a day where GitHub drops everything
(e.g. 9/2).

Verify: overnight `gh run list --workflow=scan.yml` should show hourly
`schedule` runs, and `scan(fast)` should commit at ~09:33 ET on three
consecutive trading days.

## 2026-09-19

**The scheduling failure that has run since 2026-08-27 was misdiagnosed from
the first day. GitHub was not dropping the crons. It was running them 3.5 to
9.5 hours late, and every investigation checked too early to see them land.**

### What the evidence actually shows

`gh run list --workflow=scan.yml --json createdAt,event` over the full
retained history, labelled by trigger (S = schedule, D = manual dispatch):

```
2026-08-26 | 13:36S 13:56S 14:01S 14:22S 14:25S 14:49S 14:58S 16:02S
2026-08-27 | 17:34D 22:31S 22:51S 22:56S 23:02S 23:03S 23:40S 23:45S 23:54S
2026-08-28 | 15:34D 15:34D 22:34S 22:55S 23:00S 23:11S 23:12S 23:28S 23:31S 23:38S
2026-08-31 | 17:44D 17:44D 19:13S 19:25S 19:29S 19:36S 19:40S 19:53S 19:59S 20:19S
2026-09-01 | 16:51D 16:51D 16:52S 17:12S 17:18S 17:25S 17:26S 17:43S 17:47S 17:55S
2026-09-02 | 14:41D 14:41D
```

All eight crons fired on 8/27, 8/28, 8/31 and 9/1. The CHANGELOG entries for
those days each claimed "zero runs of any kind" — true at the moment someone
looked (10:40-12:50 ET), false by that afternoon. **Only 2026-09-02 was a
genuine full drop.**

Measured run-creation delay (`createdAt == startedAt` on every run, so this
is GitHub-side queuing, not this repo's `concurrency:` groups and not runner
wait time):

| era | delay |
|---|---|
| 8/18-8/26, round-minute crons | ~45-52 min |
| 8/27, 8/28 | ~9.5 h (landed after the close) |
| 8/31 | ~5.5 h |
| 9/1, 9/3, 9/4, 9/7, 9/8, off-round crons | ~3.5-4.2 h |

**The 2026-09-02 cron-minute shift (`ab43cc5`) was inert.** It was recorded
as "a genuine root-cause fix, not a backstop". Delay was the same or worse
after it. The round-minute congestion theory is refuted, not unconfirmed.

### The actual root cause: a zero-margin design

`scan.py` sleeps to a wall-clock target (09:32:30 ET fast, 10:02:00 range),
so it *requires GitHub to create the run before that instant*. The earliest
cron is 08:40 ET — 52 minutes of budget against a scheduler already spending
~50 of them. In the working era only the 08:40 ET backup cron ever did the
work, with roughly zero seconds to spare; the 09:00 ET "primary" was already
arriving late. When the delay grew past the target, every run either
captured hours late or was rejected by `should_run()`'s 14:00 ET guard end
and wrote nothing at all.

`scan(fast)` commit landing times, ET:

```
8/18-8/26   09:32 09:32 09:32 09:32 09:33 09:32 09:36
8/27 on     13:35 11:34 13:44 12:52 10:42 12:37 12:32 12:48 … or never
```

Gap Up Alert checks at 09:42 ET and Brief at 10:30 ET. **Since 8/27 a
scheduled run has never once delivered data before either of them.**

### Two other corrections to the record

- The 9/9-9/17 blackout was not GitHub. Both workflows were
  `disabled_manually` during the v2 migration. `scan.yml`'s `updated_at`
  flipped back to active at 2026-09-17T21:48:46-04:00, 22s after the revert
  commit. 9/18 was the first live schedule day since 9/8.
- 2026-09-18 behaved exactly as the delay theory predicts: zero runs at
  09:42 and 10:30 ET when both routines checked and emailed "not ready",
  then all eight crons arriving 16:42-17:49 UTC, data committed 12:42 ET —
  three hours after the emails.

### Decision

Stop using GitHub's `schedule:` trigger as the clock. It cannot be fixed
from this repo; the only lever that exists (cron minute) was pulled and did
nothing. Keep GitHub Actions as the *runner*.

Two facts make the replacement sound, both measured:

- `workflow_dispatch` is not queued: the 2026-09-18 02:00 UTC dispatch was
  created 01:58:59 and finished 01:59:37, under 40 seconds.
- The claude.ai routine scheduler is punctual: Gap Up Alert fired at
  13:42:12-13:42:59 UTC on every one of the 10 days it was enabled, a
  maximum skew of 59 seconds.

Planned shape: one routine at 09:25 ET on weekdays, dispatching both modes
with `force=false` so `scan.py` still sleeps to its own targets. Fast commits
~09:33 (9 min before Alert), range ~10:03 (27 min before Brief). The eight
`schedule:` crons get deleted — no watchdogs, no backstops, one trigger path.

### Blocked: the routine cannot authenticate to GitHub

Not yet working, and this is where the session ended.

An **API credential** was added to the `gap up scanner` cloud environment
(`env_01EeRmqm2R5UzRjqYXcgxjCE`) — Bearer, host `api.github.com`, header
`Authorization`, holding a fine-grained PAT scoped to this repo with
Actions: Read and write. A dispatch from a routine in that environment still
returns 403.

Diagnosis, from a routine sending **no** Authorization header of its own:

```
GET /user                  -> 200, login codingmachine69420
GET .../actions/workflows  -> 200            (actions=read works)
POST .../dispatches        -> 403
  X-Accepted-Github-Permissions: actions=write
  X-Oauth-Scopes:                            (present, empty)
  X-Ratelimit-Limit: 15000
```

Unauthenticated, the same POST returns **401 Requires authentication**, so a
credential is definitely being attached — the proxy is in the path.

But it is not the PAT. A fine-grained PAT gets a 5000/hr limit and returns no
`X-Oauth-Scopes` header at all. A **GitHub App user-to-server token** returns
the user's own login from `/user`, carries an empty `X-Oauth-Scopes`, and
gets the higher limit. That also explains 2026-09-09's 403 body, which read
`Resource not accessible by integration` — GitHub's wording for App and
OAuth credentials, never for a PAT.

Conclusion: the environment's own built-in GitHub credential (the one that
clones the repo into the sandbox — every run log shows `Fetching repository
codingmachine69420/gap-scanner`) is winning for `api.github.com`, and the
added credential is not being sent. The cloud-environment docs describe this:
*"Two credentials whose hosts overlap without matching exactly get no marker,
and the agent proxy sends only one of them."*

Untried options, in the order worth trying:

1. Go back to an **environment variable** holding the PAT and an explicit
   `Authorization` header in the routine's `curl`. A header the request
   already carries should not be replaced by the proxy. This is how the 9/9
   routine was written, and its 403 is now better explained by the token
   never having been provisioned than by the approach being wrong.
2. Grant the Claude GitHub App installation `Actions: write` on this repo, if
   the App requests that permission — then no PAT is needed at all.
3. Narrow the added credential's host so it does not merely overlap the
   built-in one.

### Security

A fine-grained PAT was pasted into a Claude Code chat transcript during this
session and must be treated as compromised. **Revoke it.** It was never
stored on Anthropic's side: `RemoteTrigger create` accepts an
`environment_variables` field, returns HTTP 200, and silently discards it —
the created routine came back with `environment_variables: {}`. Same class of
silent partial-write as the 9/2 `job_config.ccr` clobber. Verify what was
stored; never assume the 200 means it landed.

### Artifacts left behind on claude.ai

- `trig_01TGM5FrUeAq8PUraNK1oZzR` "Gap Scan tap diagnostic" — disabled
- `trig_01U3JyY5oLftvGeazWu2obWq` "Gap Scan tap diagnostic 2" — disabled,
  holds the three-command credential-identity probe used above
- `trig_0194kd1LLBkyWzqsZH14PBU5` "Gap Scan daily trigger" — still disabled,
  still points at `gap-scanner-v2`, still references an unprovisioned
  `$GAP_SCANNER_DISPATCH_TOKEN`
- Gap Up Alert / Brief are enabled and unchanged; the 9/2 self-heal branch is
  gone, removed by the revert, so they only fetch and report

### Still outstanding, none of it started

- `scan.py` hardening, all of it pre-existing and none of it touched between
  8/27 and the revert (the file is byte-identical across that window):
  - `FORCE_RUN=1` skips the weekend/holiday guard **and** the sleep, and
    `workflow_dispatch` defaults `force: true`. A dispatch before 09:30 ET
    writes a pre-open file stamped with today's `session_date`, which then
    satisfies `already_ran_today()` and blocks the real capture all day.
    This must be fixed before anything dispatches automatically.
  - `GUARD_WINDOW_END = 14:00 ET` turns a late run into no data rather than
    late data (observed 9/8, run at 14:03 ET).
  - No precondition that the capture instant is after the open.
  - No `captured_at` / `on_time` stamp, so a late capture is
    indistinguishable from a punctual one downstream.
- Deleting the eight `schedule:` crons.
- Three consecutive trading days of `scan(fast)` landing before 09:42 ET
  before calling any of this fixed. Every previous fix was declared correct
  on zero days of evidence.
