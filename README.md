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
Streams are direct: the addon returns upstream URLs with
`behaviorHints.proxyHeaders` (Referer + User-Agent), so video flows straight
from the host to the player — noone proxies video, no server bandwidth, 0 readahead.

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

VPS bandwidth: `raw` (default) uses zero video bytes; if Stremio stalls,
switch that host to `proxy`:

```yaml
environment:
  - STREAM_MODE=proxy  # Stremio fallback if direct buffers
```

Then put HTTPS in front (Stremio expects `https://` for non-local addons) — host nginx, not Docker:

```sh
# host nginx (like stremio-movy)
sudo cp nginx/cinesrc.conf /etc/nginx/sites-available/cinesrc.ddns.net
sudo ln -sf /etc/nginx/sites-available/cinesrc.ddns.net /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d cinesrc.ddns.net  # or: sudo ./setup-nginx.sh --email you@example.com
```

`nginx/cinesrc.conf` proxies `cinesrc.ddns.net` → `127.0.0.1:7001` (host → Docker, `STREAM_MODE=direct` is 0 video bandwidth). For manual setup:

```nginx
server {
    listen 80;
    server_name cinesrc.ddns.net;

    # video proxy: stream bytes straight through, don't buffer to disk
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_http_version 1.1;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:7001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```sh
nginx -t && systemctl reload nginx
certbot --nginx -d your-domain.com   # auto-renew via systemd timer
```

(`X-Forwarded-Proto` matters: `run.py` runs uvicorn with
`proxy_headers=True` so generated `/hls` URLs keep the public `https://`
scheme. Caddy alternative: `your-domain.com { reverse_proxy 127.0.0.1:7001 }`.)
```

Install the addon in Stremio with `https://your-domain.com/manifest.json`.

## Sources

| Source | Quality | How | Needs |
|---|---|---|---|
| CineSrc | True 1080p (~4.7 Mbps), 15 providers | Local Node sidecar (`sidecar/`) | Node.js 20+ |

CineSrc entries appear as `CineSrc 1080p` with details like
`VidCloud • 1080p • 1920x1080 • 4.7 Mbps` (server • quality • resolution •
bitrate). `CINESRC_PROVIDERS` controls how many top-ranked servers are
probed/listed per title (default 5 — first 5 that have the stream, no
fallbacks past them). If the sidecar isn't running, the addon returns no streams.

## Config

| Var | Default | Purpose |
|---|---|---|
| `PORT` | `7001` | Addon HTTP port |
| `SIDECAR_PORT` | `8001` | Sidecar HTTP port (must match the port in `CINESRC_URL`) |
| `WITH_SIDECAR` | `0` | `1` = `run.py` also starts the sidecar in the same window (same as `--with-sidecar`) |
| `CINESRC_URL` | `http://127.0.0.1:8001` | Where the addon reaches the sidecar |
| `STREAM_MODE` | `direct` | `direct` = upstream URLs + proxyHeaders, zero VPS bytes, 0 readahead (verified in Nuvio) · `proxy` = VPS proxies every byte (Stremio fallback if direct stalls) · `redirect` = also zero-byte via 302, extra hop |
| `CINESRC_PROVIDERS` | `5` | Top-ranked servers probed per title (first 5 that have the stream, no fallbacks) |
| `CINESRC_REGIONS` | `us` | Only probe servers flagged with these regions (comma-separated, e.g. `us,fr`); others are skipped entirely |
| `CINESRC_WORKERS` | `8` | Max concurrent rendition expansions (higher = faster scrape, more sidecar load) |
| `STATIC_PROVIDERS` | `nebula` | Providers with static URLs, cached for `STATIC_CACHE_TTL` (repeats instant, sidecar untouched) |
| `STATIC_CACHE_TTL` | `21600` | Cache seconds for static providers (6h) |
| `SHORT_CACHE_TTL` | `60` | Cache seconds for everyone else (signed URLs go stale fast) |
| `CINESRC_ENABLED` | `1` | `0` disables resolving (streams always empty) |
| `ADDON_ID` / `ADDON_NAME` / `ADDON_VERSION` | `com.cinesrc.stremio` / `CineSrc` / `1.0.0` | Manifest identity |

> Note: `proxy` carries all video traffic (~4.7 Mbps per viewer); `raw`/`redirect`/`direct` are zero-byte. Also, some providers dislike datacenter IPs — if resolves start failing on the VPS while working from home, that's why (the resolve still happens on the VPS in every mode).

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
