# CONTEXT

Domain glossary for the metal-release notifier. These are the words we use in
code, ADRs, and notifications. Don't drift to synonyms.

## Glossary

- **Release** — a new or upcoming album/EP/etc. listed on Metal Archives that is
  a candidate for a notification. All release types count (full-length, EP,
  single, demo, split, live, compilation, reissue).
- **Taste set** — the artists derived from the user's Spotify that define "my
  taste": the union of **top artists**, **playlist artists**, and **saved-album
  artists** (deliberately *not* followed artists). Broad and noisy by design;
  filtered to real metal bands at match time — an artist with no Metal Archives
  entry can't produce a match.
- **Similar artist** — an artist related to a Taste-set artist, from **Last.fm**
  `artist.getSimilar` (ranked, 0–1 match score). See ADR-0002.
- **Known-artist match** — a Release whose band is in the Taste set. Serves the
  "never miss a drop" goal. Fires for *all* release types.
- **Discovery match** — a Release whose band is a **high-confidence** Similar
  artist not itself in the Taste set: similar to ≥2 taste artists (consensus) or
  match score ≥0.6. Serves the "find me new bands" goal without flooding.
- **Notification** — an ntfy push (ADR-0005) announcing matched Release(s),
  tagged known-artist or discovery.
- **Seen set** — Releases already notified, so we never notify the same one twice.
