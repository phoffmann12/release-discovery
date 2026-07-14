# Metal release notifier

Pushes an [ntfy](https://ntfy.sh) notification for new metal releases on
[Metal Archives](https://www.metal-archives.com/) that match your Spotify taste —
both bands you already listen to and high-confidence similar bands.

Design + decisions: [`CONTEXT.md`](CONTEXT.md) (glossary) and
[`docs/adr/`](docs/adr/) (why each choice was made).

## What you need first

1. **Spotify app** — <https://developer.spotify.com/dashboard> → *Create app*.
   Add redirect URI `http://localhost:8888/callback`. Copy the Client ID + Secret.
2. **Last.fm API key** (free) — <https://www.last.fm/api/account/create>.
3. **ntfy** — install the app (iOS/Android/web), pick an unguessable topic name,
   and subscribe to it. That topic string is your `NTFY_TOPIC`.

## Setup

```bash
cp .env.example .env      # fill in the 4 required values
docker compose build
docker compose run --rm notifier auth   # one-time Spotify login (paste redirect URL)
docker compose up -d                     # runs forever, scans every 6h
docker compose logs -f                   # watch it
```

The first scan **seeds silently** — it records everything currently listed and
only pings you about releases that appear *after* that. You'll get one "started"
notification confirming how many artists it's tracking.

## Deploy on Portainer

Two things differ from the local flow: secrets go in Portainer's env-var fields
(not a `.env` file), and the one-time Spotify login is done from Portainer's
container console.

**1. Add the stack from this repo.** Portainer → **Stacks → Add stack →
Repository**:
- Repository URL: `https://github.com/phoffmann12/release-discovery`
- If the repo is **private**, toggle **Authentication** on and enter your GitHub
  username + a Personal Access Token with read access to the repo.
- Reference: `refs/heads/main`
- Compose path: **`portainer-stack.yml`** (not `docker-compose.yml` — that one is
  for local use and expects a `.env` file). Portainer builds the image from the
  repo's Dockerfile.

**2. Environment variables.** In the stack's **Environment variables** section add
the four required values — `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`,
`LASTFM_API_KEY`, `NTFY_TOPIC`. (`portainer-stack.yml` reads them via `${...}`;
no secret lives in the repo.) Then **Deploy the stack**.

**3. One-time Spotify login.** After the stack is up it'll ping your ntfy topic
"Spotify re-auth needed" (no token yet). In Portainer → **Containers →** the
notifier → **Console** → connect with `/bin/sh`, then:

```sh
python main.py auth
```

Open the printed URL, approve, and copy the FULL `localhost:8888/callback?...`
URL your browser lands on (it won't load — that's fine, you only need the URL) and
paste it back. The token is written to the `metal-data` volume. **Recreate/restart
the container** so it picks the token up immediately; the first scan then seeds
silently and you're live. Same console command handles the ~6-monthly re-auth.

> The `http://localhost:8888/callback` redirect URI just has to be registered in
> your Spotify app and match `SPOTIFY_REDIRECT_URI` (the default). Nothing actually
> listens on that port — you're only harvesting the code from the URL.

**Prefer the API?** [`deploy-portainer.sh`](deploy-portainer.sh) does steps 1–2
over the Portainer API instead of the UI (needs `curl` + `jq`; see the env vars
in its header). The one-time Spotify login (step 3) is still manual.

## Notes

- **Spotify re-auth ~every 6 months.** Spotify expires refresh tokens; when that
  happens the bot pings your ntfy topic to re-run `... notifier auth`. It keeps
  scanning on your last-known taste until you do.
- **Metal Archives has no API** and blocks bots — this reads its internal JSON
  endpoints via `curl_cffi` Chrome TLS impersonation (a plain browser user-agent
  gets 403'd) at ~1 req/s. It can break if MA changes its markup or tightens
  Cloudflare (ADR-0003). If scans start 403ing or "returned rows but none
  parsed", that's the cause.
- Tune discovery vs. noise via `SIMILAR_SCORE_MIN` / `SIMILAR_CONSENSUS` in `.env`.

## Dev

```bash
python main.py selftest   # asserts for normalize / parse / match / discovery filter
python main.py once       # single cycle (needs .env + a cached token)
```
