#!/usr/bin/env python3
"""Metal-release notifier. See CONTEXT.md + docs/adr/ for the design.

Pipeline: Spotify taste -> Last.fm similar (high-confidence) -> scan Metal
Archives -> normalized-name match -> ntfy push, one per new release.

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
        self.spotify_redirect = e("SPOTIFY_REDIRECT_URI", "http://localhost:8888/callback")
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
        self.user_agent = e("USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


CFG = Cfg()
SESSION = requests.Session()  # Last.fm + ntfy — plain requests is fine
SESSION.headers.update({"User-Agent": CFG.user_agent})
# ADR-0003: Metal Archives is Cloudflare-fronted and 403s plain requests even
# with a browser UA (verified). curl_cffi impersonates Chrome's TLS fingerprint.
MA_SESSION = cffi_requests.Session(impersonate="chrome")


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
    return spotipy.Spotify(auth_manager=_oauth())


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
def _lastfm_similar(artist):
    r = SESSION.get("https://ws.audioscrobbler.com/2.0/", timeout=30, params={
        "method": "artist.getsimilar", "artist": artist, "api_key": CFG.lastfm_key,
        "format": "json", "limit": CFG.similar_limit, "autocorrect": 1})
    r.raise_for_status()
    out = []
    for a in r.json().get("similarartists", {}).get("artist", []):
        try:
            match = float(a.get("match") or 0)
        except (TypeError, ValueError):
            match = 0.0
        out.append((a["name"], match))
    return out


def fetch_similar(taste_names):
    cand = {}
    for artist in taste_names:
        for name, match in _lastfm_similar(artist):
            nn = normalize(name)
            if not nn:
                continue
            e = cand.setdefault(nn, {"name": name, "sources": set(), "score": 0.0})
            e["sources"].add(artist)
            e["score"] = max(e["score"], match)
        time.sleep(0.25)  # ponytail: gentle on Last.fm; drop if it's ever slow
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
        taste = fetch_taste(_spotify())
        similar = high_confidence(fetch_similar(taste), norm_set(taste))
    except SpotifyOauthError:
        _nag_reauth(con)
        log("spotify auth expired; continuing on cached taste")
        return cached_taste or [], cached_similar
    except Exception as ex:
        # review #5: a transient Spotify/Last.fm failure shouldn't black out the MA
        # scan for a whole cycle — fall back to cached taste when we have it.
        log(f"taste refresh failed ({type(ex).__name__}: {ex}); using cached taste")
        if cached_taste is not None:
            return cached_taste, cached_similar
        raise
    kv_set(con, "taste", taste)
    kv_set(con, "similar", similar)
    kv_set(con, "taste_ts", now_iso())
    kv_set(con, "reauth_nagged", False)
    con.commit()
    log(f"taste refreshed: {len(taste)} artists -> {len(similar)} high-confidence similar")
    return taste, similar


def cycle(con):
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
