# stremio-cinesrc (CineSrc, Python)

Fast Stremio addon that resolves **CineSrc** streams to playable `m3u8` URLs Stremio understands.

- Local Node sidecar (`sidecar/`) owns the provider challenge ritual; Python only speaks its small JSON API.
- Accepts Stremio Cinemeta IDs directly (`tt...`, `tt...:S:E`, `tmdb:...`) — no TMDB API key needed.

## How it resolves

1. Cinemeta `tt...` → TMDB id mapping (series need numeric ids)
2. Sidecar `GET /api/stream/batch` → master `m3u8` for all providers in one
   call (one catalog fetch, one challenge host per provider probed in
   parallel — wall time ~= slowest probe, not the sum)
3. Master playlist expanded into renditions (max 1080p, no 4K)

Stremio gets one entry per rendition per working server (e.g. name
`CineSrc 1080p`, title `VidCloud • 1080p • 1920x1080 • 4.7 Mbps`).
How video bytes flow is one switch, `STREAM_MODE` (default `proxy`):
Stremio plays `/hls` URLs and the VPS proxies playlists (rewritten, correct
content-type) plus every segment — works on all clients incl. Web and Nuvio,
at the cost of VPS video bandwidth. The other modes (`redirect`, `direct`,
`raw`) try to keep video bytes off the VPS, but these providers disguise
playlists/segments as `image/jpeg` + `.jpg/.png/.html`, so players stall or
buffer on them. `redirect` is closest (playlists local, 307 per segment) but
buffered in testing; `proxy` is the reliable default.

## Prerequisites

- Python 3.12+
- Node.js 20+ (for the sidecar)

Both processes must run: the sidecar (`127.0.0.1:8001`) resolves providers,
the addon (`127.0.0.1:7001`) serves Stremio. Without the sidecar, `/stream`
returns no results.

## Run locally (Windows)

Double-click `start.bat` — it installs deps on first run, then starts the
sidecar and the addon together in the one window (sidecar lines are prefixed
`[sidecar]`). Ctrl+C stops both.

Or from a terminal (same single-window mode):

```powershell
pip install -r requirements.txt      # first time only (prefer a venv)
npm ci --prefix sidecar --omit=dev   # first time only
python run.py --with-sidecar         # sidecar + addon in this window
```

Prefer two windows? Run them separately instead:

```powershell
# terminal 1 — sidecar (keep running)
$env:SIDECAR_PORT = "8001"
node sidecar/src/server.js

# terminal 2 — addon
python run.py                        # serves http://127.0.0.1:7001
```

Verify, then add to Stremio:

```powershell
curl http://127.0.0.1:7001/health        # -> {"ok": true}
curl http://127.0.0.1:7001/manifest.json # -> CineSrc manifest
```

Install in Stremio → Addons → paste `http://127.0.0.1:7001/manifest.json`.

## Deploy on a VPS (Docker)

One container runs both processes (sidecar on internal `8001`, addon on `7001`):

```sh
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:7001/health
docker compose logs -f
# or: docker build -t stremio-cinesrc . && docker run -d --name stremio-cinesrc -p 7001:7001 --restart unless-stopped stremio-cinesrc
```

To save VPS bandwidth you can try `STREAM_MODE=redirect` (playlists local,
307 per segment, ~zero video bytes), but note it buffered in testing while
`proxy` stays smooth:

```yaml
environment:
  - STREAM_MODE=redirect
```

Then put HTTPS in front (Stremio expects `https://` for non-local addons).
Easiest is Caddy — one-liner reverse proxy with automatic certificates:

```
your-domain.com {
    reverse_proxy 127.0.0.1:7001
}
```

Install the addon in Stremio with `https://your-domain.com/manifest.json`.

## Sources

| Source | Quality | How | Needs |
|---|---|---|---|
| CineSrc | True 1080p (~4.7 Mbps), 15 providers | Local Node sidecar (`sidecar/`) | Node.js 20+ |

CineSrc entries appear as `CineSrc 1080p` with details like
`VidCloud • 1080p • 1920x1080 • 4.7 Mbps` (server • quality • resolution •
bitrate). `CINESRC_PROVIDERS` controls how many servers are probed/listed
(default 12 ~= all `us`). If the sidecar isn't running, the addon returns no streams.

## Config

| Var | Default | Purpose |
|---|---|---|
| `PORT` | `7001` | Addon HTTP port |
| `SIDECAR_PORT` | `8001` | Sidecar HTTP port (must match the port in `CINESRC_URL`) |
| `WITH_SIDECAR` | `0` | `1` = `run.py` also starts the sidecar in the same window (same as `--with-sidecar`) |
| `CINESRC_URL` | `http://127.0.0.1:8001` | Where the addon reaches the sidecar |
| `STREAM_MODE` | `proxy` | `proxy` = VPS proxies every byte (reliable default, max bandwidth) · `redirect` / `direct` / `raw` = attempts to keep video bytes off the VPS; none play reliably on these providers (disguised segments, Referer gates, expiring URLs) — experiments only |
| `CINESRC_PROVIDERS` | `12` | How many servers to probe/list per title (`1` = fastest, first server only; `12` ~= all `us`) |
| `CINESRC_REGIONS` | `us` | Only probe servers flagged with these regions (comma-separated, e.g. `us,fr`); others are skipped entirely |
| `CINESRC_WORKERS` | `8` | Max concurrent rendition expansions (higher = faster scrape, more sidecar load) |
| `CINESRC_ENABLED` | `1` | `0` disables resolving (streams always empty) |
| `ADDON_ID` / `ADDON_NAME` / `ADDON_VERSION` | `com.cinesrc.stremio` / `CineSrc` / `1.0.0` | Manifest identity |

> Note: the default `proxy` mode carries all video traffic, so plan VPS
> bandwidth accordingly (~4.7 Mbps per viewer). Also, some providers dislike
> datacenter IPs — if resolves start failing on the VPS while working from
> home, that's why (the resolve still happens on the VPS in every mode).

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /manifest.json` | Stremio manifest |
| `GET /stream/movie/{id}.json` | Movie streams (`tt123`, `tmdb:123`) |
| `GET /stream/series/{id}.json` | Episode streams (`tt123:1:2`) |
| `GET /extract?id=..&type=movie\|tv&season=&episode=` | Raw resolver (debug) |
| `GET /hls?url=..` | Playlist proxy + segment serving (see `STREAM_MODE`) |
| `GET /health` | Health check |

## Notes

- Results are cached ~2 min; if the sidecar is down the addon backs off for 30-60s.

## Troubleshooting

- `/stream` returns `{"streams": []}` → sidecar isn't running or no provider
  worked. Check the sidecar window / `docker compose logs`, then
  `GET /extract?id=tt0111161&type=movie` for the raw error.
- No audio on a server → its variants are video-only with separate audio
  tracks; the addon hands Stremio the master playlist for those so audio
  joins correctly. If a server is still silent, report the server name +
  title (shown in each entry) so it can be checked.
- `EADDRINUSE` on startup → something already uses 7001 (addon) or 8001
  (sidecar). Change `PORT` / `SIDECAR_PORT` (keep `CINESRC_URL` in sync).
- Works at home but not on the VPS → provider blocking datacenter IPs.
  Try a residential host.
- Stremio won't install the URL → remote addons require `https://`; put
  Caddy/Nginx in front and use `https://your-domain.com/manifest.json`.
