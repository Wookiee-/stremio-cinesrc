"""CineSrc source via the local Node sidecar (sidecar/src/server.js).

The sidecar owns the whole challenge ritual (bootstrap, PoW, tokens,
r2 decrypt). This module only speaks its small JSON API:
  GET /api/catalog?id=&type=movie|tv&season=&episode= -> providers[]
  GET /api/stream/batch?...&providers=id1,id2 -> {providers: [{ok, url, ...}]}
    (single challenge host shared by all probes; falls back to
    GET /api/stream/provider on older sidecars)
  GET /api/stream/provider?...&provider= -> {ok, url, ...}

Any failure (sidecar down, provider errors) returns None so the addon
returns no streams.
"""
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

log = logging.getLogger("cinesrc")

SIDECAR_URL = os.getenv("CINESRC_URL", "http://127.0.0.1:8001").rstrip("/")
CINESRC_ENABLED = os.getenv("CINESRC_ENABLED", "1") == "1"
MAX_PROVIDERS_TRY = int(os.getenv("CINESRC_PROVIDERS", "12"))
MAX_PROBE_N = int(os.getenv("CINESRC_PROBE", "5"))
MAX_PROBE_WORKERS = int(os.getenv("CINESRC_WORKERS", "1"))
# Comma-separated region flags; only providers carrying one of these are
# probed (upstream flags look like ["us"], ["fr"], ["de"], ["mx"]).
# Providers with no flag info are kept so nothing breaks if flags go missing.
REGIONS = {f.strip().lower() for f in os.getenv("CINESRC_REGIONS", "us").split(",")
           if f.strip()}
# Providers with static URLs (measured: Nebula serves the same master for
# days) cache hits long; everyone else (signed/expiring URLs, e.g. Lisbon
# 404s ~1 min after resolve) caches short. Repeats then skip the sidecar.
STATIC_PROVIDERS = {p.strip().lower() for p in os.getenv("STATIC_PROVIDERS", "nebula").split(",")
                    if p.strip()}
STATIC_TTL = int(os.getenv("STATIC_CACHE_TTL", "21600"))
SHORT_TTL = int(os.getenv("SHORT_CACHE_TTL", "60"))
MAX_QUALITY = 1080  # cap (no 4K)


def _ttl_for(pid: str | None) -> int:
    return STATIC_TTL if (pid or "").lower() in STATIC_PROVIDERS else SHORT_TTL


def _region_ok(prov: dict) -> bool:
    flags = prov.get("flags") or []
    if not flags:
        return True
    return any(str(f).lower() in REGIONS for f in flags)


def _quality_label(w: int, h: int) -> int:
    for label, min_w, min_h in ((2160, 3800, 2000), (1080, 1900, 1000),
                                (720, 1270, 700), (480, 840, 460),
                                (360, 630, 340), (240, 420, 220)):
        if w >= min_w or h >= min_h:
            return label
    return h


def variant_exceeds_cap(attrs: str, cap: int = MAX_QUALITY) -> bool:
    """True if an EXT-X-STREAM-INF attribute line is above the quality cap."""
    m = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
    return bool(m) and _quality_label(int(m.group(1)), int(m.group(2))) > cap


