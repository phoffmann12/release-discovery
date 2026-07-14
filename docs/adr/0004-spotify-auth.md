# ADR-0004: Spotify auth — OAuth refresh token with periodic manual re-auth

## Status
Accepted (2026-07-14)

## Context
The Taste set (top + playlist + saved-album artists) is user-scoped, so the
service needs Spotify Authorization Code OAuth: a one-time browser login mints a
refresh token the headless service uses to get access tokens.

New constraint: Spotify is enforcing **6-month refresh-token expiration from
2026-07-20**. An expired token returns `invalid_grant`, requiring a fresh
browser login. "Set-and-forget headless" no longer holds.

## Decision
- One-time browser OAuth to mint the refresh token; store it in a mounted volume.
- On `invalid_grant`, **the bot notifies the user** over the same chat channel
  with the re-auth link, instead of silently going dark.
- Scopes: `user-top-read`, `user-library-read` (saved albums), and playlist read.
  `user-follow-read` is not needed — followed artists are excluded (CONTEXT.md).

## Consequences
- The user must re-authenticate roughly every 6 months. Accepted — it's the only
  way to read personal Spotify taste; no unattended alternative exists.
- Requires a tiny one-time local web step to catch the OAuth redirect.
