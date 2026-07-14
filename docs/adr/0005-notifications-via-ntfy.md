# ADR-0005: Notifications delivered via ntfy

## Status
Accepted (2026-07-14)

## Context
Notifications need a push transport to the user's phone. Signal was the initial
preference but has no bot API and requires registering a dedicated number and
running a second container (signal-cli-rest-api) — high setup and maintenance.

## Decision
Deliver notifications via **ntfy**: the service HTTP-POSTs a message to a topic;
the user's ntfy app (public ntfy.sh or self-hosted) receives the push. No
accounts, no auth tokens, no extra container — a single HTTP POST.

## Consequences
- Transport is a one-line HTTP call; trivial to code and test.
- The Spotify re-auth nag (ADR-0004) and any error alerts ride the same topic.
- The topic name is a shared secret (anyone who knows it can read/post) — use an
  unguessable topic, or self-host ntfy if that matters. (ponytail: fine for a
  personal notifier; ceiling noted.)
- If richer/private delivery is wanted later, swap the one POST — nothing else
  depends on the transport.
