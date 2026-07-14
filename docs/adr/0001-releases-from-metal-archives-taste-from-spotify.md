# ADR-0001: Releases come from Metal Archives, taste comes from Spotify

## Status
Accepted (2026-07-14)

## Context
Goal: get notified about new metal releases matching the user's taste, including
releases by artists *similar* to ones they already like.

Spotify's own new-release surface (Release Radar, "new from followed artists")
exists but is (a) a playlist, not a push, (b) not metal-curated, and (c) opaque.
Metal Archives (Encyclopaedia Metallum) is the canonical, metal-only database and
publishes an upcoming/recent releases listing.

## Decision
- Source new/upcoming **Releases** from **Metal Archives**.
- Use **Spotify** only to derive the **Taste set** (which artists the user likes),
  not as the release feed.
- Scope is **both** Known-artist matches (completeness) and Discovery matches
  (similar artists).

## Consequences
- Metal Archives has **no official API** and actively rate-limits/bans scrapers.
  We must consume it politely: low poll frequency, caching, and (as ADR-0003
  later established) Chrome **TLS-fingerprint impersonation** — a plain browser
  User-Agent is not enough. (ponytail: hard ceiling — if MA blocks us, revisit
  the source.)
- We must **match band identity across two systems** (Metal Archives band name ↔
  Spotify artist). Names collide and differ; matching reliability is the main
  correctness risk. Strategy: open.
- The **Similar artist** source is a separate open decision — Spotify's related-
  artists API may be unavailable to a new app, so Last.fm or Metal Archives' own
  fan recommendations are candidates. (Under research.)
