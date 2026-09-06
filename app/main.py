"""Stremio addon (FastAPI) backed by CineSrc via the local Node sidecar.

Protocol:
  GET /manifest.json
  GET /stream/{movie|series}/{id}.json   (id = ttXXXXXXX or ttXXXXXXX:S:E, or tmdb:12345...)
  GET /extract?id=..&type=..            (debug: raw m3u8 JSON)
  GET /hls?url=..                       (playlist proxy + segment serving; see STREAM_MODE)
"""
import os
import re
import urllib.parse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response

import httpx

from .extractor_cinesrc import CinesrcExtractor, variant_exceeds_cap

# Reuse TCP/TLS connections for /hls — avoids a new handshake per 5MB chunk
# while still buffering the whole chunk (needed for correct append).
# http2 left off (needs `h2` extra, not in requirements) — keep-alive over
# HTTP/1.1 already gives the speedup.
_h_client = httpx.Client(
    timeout=httpx.Timeout(25, connect=10),
    follow_redirects=True,
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
)

ADDON_ID = os.getenv("ADDON_ID", "com.cinesrc.stremio")
ADDON_NAME = os.getenv("ADDON_NAME", "CineSrc")
ADDON_VERSION = os.getenv("ADDON_VERSION", "1.0.0")
# One switch for how video bytes reach the player (default: proxy).
#   proxy    - Stremio plays /hls URLs, VPS proxies every byte (works
#              everywhere, max VPS bandwidth).
#   redirect - /hls serves playlists itself (rewritten, correct content-type,
#              a few KB) and 307-redirects each segment/init file straight to
#              upstream (307 preserves Range for seeking). ~Zero VPS video
#              bytes, one tiny redirect per segment, works on all clients.
#   direct   - /hls serves playlists once with absolute upstream segment URLs
#              inside: after that the client never contacts the VPS again.
#              Some players reject the raw disguised segments (.jpg/.png/
#              .html served as image/jpeg) and buffer — fall back to redirect.
#   raw      - Stremio gets raw upstream m3u8 URLs + proxyHeaders (needs
#              Desktop/Android; broken on these providers: players stall after
#              the first segment). Kept for experiments only.
STREAM_MODE = os.getenv("STREAM_MODE", "proxy").strip().lower()
if STREAM_MODE not in ("proxy", "redirect", "direct", "raw"):
    STREAM_MODE = "proxy"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

MANIFEST = {
    "id": ADDON_ID,
    "version": ADDON_VERSION,
    "name": ADDON_NAME,
    "description": "CineSrc streams (15 providers, up to 1080p).",
    "logo": "https://cinesrc.st/favicon.ico",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt", "tmdb:"],
    "catalogs": [],
    "behaviorHints": {"configurable": False, "configurationRequired": False},
}

app = FastAPI(title=ADDON_NAME)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
cinesrc = CinesrcExtractor()

CINESRC_REFERER = "https://cinesrc.st/"


def parse_stremio_id(sid: str) -> tuple[str, str, str | None, str | None]:
    """'tt123[:S:E]' / 'tmdb:123[:S:E]' -> (lookup, movie|tv, season, episode)."""
    parts = urllib.parse.unquote(sid).split(":")
    if parts[0] == "tmdb":
        lookup, rest = parts[1] if len(parts) > 1 else "", parts[2:]
    else:
        lookup, rest = parts[0], parts[1:]
    if len(rest) >= 2:
        return lookup, "tv", rest[0], rest[1]
    return lookup, "movie", None, None


def proxy_url(base_url: str, url: str, referer: str | None = None,
              seg: bool = False) -> str:
    p = f"{base_url}/hls?url={urllib.parse.quote(url, safe='')}"
    if referer:
        p += f"&referer={urllib.parse.quote(referer, safe='')}"
    if seg:
        # media segment / init file: in mode redirect /hls answers 307 ->
        # upstream (video bytes bypass the VPS); otherwise plain proxy.
        # Playlists never set this (they must be rewritten).
        p += "&seg=1"
    return p


