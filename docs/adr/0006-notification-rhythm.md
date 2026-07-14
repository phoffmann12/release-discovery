# ADR-0006: Notification rhythm — immediate, one push per match

## Status
Accepted (2026-07-14)

## Context
Decide when and how matched Releases reach the user.

## Decision
- **Scan Metal Archives ~4×/day** (≈ every 6 hours) — well within the ~1 req/s
  politeness budget (ADR-0003), and MA's feed changes at most daily.
- Notify **immediately** on each newly-seen match: **one ntfy push per Release**,
  tagged known-artist or discovery.
- **Window = [today, today + `NOTIFY_LEAD_DAYS`]** (default 7): only releases
  dropping within the next week are announced. The feed is bounded with
  `fromDate = today - MA_LOOKBACK_DAYS` (default **0**, i.e. today onward) and
  `toDate = today + lead` — MA filters server-side (verified). A far-future
  release stays silent until it enters the window. Raise `MA_LOOKBACK_DAYS` only
  for a downtime safety margin (re-catch releases that dropped while the service
  was off).
- A Release is "new" the first time a scan sees it *within that window*; the Seen
  set guarantees it is pushed exactly once. Future releases enter the window as
  their date approaches and are notified then.

## Consequences
- Busy days produce several separate pings. Accepted (user chose per-match over a
  digest). (ponytail: if one scan surfaces a burst, coalesce that scan's matches
  into a single message — a one-line change, only if the pings annoy.)
- No digest scheduling/state needed; push-and-forget per match.
