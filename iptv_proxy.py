#!/usr/bin/env python3
"""
IPTV HLS Reverse Proxy — streaming, HTTP/1.1, HEAD support

A lightweight reverse proxy for IPTV HLS streams that:
- Streams upstream → client (no full buffer wait)
- Supports HEAD requests for player probing
- Rewrites m3u8 URLs to use the proxy endpoint
- Caches m3u8 playlists with TTL
- Handles 302 redirects from upstream CDNs

Usage:
    python3 iptv_proxy.py [--port PORT]

Environment variables:
    EXTERNAL_BASE_URL  Base URL for rewriting m3u8 ts URLs
    PROXY_PORT         Port to listen on (default: 8830)
"""

import http.server
import http.client
import os
import urllib.request
import urllib.parse
import re
import logging
import ssl
import threading
import time
from collections import OrderedDict

# Simple .env loader (no external dependency)
def load_env():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    os.environ.setdefault(key.strip(), val.strip())

load_env()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('iptv-proxy')

PORT = int(os.environ.get('PROXY_PORT', '8830'))
EXTERNAL_BASE_URL = os.environ.get('EXTERNAL_BASE_URL', '')
M3U8_TIMEOUT = 15
TS_TIMEOUT = 20
TS_MIN_SIZE = 10240  # 10KB — minimum valid ts size
CHUNK_SIZE = 65536  # 64KB streaming chunks
PROTOCOL_VERSION = "HTTP/1.1"

# ============================================================
# TTL Cache (for m3u8 only — ts is streamed, not cached)
# ============================================================

class TTLCache:
    """Thread-safe TTL cache using OrderedDict."""
    
    def __init__(self, name, maxsize=200, default_ttl=2):
        self.name = name
        self._maxsize = maxsize
        self._default_ttl = default_ttl
        self._data = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return None, None, None, 'MISS'
            body, ct, hdrs, ts, ttl = entry
            if time.time() - ts > ttl:
                self._misses += 1
                return body, ct, hdrs, 'STALE'
            self._hits += 1
            return body, ct, hdrs, 'HIT'

    def put(self, key, body, ct, hdrs=None, ttl=None):
        with self._lock:
            if ttl is None:
                ttl = self._default_ttl
            self._data[key] = (body, ct, hdrs, time.time(), ttl)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def stats(self):
        total = self._hits + self._misses
        rate = (self._hits / total * 100) if total > 0 else 0.0
        return f"size={len(self._data)}/{self._maxsize} hits={self._hits} misses={self._misses} rate={rate:.1f}%"


m3u8_cache = TTLCache('m3u8', maxsize=200, default_ttl=2)
redirect_cache = TTLCache('302', maxsize=100, default_ttl=90)

# ============================================================
# SSL context for HTTPS upstream connections
# ============================================================