def to_cinesrc_streams(payload: dict, base_url: str) -> list:
    """CineSrc renditions as /hls URLs (modes proxy/redirect/direct), or raw
    upstream URLs with proxyHeaders in mode raw.

    Stremio list entry shows quality in `name` and server + resolution in
    `title`, e.g. name="CineSrc 1080p", title="ServerA • 1920x1080 • 4.7 Mbps".
    """
    streams = []
    for r in payload.get("renditions", []):
        q = r.get("quality") or ""
        server = r.get("provider_name") or r.get("provider") or payload.get(
            "provider_name") or payload.get("provider") or "CineSrc"
        reso = r.get("resolution") or ""
        mbps = r.get("mbps") or 0
        # name = quality (bold header in Stremio), title = server details
        name = f"CineSrc {q}".strip()
        bits = [server]
        if q:
            bits.append(q)
        if reso and reso != q:
            bits.append(reso)
        if mbps:
            bits.append(f"{mbps} Mbps")
        label = " • ".join(bits)
        if STREAM_MODE == "raw":
            s = {"url": r["url"],
                 "title": label + " (raw)",
                 "name": name,
                 "behaviorHints": {
                     "bingeGroup": f"cinesrc-{q}" if q else "cinesrc",
                     "notWebReady": True,
                     "proxyHeaders": {
                         "request": {
                             "Referer": CINESRC_REFERER,
                             "Origin": CINESRC_REFERER.rstrip("/"),
                             "User-Agent": UA,
                         }
                     },
                 }}
            streams.append(s)
            continue
        s = {"url": proxy_url(base_url, r["url"], CINESRC_REFERER),
             "title": label,
             "name": name,
             "behaviorHints": {"bingeGroup": f"cinesrc-{q}" if q else "cinesrc",
                               "notWebReady": False}}
        streams.append(s)
    return streams


@app.get("/manifest.json")
def manifest():
    return MANIFEST


@app.get("/")
def root():
    return RedirectResponse("/manifest.json")


@app.get("/stream/{ctype}/{sid:path}")
def stream(ctype: str, sid: str, request: Request):
    if sid.endswith(".json"):
        sid = sid[: -len(".json")]
    if ctype not in ("movie", "series"):
        return {"streams": []}
    lookup, mtype, season, episode = parse_stremio_id(sid)
    if ctype == "series":
        mtype, season, episode = "tv", season or "1", episode or "1"
    base = str(request.base_url).rstrip("/")
    cs = cinesrc.get_stream(lookup, mtype, season, episode)
    if cs:
        return {"streams": to_cinesrc_streams(cs, base)}
    return {"streams": []}


@app.get("/extract")
def extract(id: str, type: str = "movie", season: str | None = None,
            episode: str | None = None):
    payload = cinesrc.get_stream(id, type, season, episode)
    if not payload:
        return JSONResponse({"success": False, "error": "no stream found"},
                            status_code=404)
    return {"success": True, **payload}


@app.get("/health")
def health():
    return {"ok": True}


