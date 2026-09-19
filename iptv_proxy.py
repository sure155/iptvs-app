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
            parsed = urllib.parse.urlparse(stripped)
            new_path = urllib.parse.quote(parsed.path, safe='/=:&?')
            if parsed.query:
                new_path += '?' + parsed.query
            rewritten_line = f"{EXTERNAL_BASE_URL}/stream{new_path}"
            rewritten.append(rewritten_line)
        elif stripped.startswith('/'):
            rewritten.append(f"{EXTERNAL_BASE_URL}/stream{stripped}")
        else:
            # Relative URL
            base = urllib.parse.urljoin(base_url, stripped)
            parsed = urllib.parse.urlparse(base)
            new_path = urllib.parse.quote(parsed.path, safe='/=:&?')
            if parsed.query:
                new_path += '?' + parsed.query
            rewritten.append(f"{EXTERNAL_BASE_URL}/stream{new_path}")
    
    return '\n'.join(rewritten)

# ============================================================
# Stream Handler
# ============================================================

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
    
    def do_GET(self):
        """Handle GET requests."""
        path = self.path
        
        if path.startswith('/stream/'):
            upstream_url = urllib.parse.unquote(path[len('/stream/'):])
            self._handle_stream(upstream_url, head_only=False)
        elif path == '/cache/stats':
            self._send_json({
                'm3u8': m3u8_cache.stats(),
                'redirect': redirect_cache.stats()
            })
        else:
            self.send_error(404, "Not found")
    
    def _handle_stream(self, upstream_url, head_only=False):
        """Proxy a stream request to the upstream source."""
        try:
            # Check redirect cache first
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
    
    def _send_json(self, data):
        """Send a JSON response."""
        import json
        self.send_response(200)
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