def create_ssl_context():
    """Create a secure SSL context for upstream connections."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx

SSL_CTX = create_ssl_context()

# ============================================================
# HTTP Request helpers
# ============================================================

def fetch_url(url, timeout, headers=None, follow_redirects=True, method='GET'):
    """Fetch a URL with optional redirect following and timeout."""
    if headers is None:
        headers = {}
    
    retries = 0
    max_retries = 3
    
    while retries <= max_retries:
        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            resp = urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)
            
            if follow_redirects and resp.status == 302:
                location = resp.headers.get('Location', '')
                if location:
                    # Cache the redirect
                    redirect_cache.put(url, location, 'text/plain', {'location': location}, 90)
                    url = urllib.parse.urljoin(url, location)
                    retries += 1
                    continue
            
            return resp
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            logger.warning(f"Fetch failed for {url}: {e}")
            retries += 1
            if retries > max_retries:
                raise
            time.sleep(0.5)

def rewrite_m3u8(content, base_url):
    """Rewrite relative URLs in an m3u8 playlist to use the proxy."""
    if not EXTERNAL_BASE_URL:
        return content
    
    lines = content.split('\n')
    rewritten = []
    
    for line in lines:
        stripped = line.strip()
        
        # Skip comments, EXT* tags, empty lines
        if not stripped or stripped.startswith('#') or not stripped:
            rewritten.append(line)
            continue
        
        # This is a media URL — rewrite it
        if stripped.startswith('http://') or stripped.startswith('https://'):
            # Encode the FULL upstream URL for the proxy to reconstruct
            full_url = urllib.parse.quote(stripped, safe='')
            base = EXTERNAL_BASE_URL.rstrip('/')
            if base.endswith('/stream'):
                rewritten_line = f"{base}/{full_url}"
            else:
                rewritten_line = f"{base}/stream/{full_url}"
            rewritten.append(rewritten_line)
        elif stripped.startswith('/'):
            # Absolute path - join with base URL and encode full URL
            full_url = urllib.parse.urljoin(base_url, stripped)
            full_url_enc = urllib.parse.quote(full_url, safe='')
            base = EXTERNAL_BASE_URL.rstrip('/')
            if base.endswith('/stream'):
                rewritten.append(f"{base}/{full_url_enc}")
            else:
                rewritten.append(f"{base}/stream/{full_url_enc}")
        else:
            # Relative URL
            base = urllib.parse.urljoin(base_url, stripped)
            full_url_enc = urllib.parse.quote(base, safe='')
            base = EXTERNAL_BASE_URL.rstrip('/')
            if base.endswith('/stream'):
                rewritten.append(f"{base}/{full_url_enc}")
            else:
                rewritten.append(f"{base}/stream/{full_url_enc}")
    
    return '\n'.join(rewritten)

# ============================================================
# Stream Handler
# ============================================================


# ============================================================
# Simple Web UI for configuration
# ============================================================

UI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IPTV Proxy 控制面板</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#f5f5f5; margin:0; padding:2rem; }
        .container { max-width:700px; margin:0 auto; background:#fff; border-radius:12px; padding:2rem; box-shadow:0 2px 8px rgba(0,0,0,0.1); }
        h1 { color:#333; margin-bottom:1.5rem; }
        .form-group { margin-bottom:1rem; }
        label { display:block; margin-bottom:0.5rem; font-weight:600; color:#444; }
        input[type=text], input[type=number] { width:100%; padding:0.75rem; border:1px solid #ddd; border-radius:6px; font-size:1rem; box-sizing:border-box; }
        button { background:#4ecdc4; color:#fff; border:none; padding:0.75rem 1.5rem; border-radius:6px; font-size:1rem; cursor:pointer; }
        button:hover { background:#3ab8ad; }
        .status { padding:1rem; border-radius:6px; margin-bottom:1rem; display:none; }
        .status.success { background:#e8f5e9; color:#2e7d32; display:block; }
        .status.error { background:#fdecea; color:#c62828; display:block; }
        .info { background:#e3f2fd; padding:1rem; border-radius:6px; margin-bottom:1.5rem; font-size:0.9rem; color:#1565c0; }
        .endpoint { font-family:monospace; background:#f5f5f5; padding:0.2rem 0.4rem; border-radius:4px; }
    </style>
</head>
<body>
<div class="container">
    <h1>🎛️ IPTV HLS Proxy 控制面板</h1>
    <div class="info">
        <strong>代理地址：</strong> <span class="endpoint" id="proxyUrl"></span><br>
        <strong>用法：</strong> <code>http://<host>:<port>/stream/<url编码的上游m3u8></code>
    </div>
    <div id="status" class="status"></div>
    <form id="configForm">
        <div class="form-group">
            <label for="externalBaseUrl">EXTERNAL_BASE_URL (必填)</label>
            <input type="text" id="externalBaseUrl" name="externalBaseUrl" placeholder="http://192.168.1.150:8830/stream" required>
        </div>
        <div class="form-group">
            <label for="proxyPort">代理端口 PROXY_PORT</label>
            <input type="number" id="proxyPort" name="proxyPort" value="8830" min="1" max="65535">
        </div>
        <button type="submit">保存配置并重启提示</button>
    </form>
    <hr style="margin:2rem 0;">
    <h3>📊 运行状态</h3>
    <pre id="stats" style="background:#fafafa;padding:1rem;border-radius:6px;overflow:auto;">加载中...</pre>
</div>
<script>
    const proxyUrl = window.location.origin.replace(/:\d+$/, ':8830'); // fallback
    document.getElementById('proxyUrl').textContent = proxyUrl + '/stream/...';

    async function loadConfig() {
        try {
            const r = await fetch('/api/config');
            const cfg = await r.json();
            document.getElementById('externalBaseUrl').value = cfg.externalBaseUrl || '';
            document.getElementById('proxyPort').value = cfg.port || 8830;
        } catch(e) { console.error(e); }
    }
    async function loadStats() {
        try {
            const r = await fetch('/cache/stats');
            const s = await r.json();
            document.getElementById('stats').textContent = JSON.stringify(s, null, 2);
        } catch(e) { document.getElementById('stats').textContent = '无法获取'; }
    }
    document.getElementById('configForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const status = document.getElementById('status');
        status.className = 'status';
        status.textContent = '';
        const data = {
            externalBaseUrl: document.getElementById('externalBaseUrl').value.trim(),
            port: parseInt(document.getElementById('proxyPort').value, 10)
        };
        try {
            const r = await fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            const resp = await r.json();
            if (r.ok) {
                status.className = 'status success';
                status.textContent = '✅ 配置已写入 .env 文件。请重启容器以生效（docker restart 容器名）。';
            } else {
                status.className = 'status error';
                status.textContent = '❌ ' + (resp.error || '保存失败');
            }
        } catch(err) {
            status.className = 'status error';
            status.textContent = '❌ 请求失败: ' + err;
        }
    });
    loadConfig();
    loadStats();
    setInterval(loadStats, 10000);
</script>
</body>
</html>
"""

