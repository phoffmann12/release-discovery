# ADR-0002: Discovery similarity comes from Last.fm, not Spotify

## Status
Accepted (2026-07-14)

## Context
Discovery matches need a "similar artist" graph seeded from the Taste set.
Spotify was the obvious source, but its Get Related Artists and Get
Recommendations endpoints have been restricted since 2024-11-27 and are
unavailable to new / development-mode apps (they return 403/404). We need
another similarity source.

## Decision
Use **Last.fm `artist.getSimilar`** for the Similar-artist graph.
- API-key only, no user auth; free; strong metal coverage.
- Returns ranked similar artists with a 0–1 match score, which we use to make
  Discovery "high-confidence only".

**Default high-confidence rule (tunable knob):** an artist is a Discovery
candidate if it is returned as similar to **≥2 Taste-set artists** (consensus),
or similar to ≥1 with **match score ≥ 0.6**. Everything else is dropped.

## Consequences
- Similarity quality is Last.fm's, not Spotify's — generally good for metal, but
  crowd-sourced and mainstream-biased at the head.
- Adds one credential: a free Last.fm API key.
- Name matching now spans three systems (Spotify, Last.fm, Metal Archives), all
  keyed on artist name — the matching strategy (open) must cover Last.fm too.
- Metal Archives' own per-band fan recommendations
  (`/band/ajax-recommendations`) remain a possible *supplement* if Last.fm
  coverage is thin. Deferred.

## Alternatives considered and rejected (revisited 2026-07-14)

- **Fold similarity into Metal Archives** (drop Last.fm, use MA fan-recs as the
  only similarity source). Rejected: needs resolving every taste artist to an MA
  band id (name collisions) plus a recs fetch each — ~2× the scrape load on the
  one fragile, ban-prone source — and MA recs are sparse and vote-ranked, so the
  0–1 score threshold is lost and discovery gets thinner. More code, more risk,
  worse results, to save one free key.
- **Drop discovery entirely** (Spotify + MA, known-artist only, no Last.fm).
  Rejected: the user explicitly wants similar-artists' releases. Kept as the clean
  fallback *if* discovery is ever judged not worth the one free API key.

Decision reaffirmed: **keep Last.fm** — a key-only API (no OAuth/login, no
maintenance) is the cheapest possible dependency and keeps similarity risk off
the MA scrape.