class CinesrcExtractor:
    def __init__(self, timeout: float = 90.0):
        self.timeout = timeout
        self._pcache: dict = {}  # (key, provider_id) -> {data, exp}
        self._down_until = 0.0  # skip fast when sidecar is known-down
        self._ids: dict = {}  # imdb -> tmdb (never expires)

    def _get(self, path: str, params: dict):
        r = httpx.get(f"{SIDECAR_URL}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _tmdb_id(self, media_id: str, media_type: str) -> str:
        """CineSrc series lookups need numeric TMDB ids; map tt* via Cinemeta."""
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
        if not CINESRC_ENABLED or time.time() < self._down_until:
            return None
        key = (media_id, media_type, str(season), str(episode))
        try:
            out = self._resolve(key, media_id, media_type, season, episode)
        except Exception as e:
            log.warning("cinesrc resolve failed: %s", e)
            self._down_until = time.time() + 30
            return None
        if out and media_type == "tv":
            self._prefetch_next(media_id, season, episode)
        return out

    def _prefetch_next(self, media_id, season, episode) -> None:
        """Best-effort background warm of S:E+1 so bingeing feels instant."""
        try:
            nxt = int(episode or 1) + 1
        except (TypeError, ValueError):
            return
        key = (media_id, "tv", str(season), str(nxt))
        if any(k[0] == key for k in self._pcache):
            return
        import threading

        def _bg():
            try:
                self._resolve(key, media_id, "tv", season, nxt)
            except Exception:
                pass

        threading.Thread(target=_bg, daemon=True).start()

    def _expand(self, res: dict) -> dict | None:
        """Expand one batch/provider result into renditions (thread-safe)."""
        pid = res.get("id") or res.get("provider")
        if not res.get("ok") or not res.get("url"):
            return None
        rends = self._renditions(res["url"])
        if not rends:
            return None
        pname = res.get("name") or pid
        for r in rends:
            r["provider"] = pid
            r["provider_name"] = pname
        return {"provider": pid, "provider_name": pname,
                "source": res.get("source", "HLS"), "renditions": rends}

    def _probe(self, params: dict, prov: dict) -> dict | None:
        """Probe one provider via legacy endpoint (fallback path)."""
        pid = prov.get("id")
        try:
            res = self._get("/api/stream/provider", {**params, "provider": pid})
        except Exception as e:
            log.warning("cinesrc provider %s error: %s", pid, e)
            return None
        if prov.get("name") and not res.get("name"):
            res["name"] = prov.get("name")
        return self._expand(res)

    def _resolve(self, key, media_id, media_type, season, episode):
        lookup = self._tmdb_id(media_id, media_type)
        params = {"id": lookup, "type": media_type}
        if media_type == "tv":
            params.update({"season": str(season or 1), "episode": str(episode or 1)})
        ckey = ("cat", lookup, media_type)
        cat = self._pcache.get(ckey)
        if cat and cat["exp"] > time.time():
            cat = cat["data"]
        else:
            try:
                cat = self._get("/api/catalog", params)
            except Exception:
                self._down_until = time.time() + 60  # sidecar likely down
                return None
            self._pcache[ckey] = {"data": cat, "exp": time.time() + 120}
        providers = sorted(cat.get("providers", []), key=lambda p: -p.get("rank", 0))
        providers = [p for p in providers if _region_ok(p)]
        # CINESRC_PROVIDERS (12) = pool considered; CINESRC_PROBE (5) = how
        # many top-ranked get probed — first 5 that have the stream, no
        # fallbacks past them. Single batch call probes them in parallel
        # (one challenge host each), wall time ~= slowest probe. Fresh
        # per-provider cache hits are reused, so repeats are instant.
        todo = providers[:MAX_PROVIDERS_TRY][:MAX_PROBE_N]
        if not todo:
            return None
        now = time.time()
        found: dict[str, dict] = {}
        stale = []
        for prov in todo:
            pid = prov.get("id")
            hit = self._pcache.get((key, pid))
            if hit and hit["exp"] > now:
                found[pid] = hit["data"]
            elif pid:
                stale.append(prov)
        if stale:
            ids = ",".join(p.get("id") for p in stale if p.get("id"))
            try:
                batch = self._get("/api/stream/batch", {**params, "providers": ids})
                results = batch.get("providers", [])
            except Exception as e:
                log.warning("cinesrc batch failed, falling back to per-provider: %s", e)
                results = None
            if results is None:
                # Legacy sidecar without /api/stream/batch.
                workers = max(1, min(len(stale), MAX_PROBE_WORKERS))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futs = [pool.submit(self._probe, params, prov) for prov in stale]
                    for fut in futs:
                        try:
                            hit = fut.result()
                        except Exception as e:
                            log.warning("cinesrc probe failed: %s", e)
                            continue
                        if hit:
                            found[hit["provider"]] = hit
            else:
                workers = max(1, min(len(results), MAX_PROBE_WORKERS))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futs = [pool.submit(self._expand, res) for res in results]
                    for fut in futs:
                        try:
                            hit = fut.result()
                        except Exception as e:
                            log.warning("cinesrc expand failed: %s", e)
                            continue
                        if hit:
                            found[hit["provider"]] = hit
            for pid, hit in found.items():
                self._pcache[(key, pid)] = {"data": hit,
                                            "exp": now + _ttl_for(pid)}
        hits = [found[p.get("id")] for p in todo if p.get("id") in found]
        if not hits:
            return None
        flat = [r for h in hits for r in h["renditions"]]
        return {"provider": hits[0]["provider"],
                "provider_name": hits[0]["provider_name"],
                "providers": [{"provider": h["provider"],
                               "provider_name": h["provider_name"],
                               "source": h["source"]} for h in hits],
                "renditions": flat}

    def _renditions(self, master_url: str) -> list[dict]:
        """Fetch master playlist, expand variants (capped at MAX_QUALITY).

        Masters carrying separate EXT-X-MEDIA audio groups are returned as a
        single master entry: their variants are video-only by design, so
        handing out variant URLs would play silent. The player needs the
        master to join audio + video.
        """
        try:
            r = httpx.get(master_url, headers={"Referer": "https://cinesrc.st/",
                                               "User-Agent": "Mozilla/5.0"},
                          timeout=25, follow_redirects=True)
        except Exception:
            return []
        if r.status_code != 200 or "EXTM3U" not in r.text[:500]:
            return []
        out = []
        base = master_url.rsplit("/", 1)[0] + "/"
        chunks = re.findall(r"#EXT-X-STREAM-INF:([^\n]*)\n([^#\n][^\n]*)", r.text)
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
                        "url": uri.strip() if uri.strip().startswith("http")
                        else base + uri.strip()})
        seen, uniq = set(), []
        for v in sorted(out, key=lambda v: int(v["quality"][:-1]), reverse=True):
            if v["quality"] not in seen:
                seen.add(v["quality"])
                uniq.append(v)
        if not uniq:
            return []
        if re.search(r"#EXT-X-MEDIA:[^\n]*TYPE=AUDIO", r.text):
            top = uniq[0]
            return [{"quality": top["quality"],
                     "resolution": top.get("resolution", ""),
                     "width": top.get("width", 0),
                     "height": top.get("height", 0),
                     "mbps": top["mbps"],
                     "url": master_url,
                     "master": True}]
        return uniq