def write_env_file(external_base_url, port):
    """Write .env file in project directory."""
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    with open(env_path, 'w') as f:
        f.write(f'EXTERNAL_BASE_URL={external_base_url}\n')
        f.write(f'PROXY_PORT={port}\n')
    return env_path

class IPTVProxyHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for IPTV HLS stream proxying."""
    
    def log_message(self, format, *args):
        """Override to use our logger."""
        logger.info(f"{self.client_address[0]} - {format % args}")
    
    def do_HEAD(self):
        """Handle HEAD requests — useful for player probing."""
        path = self.path
        
        if path.startswith('/stream/'):
            upstream_url = urllib.parse.unquote(path[len('/stream/'):])
            self._handle_stream(upstream_url, head_only=True)
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        """Handle POST requests — config updates."""
        import json
        path = self.path
        if path == '/api/config':
            content_length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(content_length).decode('utf-8')
            try:
                data = json.loads(raw)
                external_base_url = data.get('externalBaseUrl', '').strip()
                port = int(data.get('port', PORT))
                if not external_base_url:
                    self._send_json({'error': 'EXTERNAL_BASE_URL 不能为空'}, status=400)
                    return
                write_env_file(external_base_url, port)
                self._send_json({'message': '配置已写入 .env，请重启容器生效'})
            except Exception as e:
                self._send_json({'error': str(e)}, status=400)
        else:
            self.send_error(404, "Not found")
    
    def do_GET(self):
        """Handle GET requests."""
        path = self.path
        
        # Web UI routes
        if path == '/' or path == '/index.html':
            self._serve_ui()
        elif path == '/api/config':
            self._send_json({
                'externalBaseUrl': EXTERNAL_BASE_URL,
                'port': PORT
            })
        elif path.startswith('/stream/'):
            upstream_url = urllib.parse.unquote(path[len('/stream/'):])
            self._handle_stream(upstream_url, head_only=False)
        elif path == '/cache/stats':
            self._send_json({
                'm3u8': m3u8_cache.stats(),
                'redirect': redirect_cache.stats()
            })
        else:
            self.send_error(404, "Not found")

    def _serve_ui(self):
        """Serve the control panel HTML."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(UI_HTML.encode('utf-8'))
    
    def _handle_stream(self, upstream_url, head_only=False):
        """Proxy a stream request to the upstream source."""
        try:
            # Decode the upstream URL (it may be a full encoded URL or a path)
            # If it contains a scheme (http/https), it's a full encoded URL
            import urllib.parse
            decoded_url = urllib.parse.unquote(upstream_url)
            if decoded_url.startswith('http://') or decoded_url.startswith('https://'):
                # This is a full encoded upstream URL
                actual_url = decoded_url
            else:
                # This is a path - try to reconstruct from redirect cache
                cached_redirect = redirect_cache.get(upstream_url)
                if cached_redirect and cached_redirect[3] != 'MISS':
                    actual_url = cached_redirect[0]
                    logger.info(f"Using cached redirect for {upstream_url[:60]}...")
                else:
                    actual_url = upstream_url
            
            # Fetch the stream
            resp = fetch_url(actual_url, timeout=TS_TIMEOUT if not head_only else M3U8_TIMEOUT)
            
            content_type = resp.headers.get('Content-Type', 'application/octet-stream')
            content_length = resp.headers.get('Content-Length')
            
            # Check if this is an m3u8 playlist that needs rewriting
            is_m3u8 = 'mpegurl' in content_type or upstream_url.endswith('.m3u8') or upstream_url.endswith('.m3u')
            
            if is_m3u8 and not head_only:
                # Read full playlist content for rewriting
                body = resp.read()
                resp.close()
                
                # Rewrite URLs in the playlist
                rewritten = rewrite_m3u8(body.decode('utf-8', errors='ignore'), actual_url)
                body_bytes = rewritten.encode('utf-8')
                
                # Send response with rewritten content
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Connection', 'keep-alive')
                self.send_header('Content-Length', str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                self.wfile.flush()
                
                # Cache the rewritten playlist
                m3u8_cache.put(upstream_url, body_bytes, content_type)
                return
            
            # For non-m3u8 (TS segments, etc.) - stream directly
            # Set response headers
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Connection', 'keep-alive')
            
            if content_length and not head_only:
                self.send_header('Content-Length', content_length)
            
            self.end_headers()
            
            if not head_only:
                # Stream in chunks
                chunk_count = 0
                min_size = TS_MIN_SIZE if 'mp2t' in content_type else 0
                
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    
                    self.wfile.write(chunk)
                    chunk_count += 1
                    
                    # Validate minimum size for TS segments
                    if min_size and chunk_count == 1 and len(chunk) < min_size:
                        logger.warning(f"TS segment too small: {len(chunk)} bytes")
                        break
                
                self.wfile.flush()
            
            resp.close()
            
        except Exception as e:
            logger.error(f"Stream error for {upstream_url[:60]}: {e}")
            self.send_error(502, "Bad Gateway")
    
    def _send_json(self, data, status=200):
        """Send a JSON response."""
        import json
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

# ============================================================
# Main
# ============================================================

def main():
    server = http.server.HTTPServer(('0.0.0.0', PORT), IPTVProxyHandler)
    server.timeout = 5
    
    logger.info(f"IPTV HLS Proxy starting on port {PORT}")
    if EXTERNAL_BASE_URL:
        logger.info(f"External base URL: {EXTERNAL_BASE_URL}")
    else:
        logger.warning("EXTERNAL_BASE_URL not set — m3u8 URL rewriting disabled")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()

if __name__ == '__main__':
    main()
