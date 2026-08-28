# Changelog

Tracks operational fixes and non-code changes that git history alone doesn't
capture — especially config that lives outside this repo (GitHub Actions
scheduling behavior, the Cowork routines that email the scan results). Code
changes are already tracked by commits; this file is for the "why" behind
infra/ops fixes and for anything that happened on claude.ai rather than in
the repo.

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
