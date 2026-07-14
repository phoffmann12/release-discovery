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
- A Release is "new" the first time a scan sees it (past- or future-dated); the
  Seen set guarantees it is pushed exactly once.

## Consequences
- Busy days produce several separate pings. Accepted (user chose per-match over a
  digest). (ponytail: if one scan surfaces a burst, coalesce that scan's matches
  into a single message — a one-line change, only if the pings annoy.)
- No digest scheduling/state needed; push-and-forget per match.
