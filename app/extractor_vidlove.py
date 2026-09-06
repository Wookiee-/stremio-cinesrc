"""VidLove source (111movies/vidlove player API) — pure Python, no sidecar.

Protocol (found in the vidlove.cc player bundle):
  GET https://api.tungtungtungtungsahur.app/movie?id={tmdb}&mode=json
  GET https://api.tungtungtungtungsahur.app/tv?id={tmdb}&season={s}&episode={e}&mode=json
with Referer/Origin https://vidlove.cc/ (bare requests get "forbidden").

Response JSON: {source: {label, url, manifest, ...}, subtitles: [...]}.
`manifest` is a clean master m3u8 (proper EXTM3U, H264+AAC renditions with
signed absolute variant URLs, tokens valid ~hours).

Caveats (measured):
- Variant playlists are proper mpegurl, but media segments are served as
  text/html and REQUIRE Referer (bare fetch returns a ~30KB decoy). So
  VidLove entries play via the /hls proxy like CineSrc — not raw.
- No JS challenge / PoW: a single JSON call resolves (much faster than the
  CineSrc sidecar ritual), which also makes this a good failover source.
"""
import logging
import os
import re
import time

import httpx

from .extractor_cinesrc import _quality_label

log = logging.getLogger("vidlove")

VIDLOVE_API = os.getenv("VIDLOVE_API",
                        "https://api.tungtungtungtungsahur.app").rstrip("/")
VIDLOVE_ORIGIN = os.getenv("VIDLOVE_ORIGIN", "https://vidlove.cc").rstrip("/")
VIDLOVE_ENABLED = os.getenv("VIDLOVE_ENABLED", "1") == "1"
MAX_QUALITY = 1080  # cap (no 4K)

HEADERS = {
    "Accept": "application/json",
    "Referer": VIDLOVE_ORIGIN + "/",
    "Origin": VIDLOVE_ORIGIN,
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
}


class VidloveExtractor:
    def __init__(self, timeout: float = 25.0):
        self.timeout = timeout
        self._cache: dict = {}
        self._ttl = 1800  # signed URLs live ~hours; 30 min keeps margin
        self._down_until = 0.0
        self._ids: dict = {}  # imdb -> tmdb (never expires)

    def _tmdb_id(self, media_id: str, media_type: str) -> str:
        if not media_id.startswith("tt"):
            return media_id
        if media_id in self._ids:
            return self._ids[media_id]
        kind = "series" if media_type == "tv" else "movie"
        try:
            m = httpx.get(f"https://v3-cinemeta.strem.io/meta/{kind}/{media_id}.json",
                          timeout=15).json()["meta"]
            tmdb = str(m.get("moviedb_id") or media_id)
        except Exception as e:
            log.warning("cinemeta mapping failed for %s: %s", media_id, e)
            tmdb = media_id
        self._ids[media_id] = tmdb
        return tmdb

    def get_stream(self, media_id: str, media_type: str = "movie",
                   season: str | int | None = None,
                   episode: str | int | None = None) -> dict | None:
        if not VIDLOVE_ENABLED or time.time() < self._down_until:
            return None
        key = (media_id, media_type, str(season), str(episode))
        hit = self._cache.get(key)
        if hit and hit["exp"] > time.time():
            return hit["data"]
        try:
            return self._resolve(key, media_id, media_type, season, episode)
        except Exception as e:
            log.warning("vidlove resolve failed: %s", e)
            self._down_until = time.time() + 30
            return None

    def _resolve(self, key, media_id, media_type, season, episode):
        lookup = self._tmdb_id(media_id, media_type)
        if media_type == "tv":
            params = {"id": lookup, "season": str(season or 1),
                      "episode": str(episode or 1), "mode": "json"}
            path = "/tv"
        else:
            params = {"id": lookup, "mode": "json"}
            path = "/movie"
        try:
            r = httpx.get(f"{VIDLOVE_API}{path}", params=params,
                          headers=HEADERS, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except Exception:
            self._down_until = time.time() + 60
            return None
        src = (data or {}).get("source") or {}
        manifest = src.get("manifest") or ""
        if "EXTM3U" not in manifest[:500]:
            return None
        rends = self._renditions(manifest)
        if not rends:
            return None
        for x in rends:
            x["provider"] = "vidlove"
            x["provider_name"] = "VidLove"
        payload = {"provider": "vidlove", "provider_name": "VidLove",
                   "source": "HLS", "renditions": rends}
        self._cache[key] = {"data": payload, "exp": time.time() + self._ttl}
        return payload

    def _renditions(self, manifest: str) -> list[dict]:
        """Parse the embedded master manifest (absolute signed URLs)."""
        out = []
        chunks = re.findall(r"#EXT-X-STREAM-INF:([^\n]*)\n([^#\n][^\n]*)", manifest)
        for attrs, uri in chunks:
            m = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
            if not m:
                continue
            q = _quality_label(int(m.group(1)), int(m.group(2)))
            if q > MAX_QUALITY:
                continue
            bw = re.search(r"BANDWIDTH=(\d+)", attrs)
            w, h = int(m.group(1)), int(m.group(2))
            out.append({"quality": f"{q}p",
                        "resolution": f"{w}x{h}",
                        "width": w,
                        "height": h,
                        "mbps": round(int(bw.group(1)) / 1e6, 1) if bw else 0,
                        "url": uri.strip()})
        seen, uniq = set(), []
        for v in sorted(out, key=lambda v: int(v["quality"][:-1]), reverse=True):
            if v["quality"] not in seen:
                seen.add(v["quality"])
                uniq.append(v)
        return uniq