@app.api_route("/hls", methods=["GET", "HEAD"])
def hls_proxy(url: str, request: Request, referer: str | None = None,
              seg: str | None = None):
    """Playlist rewrite + segment serving. Preserves ?token= on relative links.

    Used in modes proxy/redirect/direct (mode raw bypasses /hls entirely).
    Answers HEAD (players like mpv probe with HEAD before GET).

    Requests flagged &seg=1 (media segments and EXT-X-MAP init files, marked
    at playlist-rewrite time) get a 307 redirect to upstream in mode
    redirect (307 preserves Range for seeking); in modes proxy/direct they
    are proxied as before (direct mode never emits seg flags for new
    playlists — only stale cached ones can still hit this path).
    """
    ref = referer or CINESRC_REFERER
    if request.method == "HEAD":
        # One HEAD path for every mode: mirror upstream's content headers
        # with zero bytes. Players probe with HEAD before GET; a bare 200
        # with content-length: 0 reads as an empty segment (black screen
        # while every GET returns 200), and a 307 on HEAD is not followed
        # by most players (reads as a dead segment). Segments also get the
        # neutral octet-stream type (see below); playlists keep mpegurl.
        try:
            h = _h_client.head(url,
                               headers={"Referer": ref, "User-Agent": UA},
                               timeout=15)
            headers = {}
            for k in ("content-length", "accept-ranges", "etag",
                      "last-modified"):
                if h.headers.get(k):
                    headers[k] = h.headers[k]
            # Match what GET serves: playlists as mpegurl (rewritten),
            # segments as neutral octet-stream (upstream lies: image/jpeg,
            # text/html). HEAD must agree with GET or players distrust the URL.
            ctype = ("application/octet-stream" if seg == "1"
                     else "application/vnd.apple.mpegurl")
            return Response(
                status_code=200 if h.status_code < 500 else h.status_code,
                headers=headers, media_type=ctype)
        except Exception:
            return Response(
                status_code=200,
                media_type="application/octet-stream" if seg == "1"
                else "application/vnd.apple.mpegurl")
    if seg == "1" and STREAM_MODE == "redirect":
        # 302 (not 307): identical zero-byte flow, but wider player support
        # for mid-stream redirect follows. Range seeking still works —
        # players re-issue Range to the follow-up URL.
        return RedirectResponse(url, status_code=302)
    try:
        r = _h_client.get(url, headers={"Referer": ref, "User-Agent": UA})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    body = r.text
    if "EXTM3U" in body[:500]:
        base = url.rsplit("/", 1)[0] + "/"

        def proxied_abs(u: str, is_seg: bool = False) -> str:
            absu = urllib.parse.urljoin(base, u)
            # keep token query when resolving relative variant/segment URLs
            if "token=" not in absu and "token=" in url:
                tk = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(url).query).get("token", [""])[0]
                if tk:
                    absu += ("&" if "?" in absu else "?") + "token=" + tk
            if is_seg and STREAM_MODE == "direct":
                # absolute upstream URL straight into the playlist: the
                # client never comes back to the VPS for this segment.
                return absu
            return proxy_url(str(request.base_url).rstrip("/"), absu,
                             referer if referer else None, seg=is_seg)

        out = []
        skip_uri = False  # drop over-cap variants (e.g. 4K) from masters
        expect_variant = False  # next URI is a variant playlist, not a segment
        for line in body.splitlines():
            s = line.strip()
            if skip_uri:
                if not s or s.startswith("#"):
                    out.append(s)
                    continue
                skip_uri = False
                expect_variant = False
                continue
            if s.startswith("#EXT-X-STREAM-INF"):
                if variant_exceeds_cap(s):
                    skip_uri = True
                    expect_variant = False
                    continue
                expect_variant = True
                out.append(s)
                continue
            if s and not s.startswith("#"):
                # Media segment (or variant playlist URI): variant playlists
                # stay proxied so they can be rewritten; segments get &seg=1
                # so they 307-redirect in mode redirect.
                s = proxied_abs(s, is_seg=not expect_variant)
                expect_variant = False
            elif s.startswith("#"):
                # rewrite URI="..." tag attributes (EXT-X-MAP init segment,
                # EXT-X-KEY, EXT-X-MEDIA) so they don't resolve to our root.
                # Only the MAP init file is a media byte range (redirect it);
                # KEY/MEDIA are tiny playlists/keys that stay proxied.
                is_map = s.startswith("#EXT-X-MAP")

                def _uri_repl(m, _base=base, _seg=is_map):
                    u = m.group(1)
                    if u.startswith("data:") or u.startswith("http"):
                        absu = u
                    else:
                        absu = urllib.parse.urljoin(_base, u)
                    return 'URI="' + proxied_abs(absu, is_seg=_seg) + '"'
                s = re.sub(r'URI="([^"]+)"', _uri_repl, s)
            out.append(s)
        return Response("\n".join(out),
                        media_type="application/vnd.apple.mpegurl")
    # Deliberately NOT passing upstream's content-type through: providers
    # disguise segments as image/jpeg / text/html, and some players refuse
    # to append those to the media pipeline (bytes fetched fine, black
    # screen). octet-stream is the neutral truth for init/segment/key bytes.
    return Response(r.content, media_type="application/octet-stream")
