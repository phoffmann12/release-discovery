# ADR-0008: Concerts come from Eventim's public search API

## Status
Accepted (2026-07-16)

## Context
Alongside releases, the user wants to be notified about **concerts** of their
taste bands and high-confidence similar bands, near them. That needs a source of
upcoming live events with performer names (to reuse the existing name-match) and
a location filter, ideally free and with strong metal coverage in Germany/EU.

Sources evaluated:
- **Ticketmaster Discovery API** — free instant key, real geo (`latlong`+`radius`),
  well maintained. But coverage skews to large promoted shows; small metal-club
  gigs (the discovery sweet spot) are largely absent.
- **Bandsintown** — best club coverage and artist-centric, but the API is gated to
  **artists and their reps** ("each API key is linked to a single artist"). Not
  usable for a personal listener tool.
- **Songkick** — great data, but new API keys are effectively no longer issued.
- **Spotify Events** — the app shows concerts, but the Web API exposes **no**
  concerts endpoint; its data is fed by Ticketmaster/Songkick upstream anyway, so
  going direct loses nothing. Reaching it means reverse-engineering private,
  app-token endpoints — too fragile for an always-on service.
- **SeatGeek** — free key, but US-centric; weak in Europe.

## Decision
Use **Eventim's public search API** — the undocumented endpoint the eventim.de
frontend itself calls:
`GET https://public-api.eventim.com/websearch/search/api/exploration/v1/products`.
No key. Best German/EU metal coverage of the free options (verified live: Khemmis,
Mork/Soulburn, Thronehammer, Integrity/Ringworm — all of which Ticketmaster omits).

It plugs into the existing architecture as a second **scan-and-match** feed,
exactly parallel to the Metal Archives release scan: pull the local concert feed
for a date window, then match performer names against the *same* taste and
high-confidence similar sets already computed each cycle (CONTEXT.md, ADR-0002).

The feature is **opt-in**: with no `CONCERT_CITIES` set it is skipped entirely and
nothing else changes.

## How the API actually behaves (verified 2026-07-16)
These quirks drove the implementation and are the fragile bits to watch:
- **Access needs Chrome TLS impersonation** (`curl_cffi`), same as Metal Archives
  (ADR-0003); a plain `requests` UA risks blocking.
- **Location is city-based, not a radius.** `city_names` accepts a comma-separated
  union of cities; `radius`/`distance`/coordinate params are ignored, and
  `postal_code` narrows to a single city. Every event *carries* a `geoLocation`,
  but it is the **city centroid**, not the venue, so no client-side radius trim is
  possible either. Hence "near me" is a **list of cities** (`CONCERT_CITIES`), not
  a lat/lon+radius.
- **`categories` only accepts the top-level `Konzerte`** — subcategory values
  ("Metal", "Rock & Pop") return zero. Genre isn't the filter anyway; the
  taste/similar name-match is.
- **No offset pagination.** `top` caps at 50 and the `page` param is a **no-op**
  (every page returns the same first 50). We paginate by **date cursor** instead:
  `sort=DateAsc`, then advance `date_from` to the last date of each batch until a
  batch returns fewer than 50. Boundary-date events reappear across batches and are
  deduped by id.
- **One show is sold as several `productId`s** (GA / VIP / lineup variants). So the
  dedup/`seen` identity is the **show** — `date | city | headliner` (normalized) —
  not the productId, which would otherwise fire N near-identical pushes per gig.

## Consequences
- No new dependency or credential — reuses `curl_cffi`, ntfy, the retry/dedup
  patterns, and the taste/similar sets already in hand.
- Matching is on `attractions[].name`. Support acts Eventim omits from that array
  won't match (a similar band billed only as unlisted support is missed); tribute
  acts ("SaD - Metallica Tribute") normalize to a distinct string, so they never
  false-match a real band.
- The endpoint is undocumented and reverse-engineered — it can change or break
  without notice. Concert failures are isolated (`run_concerts` never raises into
  the release cycle) and alert once, deduped; the "returned rows but none parsed"
  guard fires if the response shape changes, like the MA guard (review #4).
- A separate seed gate (`concerts_initialized`) means enabling concerts on an
  existing install seeds silently once instead of flooding the lookback window.
- Coordinates are city-granular, so `CONCERT_CITIES` must list the actual cities
  you'd travel to (your city + neighbours), using German spellings (Köln, München).

## Alternatives considered and rejected
- **Per-artist search** (`search_term=<band>` for each taste/similar artist).
  Rejected: taste+similar is hundreds–thousands of artists → as many requests per
  cycle; the scan-and-match feed is one bounded walk, consistent with the MA scan.
- **True radius via geocoding** each event's coordinates. Rejected: the API's
  coordinates are city centroids, so a venue-precise radius isn't achievable from
  this source; a city list is the honest interface.
- Ticketmaster/Bandsintown/Songkick/Spotify/SeatGeek — see Context. Ticketmaster
  remains the natural fallback (official, stable geo) **if** Eventim's unofficial
  endpoint is ever locked down.
