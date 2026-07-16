#!/usr/bin/env python3
"""Metal-release notifier. See CONTEXT.md + docs/adr/ for the design.

Pipeline: Spotify taste -> Last.fm similar (high-confidence) -> scan Metal
Archives -> normalized-name match -> ntfy push, one per new release. The same
taste/similar sets also drive an opt-in concert scan of Eventim (ADR-0008).

Commands:
  python main.py run       # loop forever (default; container entrypoint)
  python main.py once      # one scan cycle, then exit
  python main.py auth      # one-time interactive Spotify login
  python main.py selftest  # run the built-in asserts
"""
import os, sys, re, json, html, time, sqlite3, unicodedata, datetime
import requests
from curl_cffi import requests as cffi_requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError
from spotipy.cache_handler import CacheFileHandler

SCOPES = "user-top-read user-library-read playlist-read-private playlist-read-collaborative"


class Cfg:
    def __init__(self):
        e = os.environ.get
        self.spotify_id = e("SPOTIFY_CLIENT_ID", "")
        self.spotify_secret = e("SPOTIFY_CLIENT_SECRET", "")
        self.spotify_redirect = e("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
        self.spotify_cache = e("SPOTIFY_CACHE", "/data/.spotify-token")
        self.lastfm_key = e("LASTFM_API_KEY", "")
        self.ntfy_url = e("NTFY_URL", "https://ntfy.sh").rstrip("/")
        self.ntfy_topic = e("NTFY_TOPIC", "")
        self.db = e("STATE_DB", "/data/state.db")
        self.scan_interval = float(e("SCAN_INTERVAL_HOURS", "6"))
        self.ttl = float(e("TASTE_TTL_HOURS", "24"))
        self.lookback = int(e("MA_LOOKBACK_DAYS", "0"))
        self.lead_days = int(e("NOTIFY_LEAD_DAYS", "7"))
        self.score_min = float(e("SIMILAR_SCORE_MIN", "0.6"))
        self.consensus = int(e("SIMILAR_CONSENSUS", "2"))
        self.similar_limit = int(e("SIMILAR_LIMIT", "50"))
        # Concerts (ADR-0008, opt-in). Eventim's geo filter is city-based, not a radius,
        # so "near me" is a list of cities. Empty CONCERT_CITIES disables the feature.
        self.concert_cities = [c.strip() for c in e("CONCERT_CITIES", "").split(",") if c.strip()]
        self.concert_lookahead = int(e("CONCERT_LOOKAHEAD_DAYS", "180"))
        self.concert_max_pages = int(e("CONCERT_MAX_PAGES", "120"))
        self.eventim_webid = e("EVENTIM_WEB_ID", "web__eventim-de")
        self.eventim_lang = e("EVENTIM_LANGUAGE", "de")
        self.eventim_category = e("EVENTIM_CATEGORY", "Konzerte")
        self.user_agent = e("USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


CFG = Cfg()
SESSION = requests.Session()  # Last.fm + ntfy — plain requests is fine
SESSION.headers.update({"User-Agent": CFG.user_agent})
# ADR-0003: Metal Archives is Cloudflare-fronted and 403s plain requests even
# with a browser UA (verified). curl_cffi impersonates Chrome's TLS fingerprint.
MA_SESSION = cffi_requests.Session(impersonate="chrome")
# ADR-0008: Eventim's public search API is what eventim.de's frontend calls; a plain
# requests UA risks blocking, so ride the same Chrome TLS impersonation as MA. Separate
# session to keep cookies/throttle independent of the MA scrape.
EVENTIM_SESSION = cffi_requests.Session(impersonate="chrome")


def _redact(s):
    # Last.fm sends the api_key in the request URL, so it lands in HTTPError text.
    return s.replace(CFG.lastfm_key, "***") if CFG.lastfm_key else s


def log(msg):
    print(f"[{now_iso()}] {_redact(msg)}", flush=True)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------- normalize
def normalize(name):
    """ADR-0007: lowercase, strip diacritics + non-alphanumerics, drop leading 'the'."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def norm_set(names):
    # Drop empties: names in non-Latin scripts (Аркона, 陰陽座) normalize to "" and
    # would otherwise match every non-Latin band. (review #2)
    return {n for n in (normalize(x) for x in names) if n}


# ------------------------------------------------------------------ spotify
def _oauth():
    return SpotifyOAuth(
        client_id=CFG.spotify_id, client_secret=CFG.spotify_secret,
        redirect_uri=CFG.spotify_redirect, scope=SCOPES,
        cache_handler=CacheFileHandler(cache_path=CFG.spotify_cache),
        open_browser=False,
    )


def cmd_auth():
    oauth = _oauth()
    print("1) Open this URL, log in, and approve:\n")
    print("   " + oauth.get_authorize_url() + "\n")
    print("2) Your browser redirects to a page that won't load. Copy its FULL")
    print("   address-bar URL and paste it below.\n")
    redirect = input("Pasted redirect URL: ").strip()
    oauth.get_access_token(oauth.parse_response_code(redirect), as_dict=False)
    print(f"\nToken cached at {CFG.spotify_cache}. You can start the service now.")


def _spotify():
    # A headless service must never fall into spotipy's interactive prompt: with no
    # cached token, SpotifyOAuth(open_browser=False) prints the auth URL and blocks on
    # input(), which EOFErrors in a detached container. Pre-validate the cached token
    # (validate_token refreshes it in-place if it's merely expired) and raise
    # SpotifyOauthError when nothing usable is cached, so refresh_taste_if_stale nags
    # for re-auth and rides on cached taste instead of crash-looping.
    oauth = _oauth()
    if not oauth.validate_token(oauth.cache_handler.get_cached_token()):
        raise SpotifyOauthError("no cached Spotify token — run `python main.py auth`")
    # requests_timeout: spotipy defaults to no timeout, so a stalled socket would hang
    # the whole cycle forever. Bound it like every other HTTP call in this module.
    return spotipy.Spotify(auth_manager=oauth, requests_timeout=30)


def fetch_taste(sp):
    """Taste set = top ∪ playlist ∪ saved-album artists (CONTEXT.md)."""
    names = set()
    for tr in ("medium_term", "long_term"):
        for a in sp.current_user_top_artists(limit=50, time_range=tr)["items"]:
            names.add(a["name"])
    res = sp.current_user_saved_albums(limit=50)
    while res:
        for it in res["items"]:
            names.update(a["name"] for a in it["album"]["artists"])
        res = sp.next(res) if res.get("next") else None
    pls = sp.current_user_playlists(limit=50)
    while pls:
        for pl in pls["items"]:
            if not pl:  # Spotify occasionally returns null playlist entries (review #11)
                continue
            items = sp.playlist_items(pl["id"], limit=100,
                                      fields="items(track(artists(name))),next")
            while items:
                for it in items["items"]:
                    tr = it.get("track") if it else None
                    if tr and tr.get("artists"):
                        names.update(a["name"] for a in tr["artists"] if a.get("name"))
                items = sp.next(items) if items.get("next") else None
        pls = sp.next(pls) if pls.get("next") else None
    names.discard("")
    return sorted(names)


# ------------------------------------------------------------------- lastfm
class _LastfmTransient(Exception):
    """A Last.fm response worth retrying rather than aborting the whole refresh on."""


def _lastfm_similar(artist):
    r = SESSION.get("https://ws.audioscrobbler.com/2.0/", timeout=30, params={
        "method": "artist.getsimilar", "artist": artist, "api_key": CFG.lastfm_key,
        "format": "json", "limit": CFG.similar_limit, "autocorrect": 1})
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        # Last.fm intermittently answers 200 with an empty/non-JSON body (rate
        # pressure or a transient hiccup). Retrying beats killing a refresh that's
        # already made thousands of calls — one bad body must not black out the cycle.
        raise _LastfmTransient(f"non-JSON body ({len(r.content)} bytes)")
    if isinstance(data, dict) and data.get("error"):
        # Documented error envelope, e.g. {"error": 29, "message": "Rate Limit Exceeded"}.
        raise _LastfmTransient(f"api error {data['error']}: {data.get('message', '')}")
    sim = data.get("similarartists", {}).get("artist", [])
    if isinstance(sim, dict):  # Last.fm returns a bare object (not a list) for a single hit
        sim = [sim]
    out = []
    for a in sim:
        try:
            match = float(a.get("match") or 0)
        except (TypeError, ValueError):
            match = 0.0
        out.append((a["name"], match))
    return out


def _lastfm_similar_retry(artist, attempts=4):
    delay = 1.0
    for i in range(attempts):
        try:
            return _lastfm_similar(artist)
        except (_LastfmTransient, requests.RequestException):
            if i == attempts - 1:
                raise
            time.sleep(delay)  # back off; helps most when Last.fm is rate-limiting us
            delay *= 2


def fetch_similar(taste_names):
    cand, failed = {}, 0
    for artist in taste_names:
        try:
            pairs = _lastfm_similar_retry(artist)
        except Exception:
            # One artist that still fails after retries is skipped, not fatal: a partial
            # Similar set is far better than falling all the way back to (often empty)
            # cached taste and aborting the cycle.
            failed += 1
            continue
        for name, match in pairs:
            nn = normalize(name)
            if not nn:
                continue
            e = cand.setdefault(nn, {"name": name, "sources": set(), "score": 0.0})
            e["sources"].add(artist)
            e["score"] = max(e["score"], match)
        time.sleep(0.25)  # ponytail: gentle on Last.fm; drop if it's ever slow
    if failed:
        log(f"Last.fm: {failed}/{len(taste_names)} artists skipped after retries")
    return cand


def high_confidence(cand, taste_norm):
    """ADR-0002: keep a similar artist iff consensus≥N or match≥threshold, and not already taste."""
    out = {}
    for nn, e in cand.items():
        if nn in taste_norm:
            continue
        if len(e["sources"]) >= CFG.consensus or e["score"] >= CFG.score_min:
            out[nn] = {"name": e["name"], "sources": sorted(e["sources"])}
    return out


# ------------------------------------------------------- metal archives scan
_A_RE = re.compile(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_last_ma = [0.0]


def _text(frag):
    return html.unescape(_TAG_RE.sub("", frag or "")).strip()


def ma_get(url, params):
    # ADR-0003: curl_cffi (Chrome TLS impersonation) clears Cloudflare; ~1 req/s
    # throttle keeps us polite. A mid-run failure aborts the cycle and retries next
    # scan — the 6h cycle is our backoff (ADR-0003). If MA blocks this too, there is
    # no supported alternative for metal-specific release data — revisit the source.
    gap = time.monotonic() - _last_ma[0]
    if gap < 1.0:
        time.sleep(1.0 - gap)
    r = MA_SESSION.get(url, params=params, timeout=30)
    _last_ma[0] = time.monotonic()
    r.raise_for_status()
    return r


def parse_row(row):
    # aaData cols: band-anchor(s), album-anchor, type, genre, date, date-added.
    # Split releases carry MULTIPLE band anchors in col 0 — parse them all so a band
    # listed second on a split still matches (review #6; CONTEXT counts splits).
    try:
        bands = [html.unescape(t).strip() for _, t in _A_RE.findall(row[0])] or [_text(row[0])]
        bands = [b for b in bands if b]
        am = _A_RE.search(row[1])
        album_url = am.group(1) if am else ""
        if not bands or not album_url:
            return None
        return {
            "band": " / ".join(bands),
            "bands": bands,
            "album": html.unescape(am.group(2)).strip() if am else _text(row[1]),
            "url": album_url,
            "type": _text(row[2]) if len(row) > 2 else "",
            "genre": _text(row[3]) if len(row) > 3 else "",
            "date": _text(row[4]) if len(row) > 4 else "",
        }
    except Exception:
        return None


def scan_releases():
    # Window = [today - MA_LOOKBACK_DAYS, today + NOTIFY_LEAD_DAYS]. Default is
    # [today, today+7]: only releases dropping within the next week. MA_LOOKBACK_DAYS
    # (default 0) can be raised for a downtime safety margin. MA filters server-side,
    # so far-future releases aren't returned until they enter the window (verified).
    today = datetime.date.today()
    from_date = (today - datetime.timedelta(days=CFG.lookback)).isoformat()
    to_date = (today + datetime.timedelta(days=CFG.lead_days)).isoformat()
    releases, start, saw_rows = [], 0, False
    while True:
        j = ma_get("https://www.metal-archives.com/release/ajax-upcoming/json/1", {
            "sEcho": 1, "iDisplayStart": start, "iDisplayLength": 100,
            "fromDate": from_date, "toDate": to_date}).json()
        rows = j.get("aaData", [])
        saw_rows = saw_rows or bool(rows)
        releases += [r for r in (parse_row(x) for x in rows) if r]
        start += 100
        if not rows or start >= j.get("iTotalRecords", 0):
            break
    # review #4: rows came back but nothing parsed => markup changed. Raise so the
    # error path alerts instead of the service silently going dark.
    if saw_rows and not releases:
        raise RuntimeError("Metal Archives returned rows but none parsed — markup changed?")
    # review #9: a live feed can repeat a row across page boundaries; dedup by URL.
    seen, uniq = set(), []
    for r in releases:
        if r["url"] not in seen:
            seen.add(r["url"])
            uniq.append(r)
    return uniq


def match(releases, taste_norm, similar):
    known, disc = [], []
    for rel in releases:
        norms = [n for n in (normalize(b) for b in (rel.get("bands") or [rel.get("band", "")])) if n]
        if any(n in taste_norm for n in norms):
            known.append(rel)
        else:
            hit = next((n for n in norms if n in similar), None)
            if hit:
                disc.append(dict(rel, sources=similar[hit]["sources"]))
    return known, disc


# --------------------------------------------------------- eventim concert scan
# ADR-0008. Eventim mirrors the MA scan: pull the local concert feed (bounded by
# CONCERT_CITIES + a date window), then name-match performers against the same taste
# and similar sets. The API filters by city (no working radius); every performer is in
# attractions[], which is what we match — tribute acts ("SaD - Metallica Tribute")
# normalize to a distinct string and so never false-match a real band.
EVENTIM_URL = "https://public-api.eventim.com/websearch/search/api/exploration/v1/products"
_last_eventim = [0.0]


def eventim_get(params):
    # ~1 req/s throttle, same courtesy as the MA scrape.
    gap = time.monotonic() - _last_eventim[0]
    if gap < 1.0:
        time.sleep(1.0 - gap)
    r = EVENTIM_SESSION.get(EVENTIM_URL, params=params, timeout=30)
    _last_eventim[0] = time.monotonic()
    r.raise_for_status()
    return r


def parse_event(prod):
    # Pull the fields we notify on. attractions[].name are the performers (match keys);
    # `name` is the event title (display fallback when attractions is empty).
    try:
        le = (prod.get("typeAttributes") or {}).get("liveEntertainment") or {}
        loc = le.get("location") or {}
        acts = [html.unescape((a.get("name") or "")).strip()
                for a in (prod.get("attractions") or []) if a.get("name")]
        acts = [a for a in acts if a]
        pid = str(prod.get("productId") or "")
        url = prod.get("link") or ""
        if not pid or not url:
            return None
        city = (loc.get("city") or "").strip()
        date = (le.get("startDate") or "")[:10]  # YYYY-MM-DD
        # Identity is the SHOW, not the ticket product: Eventim sells one gig as several
        # productIds (GA / VIP / lineup variants), so dedup/seen key on date+city+headliner,
        # not productId — otherwise one concert fires N near-identical pushes. Normalized so
        # minor string drift doesn't resurrect a show. Falls back to productId when there's
        # no performer to key on (those never match a taste anyway). "eventim:" namespaces it
        # so a concert id never collides with a release URL in the shared `seen` table.
        primary = normalize(acts[0]) if acts else normalize(prod.get("name") or "")
        key = f"{date}|{normalize(city)}|{primary}" if primary else f"pid|{pid}"
        return {
            "id": "eventim:" + key,
            "acts": acts,
            "title": (prod.get("name") or "").strip(),
            "city": city,
            "venue": (loc.get("name") or "").strip(),
            "date": date,
            "url": url,
        }
    except Exception:
        return None


def scan_concerts():
    # Window = [today, today + CONCERT_LOOKAHEAD_DAYS] across CONCERT_CITIES (union).
    # Returns [] when disabled (no cities configured).
    #
    # Pagination is by DATE CURSOR, not page number: Eventim's `page` param is a verified
    # no-op (every page returns the same first 50) and `top` caps at 50. So we sort DateAsc
    # and advance `date_from` to the last date of each batch. Boundary-date events reappear
    # across batches; dedup by id absorbs the overlap.
    if not CFG.concert_cities:
        return []
    today = datetime.date.today()
    to_date = (today + datetime.timedelta(days=CFG.concert_lookahead)).isoformat()
    cities = ",".join(CFG.concert_cities)
    cursor, saw_rows = today.isoformat(), False
    by_id = {}
    for _ in range(CFG.concert_max_pages):
        j = eventim_get({
            "webId": CFG.eventim_webid, "language": CFG.eventim_lang,
            "page": 1, "top": 50, "sort": "DateAsc",             # 50 is the API's hard ceiling
            "categories": CFG.eventim_category, "city_names": cities,
            "date_from": cursor, "date_to": to_date}).json()
        prods = j.get("products", [])
        saw_rows = saw_rows or bool(prods)
        parsed = [e for e in (parse_event(p) for p in prods) if e]
        for e in parsed:
            prev = by_id.get(e["id"])
            if prev is None or len(e["acts"]) > len(prev["acts"]):
                by_id[e["id"]] = e   # collapse ticket-product variants; keep the fullest lineup
        if len(prods) < 50:
            break                                                # short batch => window exhausted
        dates = sorted(e["date"] for e in parsed if e["date"])
        last = dates[-1] if dates else ""
        if not last or last <= cursor:
            # A full batch all on one day (>50 shows) can't advance the cursor; step past it.
            # Truncates same-day shows beyond 50 — implausible for a normal city list, but log it.
            log(f"concerts: >50 shows dated {cursor}; some same-day shows may be skipped")
            cursor = (datetime.date.fromisoformat(cursor) + datetime.timedelta(days=1)).isoformat()
        else:
            cursor = last
        if cursor > to_date:
            break
    else:
        log(f"concerts: hit request cap ({CFG.concert_max_pages}); farthest-future shows skipped")
    # Mirror the MA guard: rows came back but nothing parsed => Eventim changed its shape.
    if saw_rows and not by_id:
        raise RuntimeError("Eventim returned products but none parsed — API shape changed?")
    return sorted(by_id.values(), key=lambda e: e["date"])


def match_concerts(events, taste_norm, similar):
    # Same known/discovery split as releases, keyed on performer names.
    known, disc = [], []
    for ev in events:
        norms = [n for n in (normalize(a) for a in ev.get("acts", [])) if n]
        if any(n in taste_norm for n in norms):
            known.append(ev)
        else:
            hit = next((n for n in norms if n in similar), None)
            if hit:
                disc.append(dict(ev, sources=similar[hit]["sources"]))
    return known, disc


# --------------------------------------------------------------------- state
def db():
    con = sqlite3.connect(CFG.db)
    con.execute("CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, ts TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    return con


def seen_has(con, rid):
    return con.execute("SELECT 1 FROM seen WHERE id=?", (rid,)).fetchone() is not None


def mark_seen(con, rid):
    con.execute("INSERT OR IGNORE INTO seen(id, ts) VALUES(?, ?)", (rid, now_iso()))


def kv_get(con, k):
    row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(row[0]) if row else None


def kv_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, json.dumps(v)))


def _age_hours(ts):
    try:
        t = datetime.datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600
    except Exception:
        return 1e9


# ---------------------------------------------------------------------- ntfy
def notify(title, message, url=None, tags=None):
    """Returns True on delivery. Callers only mark a release seen when this is True,
    so an ntfy outage retries next cycle instead of burying releases (review #1)."""
    payload = {"topic": CFG.ntfy_topic, "title": _redact(title), "message": _redact(message)}
    if url:
        payload["click"] = url
    if tags:
        payload["tags"] = tags
    try:
        requests.post(CFG.ntfy_url, json=payload, timeout=15).raise_for_status()
        return True
    except Exception as ex:
        log(f"ntfy push failed: {ex}")
        return False


def _meta(r):
    return " · ".join(p for p in (r["type"], r["genre"], r["date"]) if p)


def _concert_title(e):
    return f"{e['acts'][0] if e['acts'] else e['title']} live in {e['city']}"


def _concert_body(e):
    acts = ", ".join(e["acts"]) if e.get("acts") else e.get("title", "")
    where = " · ".join(p for p in (e.get("venue", ""), e.get("city", "")) if p)
    return " · ".join(p for p in (acts, where, e.get("date", "")) if p)


# --------------------------------------------------------------------- cycle
def _nag_reauth(con):
    # review #7: fire the re-auth nag once, not every 6h. Cleared on the next
    # successful refresh.
    if kv_get(con, "reauth_nagged"):
        return
    notify("Spotify re-auth needed",
           "Run:  docker compose run --rm notifier auth\n"
           "then restart. (Spotify refresh tokens expire ~every 6 months.)",
           tags=["warning", "key"])
    kv_set(con, "reauth_nagged", True)
    con.commit()


def refresh_taste_if_stale(con):
    ts = kv_get(con, "taste_ts")
    if ts and _age_hours(ts) < CFG.ttl and kv_get(con, "taste") is not None:
        return kv_get(con, "taste"), kv_get(con, "similar")
    cached_taste, cached_similar = kv_get(con, "taste"), kv_get(con, "similar") or {}
    try:
        log("refreshing taste: fetching Spotify library")
        taste = fetch_taste(_spotify())
        log(f"taste: {len(taste)} artists; querying Last.fm similar "
            f"(~{len(taste) * 0.25:.0f}s of throttling)")
        similar = high_confidence(fetch_similar(taste), norm_set(taste))
    except SpotifyOauthError:
        _nag_reauth(con)
        log("spotify token missing or expired; continuing on cached taste")
        return cached_taste or [], cached_similar
    except Exception as ex:
        # review #5: a transient Spotify/Last.fm failure shouldn't black out the MA
        # scan for a whole cycle — fall back to cached taste when we have it.
        if cached_taste is not None:
            log(f"taste refresh failed ({type(ex).__name__}: {ex}); using cached taste")
            return cached_taste, cached_similar
        log(f"taste refresh failed ({type(ex).__name__}: {ex}); no cached taste — aborting cycle")
        raise
    kv_set(con, "taste", taste)
    kv_set(con, "similar", similar)
    kv_set(con, "taste_ts", now_iso())
    kv_set(con, "reauth_nagged", False)
    con.commit()
    log(f"taste refreshed: {len(taste)} artists -> {len(similar)} high-confidence similar")
    return taste, similar


def run_concerts(con, taste_norm, similar):
    # Opt-in supplement to the release scan (ADR-0008). Self-contained: never raises,
    # so a flaky reverse-engineered API can't abort the (already-notified) release cycle
    # or leave release `seen` marks uncommitted. Failures alert once, deduped like the
    # main error path. Runs only when CONCERT_CITIES is set.
    if not CFG.concert_cities:
        return
    try:
        events = scan_concerts()
    except Exception as ex:
        msg = f"{type(ex).__name__}: {ex}"
        log(f"concert scan failed: {_redact(msg)}")
        if kv_get(con, "concert_last_error") != msg:
            notify("Concert scan error", _redact(msg)[:300], tags=["warning"])
            kv_set(con, "concert_last_error", msg)
            con.commit()
        return
    kv_set(con, "concert_last_error", "")
    known, disc = match_concerts(events, taste_norm, similar)
    new_known = [e for e in known if not seen_has(con, e["id"])]
    new_disc = [e for e in disc if not seen_has(con, e["id"])]

    if kv_get(con, "concerts_initialized") is None:
        # Separate seed gate from the release `initialized` flag: enabling concerts on an
        # existing install must seed silently once, not flood the whole lookback window.
        for e in known + disc:
            mark_seen(con, e["id"])
        kv_set(con, "concerts_initialized", True)
        notify("Concert tracking on",
               f"Watching {len(CFG.concert_cities)} cities "
               f"({', '.join(CFG.concert_cities[:6])}). "
               f"Seeded {len(new_known) + len(new_disc)} current shows; "
               f"you'll be pinged on new ones from here on.", tags=["stadium"])
        log(f"concerts: seeded {len(new_known) + len(new_disc)} shows (no spam)")
    else:
        for e in new_known:
            if notify(_concert_title(e), _concert_body(e), url=e["url"], tags=["stadium"]):
                mark_seen(con, e["id"])
        for e in new_disc:
            body = _concert_body(e) + f"\n🔍 similar to {', '.join(e['sources'][:4])}"
            if notify(_concert_title(e), body, url=e["url"], tags=["stadium", "mag"]):
                mark_seen(con, e["id"])
        log(f"concerts: {len(events)} shows -> {len(new_known)} new known, "
            f"{len(new_disc)} new discovery")
    con.commit()


def cycle(con):
    log("cycle start")
    taste, similar = refresh_taste_if_stale(con)
    taste_norm = norm_set(taste)
    releases = scan_releases()
    known, disc = match(releases, taste_norm, similar)

    new_known = [r for r in known if not seen_has(con, r["url"])]
    new_disc = [r for r in disc if not seen_has(con, r["url"])]

    if kv_get(con, "initialized") is None:
        # review #8: don't seed on an empty taste (e.g. first run with broken auth),
        # or the whole lookback window floods as "new" once auth is fixed.
        if not taste:
            log("first run but taste is empty (auth not done?) — not seeding yet")
            return
        for r in known + disc:
            mark_seen(con, r["url"])
        kv_set(con, "initialized", True)
        notify("Metal notifier started",
               f"Tracking {len(taste)} artists (+{len(similar)} similar acts). "
               f"Seeded {len(new_known) + len(new_disc)} current releases; "
               f"you'll be pinged on new ones from here on.", tags=["guitar"])
        log(f"first run: seeded {len(new_known) + len(new_disc)} releases (no spam)")
    else:
        for r in new_known:
            if notify(f"{r['band']} – {r['album']}", _meta(r), url=r["url"], tags=["guitar"]):
                mark_seen(con, r["url"])
        for r in new_disc:
            body = _meta(r) + f"\n🔍 similar to {', '.join(r['sources'][:4])}"
            if notify(f"{r['band']} – {r['album']}", body, url=r["url"], tags=["mag"]):
                mark_seen(con, r["url"])
        log(f"scan: {len(releases)} releases -> {len(new_known)} new known, "
            f"{len(new_disc)} new discovery")

    run_concerts(con, taste_norm, similar)

    kv_set(con, "last_error", "")
    con.commit()


def run_loop():
    con = db()
    while True:
        try:
            cycle(con)
        except Exception as ex:
            msg = _redact(f"{type(ex).__name__}: {ex}")
            log(f"cycle error: {msg}")
            if kv_get(con, "last_error") != msg:  # don't spam repeats
                notify("Metal notifier error", msg[:300], tags=["warning"])
                kv_set(con, "last_error", msg)
                con.commit()
        time.sleep(CFG.scan_interval * 3600)


def require_env():
    missing = [k for k, v in {
        "SPOTIFY_CLIENT_ID": CFG.spotify_id, "SPOTIFY_CLIENT_SECRET": CFG.spotify_secret,
        "LASTFM_API_KEY": CFG.lastfm_key, "NTFY_TOPIC": CFG.ntfy_topic}.items() if not v]
    if missing:
        sys.exit("Missing required env: " + ", ".join(missing) + " (see .env.example)")


# ------------------------------------------------------------------ selftest
def selftest():
    assert normalize("The Ökämp Band!") == "okampband", normalize("The Ökämp Band!")
    assert normalize("Mötley Crüe") == "motleycrue"
    assert norm_set(["Аркона", "Metallica", "The Who"]) == {"metallica", "who"}  # #2

    row = ['<a href="https://x/bands/Metallica/125">Metallica</a>',
           '<a href="https://x/albums/Metallica/72_Seasons/1052540">72 Seasons</a>',
           'Full-length', 'Thrash Metal', 'April 14th, 2023']
    p = parse_row(row)
    assert p["band"] == "Metallica" and p["bands"] == ["Metallica"] and p["album"] == "72 Seasons"
    assert p["url"].endswith("/1052540") and p["type"] == "Full-length"

    split = ['<a href="/b/A/1">Aaa</a> / <a href="/b/B/2">Bbb</a>',  # #6 split: two bands
             '<a href="/albums/split/9">Split LP</a>', 'Split', 'Black Metal', '2020']
    sp = parse_row(split)
    assert sp["bands"] == ["Aaa", "Bbb"] and sp["band"] == "Aaa / Bbb", sp

    cand = {
        "a": {"name": "A", "sources": {"x"}, "score": 0.7},           # score -> in
        "b": {"name": "B", "sources": {"x"}, "score": 0.3},           # weak    -> out
        "c": {"name": "C", "sources": {"x", "y"}, "score": 0.3},      # consensus -> in
        "known": {"name": "K", "sources": {"x", "y"}, "score": 0.9},  # already taste -> out
    }
    hc = high_confidence(cand, {"known"})
    assert set(hc) == {"a", "c"}, set(hc)
    assert hc["c"]["sources"] == ["x", "y"]

    taste_norm = {"metallica"}
    similar = {"slayer": {"name": "Slayer", "sources": ["Metallica"]}}
    rels = [{"bands": ["Metallica"], "album": "x", "url": "1"},
            {"bands": ["Slayer"], "album": "y", "url": "2"},
            {"bands": ["Nobody"], "album": "z", "url": "3"},
            {"bands": ["Foo", "Metallica"], "album": "split", "url": "4"},  # #6 2nd band known
            {"bands": ["Аркона"], "album": "cyr", "url": "5"}]              # #2 empty norm, no match
    known, disc = match(rels, taste_norm, similar)
    assert [r["url"] for r in known] == ["1", "4"], [r["url"] for r in known]
    assert [r["url"] for r in disc] == ["2"] and disc[0]["sources"] == ["Metallica"]

    # --- concerts (ADR-0008) ---
    prod = {"productId": 20949496, "name": "72 Seasons Tour",
            "attractions": [{"name": "Metallica"}, {"name": "Pantera"}],
            "link": "https://www.eventim.de/event/x-20949496/",
            "typeAttributes": {"liveEntertainment": {
                "startDate": "2026-08-01T20:00:00+02:00",
                "location": {"city": "Dortmund", "name": "Westfalenhallen"}}}}
    ev = parse_event(prod)
    assert ev["id"] == "eventim:2026-08-01|dortmund|metallica", ev["id"]
    assert ev["acts"] == ["Metallica", "Pantera"]
    assert ev["city"] == "Dortmund" and ev["venue"] == "Westfalenhallen" and ev["date"] == "2026-08-01"
    assert parse_event({"name": "no id"}) is None          # missing productId/link -> dropped
    assert _concert_title(ev) == "Metallica live in Dortmund"
    # Same show sold as two ticket products (different productId, different lineup depth)
    # must collapse to ONE id, so one gig can't fire multiple pushes.
    variant = dict(prod, productId=99999999, attractions=[{"name": "Metallica"}])
    assert parse_event(variant)["id"] == ev["id"], "ticket-product variants must share an id"

    events = [
        {"id": "a", "acts": ["Metallica"], "city": "X"},                  # known
        {"id": "b", "acts": ["Slayer"], "city": "Y"},                     # discovery
        {"id": "c", "acts": ["Nobody"], "city": "Z"},                     # no match
        {"id": "d", "acts": ["SaD - Metallica Tribute"], "city": "W"},    # tribute -> no false match
        {"id": "e", "acts": ["Foo", "Metallica"], "city": "V"}]           # 2nd act known
    ck, cd = match_concerts(events, taste_norm, similar)
    assert [e["id"] for e in ck] == ["a", "e"], [e["id"] for e in ck]
    assert [e["id"] for e in cd] == ["b"] and cd[0]["sources"] == ["Metallica"]
    print("selftest OK")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "auth":
        cmd_auth()
    elif cmd == "once":
        require_env()
        cycle(db())
    elif cmd == "selftest":
        selftest()
    elif cmd == "run":
        require_env()
        run_loop()
    else:
        sys.exit(f"unknown command: {cmd} (use run|once|auth|selftest)")
