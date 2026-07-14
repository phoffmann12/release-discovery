# ADR-0007: Band identity matching — exact match on normalized names

## Status
Accepted (2026-07-14)

## Context
The main correctness risk (ADR-0001): deciding whether a Metal Archives release's
band is the same act as a Spotify/Last.fm artist, across three name-keyed
systems. Metal Archives hosts many distinct bands sharing a name.

## Decision
- Normalize names: lowercase, strip diacritics and punctuation, collapse
  whitespace, drop a leading "the". Build a set of normalized Taste + Similar
  names.
- Match a release's normalized band name by **exact set membership**. No fuzzy /
  edit-distance matching (e.g. "Havok" vs "Havoc" are different acts — fuzzing
  them manufactures false positives).
- Accept that a same-name collision (a different MA band sharing a name with one
  of your artists) can cause a **rare false positive**. Cost is one stray ntfy
  ping — cheap, and tolerable for a completist "everything" feed.

## Consequences
- Simple, fast; no up-front per-artist MA id resolution needed.
- ponytail upgrade path, only if false positives annoy: resolve each artist to a
  Metal Archives band id once (advanced-search) and/or cross-check the feed's
  genre column, then match on id.
- Diacritic stripping handles "Mötley Crüe" → "motley crue". If we later find
  we're *missing* matches from spelling variants, revisit (add limited fuzzing).
