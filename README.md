# IPTV HLS Reverse Proxy / IPTV HLS 反向代理

A lightweight, dependency-free reverse proxy for IPTV HLS streams.  
轻量级、零依赖的 IPTV HLS 流反向代理。

---

## Features / 功能特性

- **Streaming proxy** — pipes upstream → client without full buffer wait  
  **流式代理** — 直接转发上游到客户端，无需完整缓冲
- **HTTP/1.1 keep-alive** — persistent connections for better performance  
  **HTTP/1.1 长连接** — 复用连接提升性能
- **HEAD support** — player probing works correctly  
  **HEAD 支持** — 播放器探测正常工作
- **m3u8 URL rewriting** — automatically rewrites relative URLs to use the proxy  
  **m3u8 URL 重写** — 自动将相对路径重写为代理地址
- **TTL caching** — caches m3u8 playlists with configurable TTL  
  **TTL 缓存** — 可配置 TTL 的播放列表缓存
- **Redirect handling** — follows 302 redirects from upstream CDNs  
  **重定向处理** — 自动跟随上游 CDN 的 302 跳转
- **Zero third-party dependencies** — Python standard library only  
  **零第三方依赖** — 仅使用 Python 标准库

---

## Architecture / 架构

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

---

## Quick Start / 快速开始

### Using Docker Compose (Recommended) / 使用 Docker Compose（推荐）

```bash
# 1. Clone and configure / 克隆并配置
git clone https://github.com/sure155/iptvs-app
cd iptvs-app
cp .env.example .env
# Edit .env with your EXTERNAL_BASE_URL / 编辑 .env 填入你的 EXTERNAL_BASE_URL

# 2. Run / 运行
docker compose up -d

# 3. Verify / 验证
curl http://localhost:8830/cache/stats
```

### Using Docker Directly / 直接使用 Docker

```bash
# Build image / 构建镜像
docker build -t iptvs-app .

# Run container / 运行容器
docker run -d   --name iptv-proxy   --restart unless-stopped   -p 8830:8830   -e EXTERNAL_BASE_URL="https://your-domain.com/stream"   iptvs-app

# Check logs / 查看日志
docker logs -f iptv-proxy
```

### Direct Python / 直接运行 Python

```bash
export EXTERNAL_BASE_URL="https://your-domain.com/stream"
export PROXY_PORT=8830
python3 iptv_proxy.py
```

---

## Configuration / 配置

| Variable / 变量 | Description / 说明 | Default / 默认值 |
|------------------|---------------------|-------------------|
| `EXTERNAL_BASE_URL` | Base URL for m3u8 ts URL rewriting / m3u8 ts URL 重写基础地址 | *(required / 必填)* |
| `PROXY_PORT` | Port to listen on / 监听端口 | `8830` |

### Environment File / 环境变量文件

Copy `.env.example` to `.env` and edit:

```bash
cp .env.example .env
# Edit .env / 编辑 .env
```

Example `.env`:
```env
EXTERNAL_BASE_URL=https://your-domain.com/stream
PROXY_PORT=8830
```

---

## API Endpoints / API 端点

- `GET /stream/<encoded-upstream-url>` — Proxy a stream request / 代理流请求
- `HEAD /stream/<encoded-upstream-url>` — Probe a stream (HEAD only) / 探测流（仅 HEAD）
- `GET /cache/stats` — Return cache hit/miss statistics / 返回缓存命中/未命中统计

---

## Usage with M3U Files / 配合 M3U 文件使用

When using this proxy, replace direct upstream URLs in your M3U playlist with:  
使用代理时，将 M3U 播放列表中的直连上游 URL 替换为：

```
http://localhost:8830/stream/<url-encoded-upstream>
```

The proxy will automatically rewrite any relative URLs found in `.m3u8` playlists to also go through itself.  
代理会自动将 `.m3u8` 播放列表中的相对路径也重写为代理地址。

---

## Well-Known Public Sources (Examples) / 常用公开源（示例）

These are popular public IPTV sources you can use with this proxy:  
以下是可搭配本代理使用的常用公开源：

| Source / 源 | URL | Description / 说明 |
|-------------|-----|-------------------|
| **iptvs.pes.im** | `https://iptvs.pes.im` | IPTV channel list aggregation / 频道列表聚合 |
| **EPG (zsdc.eu.org)** | `https://epg.zsdc.eu.org/t.xml` | Electronic Program Guide XML / 电子节目单 XML |
| **Logo Base (Jarrey)** | `https://ghfast.top/https://raw.githubusercontent.com/Jarrey/iptv_logo/main/tv/` | Channel logo CDN via ghfast.top proxy / 经 ghfast.top 代理的频道 Logo CDN |

### Example M3U Entry with Proxy / 代理模式 M3U 示例

```m3u
#EXTINF:-1 tvg-id="CCTV1" tvg-logo="https://ghfast.top/https://raw.githubusercontent.com/Jarrey/iptv_logo/main/tv/CCTV1.png",CCTV-1 综合
http://your-proxy-host:8830/stream/https://example-cdn.com/live/cctv1.m3u8
```

### Using EPG with TVBox/OTT Player / 在 TVBox/OTT 播放器中使用 EPG

In your player config, set:  
在播放器配置中设置：

- **EPG URL**: `https://epg.zsdc.eu.org/t.xml`
- **Logo Base**: `https://ghfast.top/https://raw.githubusercontent.com/Jarrey/iptv_logo/main/tv/`

These are public community-maintained sources — no authentication required.  
这些是社区维护的公开源 —— 无需认证。

---

## Project Structure / 项目结构

```
iptvs-app/
├── iptv_proxy.py          # Core proxy script / 核心代理脚本
├── Dockerfile             # Docker image definition / Docker 镜像定义
├── docker-compose.yml     # Compose deployment / Compose 部署
├── .env.example           # Environment template / 环境变量模板
├── config.example.json    # Config reference / 配置参考
├── requirements.txt       # Dependencies (empty - stdlib only) / 依赖（空 - 仅标准库）
├── .gitignore             # Git ignore rules / Git 忽略规则
└── README.md              # This file / 本文件
```

---

## License / 许可证

MIT
