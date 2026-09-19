# IPTV HLS Reverse Proxy

A lightweight, dependency-free reverse proxy for IPTV HLS streams.

## Features

- **Streaming proxy** — pipes upstream → client without full buffer wait
- **HTTP/1.1 keep-alive** — persistent connections for better performance
- **HEAD support** — player probing works correctly
- **m3u8 URL rewriting** — automatically rewrites relative URLs to use the proxy
- **TTL caching** — caches m3u8 playlists with configurable TTL
- **Redirect handling** — follows 302 redirects from upstream CDNs
- **Zero third-party dependencies** — Python standard library only

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────┐
│   IPTV Player │────▶│  iptv-proxy      │────▶│ Upstream CDN │
│  (local LAN)  │◀────│  :8830           │◀────│              │
└──────────────┘     └──────────────────┘     └─────────────┘
                         │
                    /stream/* URLs
                    Rewrites m3u8
                    Caches playlists
```

## Quick Start

### Using Docker Compose (Recommended)

```bash
# 1. Clone and configure
git clone <repo-url>
cd iptvs-app
cp .env.example .env
# Edit .env with your EXTERNAL_BASE_URL

# 2. Build and run
docker compose up -d --build

# 3. Verify
curl http://localhost:8830/cache/stats
```

### Direct Python

```bash
export EXTERNAL_BASE_URL="https://your-domain.com/stream"
export PROXY_PORT=8830
python3 iptv_proxy.py
```

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `EXTERNAL_BASE_URL` | Base URL for m3u8 ts URL rewriting | *(required)* |
| `PROXY_PORT` | Port to listen on | `8830` |

## API Endpoints

- `GET /stream/<encoded-upstream-url>` — Proxy a stream request
- `HEAD /stream/<encoded-upstream-url>` — Probe a stream (HEAD only)
- `GET /cache/stats` — Return cache hit/miss statistics

## Usage with M3U Files

When using this proxy, replace direct upstream URLs in your M3U playlist with:

```
http://localhost:8830/stream/<url-encoded-upstream>
```

The proxy will automatically rewrite any relative URLs found in `.m3u8` playlists to also go through itself.

## License

MIT


## Well-Known Public Sources (Examples)

These are popular public IPTV sources you can use with this proxy:

| Source | URL | Description |
|--------|-----|-------------|
| **iptvs.pes.im** | `https://iptvs.pes.im` | IPTV channel list aggregation |
| **EPG (zsdc.eu.org)** | `https://epg.zsdc.eu.org/t.xml` | Electronic Program Guide XML |
| **Logo Base (Jarrey)** | `https://ghfast.top/https://raw.githubusercontent.com/Jarrey/iptv_logo/main/tv/` | Channel logo CDN via ghfast.top proxy |

### Example M3U Entry with Proxy

```m3u
#EXTINF:-1 tvg-id="CCTV1" tvg-logo="https://ghfast.top/https://raw.githubusercontent.com/Jarrey/iptv_logo/main/tv/CCTV1.png",CCTV-1 综合
http://your-proxy-host:8830/stream/https://example-cdn.com/live/cctv1.m3u8
```

### Using EPG with TVBox/OTT Player

In your player config, set:
- **EPG URL**: `https://epg.zsdc.eu.org/t.xml`
- **Logo Base**: `https://ghfast.top/https://raw.githubusercontent.com/Jarrey/iptv_logo/main/tv/`

These are public community-maintained sources — no authentication required.
