# ADR-0003: Metal Archives access — scrape the AJAX JSON endpoints, politely

## Status
Accepted (2026-07-14)

## Context
Metal Archives has no public API (ADR-0001). It is Cloudflare-fronted and its
robots.txt Disallows AI/bots. **Verified during the build (2026-07-14):** the
JSON endpoint 403s `requests` *even with a real browser User-Agent* — Cloudflare
fingerprints the TLS handshake, not the UA. `curl_cffi` with `impersonate="chrome"`
returns 200. So TLS impersonation is required, not a fallback.

## Decision
Consume the internal DataTables AJAX endpoints as our de-facto API, **via
`curl_cffi` (Chrome impersonation)**:
- Upcoming/recent releases:
  `/release/ajax-upcoming/json/1?sEcho=1&iDisplayStart=N&iDisplayLength=100&fromDate=YYYY-MM-DD&toDate=0000-00-00`
  → JSON `aaData` rows of HTML fragments. Verified column order:
  `[band-anchor, album-anchor, type, genre, release-date, date-added]` (6 cols;
  we read 0–4). Page via `iDisplayStart` against `iTotalRecords`.
- Band lookup → MA id:
  `/search/ajax-advanced/searching/bands/?bandName=<name>&iDisplayStart=0`.
- (Optional) similar bands: `/band/ajax-recommendations/id/<bandId>`.

Access rules, non-negotiable:
- Client: **`curl_cffi` Session, `impersonate="chrome"`** (plain `requests` 403s).
- Throttle to **~1 request/second**. A failed request aborts the cycle and
  retries on the next scan — **the 6-hour scan cycle is the backoff** (no
  per-request retry, deliberately — this is a personal, low-frequency tool).
- **Cache** aggressively; fetch the release feed once per poll cycle.
- Reuse `python-metallum`'s endpoint templates rather than reinventing them.

## Consequences
- ponytail: **hard ceiling.** Unofficial; can break or get us blocked at any
  time (markup change, Cloudflare tightening). If it does, the source must be
  revisited — there is no supported alternative for metal-specific release data.
- Release rows are HTML, not clean JSON → a small parse layer is required.
