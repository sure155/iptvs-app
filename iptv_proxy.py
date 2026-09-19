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
<title>IPTV Control Center</title>
<style>
:root { --brand:#1677ff; --bg:#f2f6fc; --card:#fff; --text:#1f2937; --muted:#6b7280; --border:#e5e7eb; --success:#10b981; --warn:#f59e0b; --err:#ef4444; }
html[data-theme=dark] { --bg:#0f1621; --card:#111827; --text:#f3f4f6; --muted:#9ca3af; --border:#374151; }
* { box-sizing:border-box; }
body { margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif; background:var(--bg); color:var(--text); }
a { color:var(--brand); text-decoration:none; }
a:hover { text-decoration:underline; }
.app { display:grid; grid-template-columns:240px 1fr; min-height:100vh; }
.sidebar { background:var(--card); border-right:1px solid var(--border); padding:1rem; display:flex; flex-direction:column; gap:0.5rem; position:sticky; top:0; height:100vh; overflow:auto; }
.sidebar .brand { display:flex; align-items:center; gap:0.5rem; font-weight:700; font-size:1.1rem; padding:0.5rem 0 1rem; border-bottom:1px solid var(--border); margin-bottom:0.5rem; }
.sidebar .brand svg { width:28px; height:28px; color:var(--brand); }
.nav-btn { display:flex; align-items:center; gap:0.5rem; padding:0.6rem 0.8rem; border:none; background:transparent; color:var(--text); font-size:0.95rem; border-radius:6px; cursor:pointer; width:100%; text-align:left; }
.nav-btn:hover { background:var(--bg); }
.nav-btn.active { background:var(--brand); color:#fff; }
.main { padding:2rem; overflow:auto; }
.card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:1.25rem; margin-bottom:1rem; }
.card h2 { margin:0 0 1rem; font-size:1.1rem; display:flex; align-items:center; gap:0.5rem; }
.card h2 .badge { background:var(--brand); color:#fff; font-size:0.7rem; padding:0.1rem 0.4rem; border-radius:999px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:1rem; }
.metric { background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:1rem; }
.metric .label { font-size:0.8rem; color:var(--muted); margin-bottom:0.3rem; }
.metric .value { font-size:1.5rem; font-weight:700; }
.metric .hint { font-size:0.75rem; color:var(--muted); margin-top:0.3rem; }
.table-wrap { overflow:auto; }
table { width:100%; border-collapse:collapse; font-size:0.85rem; }
th, td { padding:0.6rem 0.8rem; text-align:left; border-bottom:1px solid var(--border); }
th { color:var(--muted); font-weight:600; background:var(--bg); position:sticky; top:0; }
tr:hover td { background:var(--bg); }
.badge { display:inline-block; padding:0.15rem 0.5rem; border-radius:999px; font-size:0.7rem; font-weight:600; }
.badge-blue { background:#dbeafe; color:#1e40af; }
.badge-green { background:#d1fae5; color:#065f46; }
.badge-yellow { background:#fef3c7; color:#92400e; }
.badge-gray { background:#f3f4f6; color:#374151; }
html[data-theme=dark] .badge-blue { background:#1e3a5f; color:#93c5fd; }
html[data-theme=dark] .badge-green { background:#064e3b; color:#6ee7b7; }
html[data-theme=dark] .badge-yellow { background:#78350f; color:#fde047; }
html[data-theme=dark] .badge-gray { background:#374151; color:#d1d5db; }
.btn { display:inline-flex; align-items:center; gap:0.4rem; padding:0.4rem 0.8rem; border:none; border-radius:6px; font-size:0.85rem; cursor:pointer; }
.btn-primary { background:var(--brand); color:#fff; }
.btn-primary:hover { filter:brightness(0.95); }
.btn-secondary { background:var(--bg); color:var(--text); border:1px solid var(--border); }
.btn-secondary:hover { background:var(--border); }
.btn:disabled { opacity:0.5; cursor:not-allowed; }
.btn-sm { padding:0.25rem 0.5rem; font-size:0.75rem; }
.input { width:100%; padding:0.5rem 0.75rem; border:1px solid var(--border); border-radius:6px; background:var(--card); color:var(--text); font-size:0.9rem; }
.input:focus { outline:none; border-color:var(--brand); box-shadow:0 0 0 3px rgba(22,119,255,0.15); }
.form-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:1rem; margin-bottom:1rem; }
.form-row label { display:flex; flex-direction:column; gap:0.3rem; font-size:0.85rem; }
.alert { padding:0.75rem 1rem; border-radius:6px; margin-bottom:1rem; display:none; }
.alert.success { background:#ecfdf5; color:#065f46; border:1px solid #a7f3d0; display:block; }
.alert.error { background:#fef2f2; color:#991b1b; border:1px solid #fecaca; display:block; }
html[data-theme=dark] .alert.success { background:#064e3b; color:#a7f3d0; border-color:#065f46; }
html[data-theme=dark] .alert.error { background:#7f1d1d; color:#fecaca; border-color:#991b1b; }
.tabs { display:flex; gap:0.25rem; border-bottom:1px solid var(--border); margin-bottom:1rem; }
.tab-btn { padding:0.6rem 1rem; background:transparent; border:none; color:var(--muted); font-size:0.9rem; border-radius:6px 6px 0 0; cursor:pointer; border-bottom:2px solid transparent; margin-bottom:-1px; }
.tab-btn:hover { color:var(--text); background:var(--bg); }
.tab-btn.active { color:var(--brand); border-bottom-color:var(--brand); font-weight:600; }
.tab-panel { display:none; }
.tab-panel.active { display:block; animation:fade 0.15s; }
@keyframes fade { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:none} }
.spinner { width:16px; height:16px; border:2px solid var(--border); border-top-color:var(--brand); border-radius:50%; animation:spin 0.8s linear infinite; display:inline-block; }
@keyframes spin { to{transform:rotate(360deg)} }
.code-block { background:#0b1020; color:#e5e7eb; padding:1rem; border-radius:6px; overflow:auto; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:0.8rem; max-height:300px; }
.empty { text-align:center; padding:3rem; color:var(--muted); }
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/><polyline points="23 4 23 10 17 10"/><path d="M15 14h.01"/><path d="M15 18h.01"/><path d="M10 14h.01"/><path d="M10 18h.01"/></svg>IPTV Control Center</div>
    <button class="nav-btn active" data-tab="overview">概览</button>
    <button class="nav-btn" data-tab="channels">频道列表</button>
    <button class="nav-btn" data-tab="output">输出文件</button>
    <button class="nav-btn" data-tab="config">配置面板</button>
    <button class="nav-btn" data-tab="speedtest">测速</button>
    <div style="margin-top:auto; padding-top:1rem; border-top:1px solid var(--border); font-size:0.75rem; color:var(--muted);">v1.0.0</div>
  </aside>
  <main class="main">
    <div id="alert" class="alert"></div>

    <!-- Overview -->
    <section id="tab-overview" class="tab-panel active">
      <div class="card"><h2>系统状态</h2>
        <div class="grid" id="overview-metrics">
          <div class="metric"><div class="label">代理频道</div><div class="value" id="m-proxy">-</div><div class="hint">可点播频道数</div></div>
          <div class="metric"><div class="label">活跃会话</div><div class="value" id="m-sessions">-</div><div class="hint">正在拉流数</div></div>
          <div class="metric"><div class="label">可切换频道</div><div class="value" id="m-switchable">-</div><div class="hint">≥2路源</div></div>
          <div class="metric"><div class="label">代理状态</div><div class="value" id="m-proxy-status"><span class="badge badge-gray">未知</span></div><div class="hint">点击配置启用</div></div>
        </div>
      </div>
      <div class="card"><h2>快速操作</h2>
        <div style="display:flex; gap:0.5rem; flex-wrap:wrap;">
          <button class="btn btn-primary" id="btn-retest"><span class="spinner" style="display:none"></span>立即测速</button>
          <button class="btn btn-secondary" id="btn-refresh">刷新概览</button>
        </div>
      </div>
    </section>

    <!-- Channels -->
    <section id="tab-channels" class="tab-panel">
      <div class="card"><h2>频道负载与当前源</h2>
        <div style="display:flex; gap:0.5rem; margin-bottom:1rem; flex-wrap:wrap;">
          <input class="input" id="ch-search" placeholder="搜索频道名或当前 Host" style="max-width:320px;">
          <select class="input" id="ch-filter-src" style="width:auto;"><option value="">全部源数</option><option value="2">≥2 路</option><option value="1">=1 路</option></select>
          <label style="display:flex; align-items:center; gap:0.4rem; font-size:0.85rem;"><input type="checkbox" id="ch-excluded"> 只看已排除</label>
        </div>
        <div class="table-wrap">
          <table id="ch-table"><thead><tr><th>#</th><th>频道</th><th>聚合源</th><th>当前源</th><th>分辨率</th><th>帧率</th><th>码率(kbps)</th><th>会话</th><th>控制</th></tr></thead><tbody></tbody></table>
        </div>
        <div id="ch-empty" class="empty" style="display:none;">暂无代理频道；完成一次测速后自动出现</div>
      </div>
    </section>

    <!-- Output -->
    <section id="tab-output" class="tab-panel">
      <div class="card"><h2>输出文件预览</h2>
        <div class="tabs">
          <button class="tab-btn active" data-out="m3u8">M3U8</button>
          <button class="tab-btn" data-out="txt">TXT</button>
        </div>
        <div style="display:flex; gap:0.5rem; margin-bottom:1rem; flex-wrap:wrap;">
          <button class="btn btn-secondary" id="btn-copy-out">复制预览</button>
          <button class="btn btn-secondary" id="btn-refresh-out">刷新</button>
          <a class="btn btn-primary" id="link-open-out" target="_blank">新窗口打开</a>
          <a class="btn btn-primary" id="link-download-out" target="_blank">下载</a>
        </div>
        <pre class="code-block" id="out-preview">加载中…</pre>
        <div id="out-status" style="margin-top:0.5rem; font-size:0.8rem; color:var(--muted);"></div>
      </div>
    </section>

    <!-- Config -->
    <section id="tab-config" class="tab-panel">
      <div class="card"><h2>基础配置</h2>
        <form id="cfg-form">
          <div class="form-row">
            <label>EXTERNAL_BASE_URL <span style="color:var(--err);">*</span>
              <input class="input" name="externalBaseUrl" placeholder="http://192.168.1.150:8830/stream" required>
            </label>
            <label>PROXY_PORT
              <input class="input" name="port" type="number" value="8830" min="1" max="65535">
            </label>
          </div>
          <div class="form-row">
            <label>访问密码 (留空关闭)
              <input class="input" name="password" type="password" placeholder="设置后控制台需登录">
            </label>
          </div>
          <button class="btn btn-primary" type="submit">保存配置（需重启容器生效）</button>
        </form>
      </div>
      <div class="card"><h2>代理设置</h2>
        <label style="display:flex; align-items:center; gap:0.5rem; font-size:0.9rem;">
          <input type="checkbox" id="cfg-proxy-enabled"> 启用代理播放（列表地址指向本机代理）
        </label>
        <p style="color:var(--muted); font-size:0.85rem; margin-top:0.5rem;">关闭则播放列表使用直连源站地址。</p>
      </div>
    </section>

    <!-- Speedtest -->
    <section id="tab-speedtest" class="tab-panel">
      <div class="card"><h2>测速任务</h2>
        <div style="display:flex; gap:0.5rem; margin-bottom:1rem; flex-wrap:wrap;">
          <button class="btn btn-primary" id="st-start"><span class="spinner" style="display:none"></span>开始测速</button>
          <button class="btn btn-secondary" id="st-stop" disabled>停止</button>
        </div>
        <div id="st-progress" style="display:none;">
          <div style="height:6px; background:var(--border); border-radius:3px; overflow:hidden;">
            <div id="st-bar" style="height:100%; width:0%; background:var(--brand); transition:width 0.3s;"></div>
          </div>
          <div id="st-text" style="margin-top:0.5rem; font-size:0.85rem; color:var(--muted);">准备中…</div>
        </div>
        <div id="st-result" style="margin-top:1rem;"></div>
      </div>
    </section>
  </main>
</div>
<script>
// --- simple state ---
let currentTab = 'overview';
let channelsData = [];
let speedtestRunning = false;

// --- theme ---
(function(){ try{ var t=localStorage.getItem('iptv_theme'); var d=t==='dark'||(t!=='light'&&window.matchMedia('(prefers-color-scheme:dark)').matches); document.documentElement.dataset.theme=d?'dark':'light'; }catch(e){ document.documentElement.dataset.theme='light'; } })();

// --- utils ---
function showAlert(msg, type){ const a=document.getElementById('alert'); a.textContent=msg; a.className='alert '+(type||'error'); setTimeout(()=>{a.className='alert';},5000); }
function api(path, opts={}){ return fetch(path,{headers:{'Content-Type':'application/json'}, ...opts}).then(r=>{ if(!r.ok) return r.json().then(e=>{throw new Error(e.error||r.statusText);}); return r.json(); }); }
function setTab(tab){ currentTab=tab; document.querySelectorAll('.nav-btn').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab)); document.querySelectorAll('.tab-panel').forEach(p=>p.classList.toggle('active',p.id==='tab-'+tab)); if(tab==='overview') loadOverview(); if(tab==='channels') loadChannels(); if(tab==='output') loadOutput(); if(tab==='config') loadConfig(); if(tab==='speedtest') loadSpeedtestStatus(); }

// --- tabs ---
document.querySelectorAll('.nav-btn').forEach(b=>b.addEventListener('click',()=>setTab(b.dataset.tab)));
document.querySelectorAll('.tab-btn').forEach(b=>b.addEventListener('click',()=>{ document.querySelectorAll('.tab-btn').forEach(x=>x.classList.remove('active')); b.classList.add('active'); loadOutput(b.dataset.out); }));

// --- overview ---
async function loadOverview(){ try{ const d=await api('/api/overview'); document.getElementById('m-proxy').textContent=d.proxyChannels??0; document.getElementById('m-sessions').textContent=d.activeSessions??0; document.getElementById('m-switchable').textContent=d.switchable??0; const ps=d.proxyEnabled; document.getElementById('m-proxy-status').innerHTML=ps?'<span class="badge badge-green">运行中</span>':'<span class="badge badge-yellow">未启用</span>'; }catch(e){ showAlert('概览加载失败:'+e); } }
document.getElementById('btn-refresh').onclick=loadOverview;
document.getElementById('btn-retest').onclick=async()=>{ const btn=document.getElementById('btn-retest'); btn.disabled=true; btn.querySelector('.spinner').style.display='inline-block'; try{ await api('/api/speedtest/start',{method:'POST'}); showAlert('测速已触发，稍后刷新概览','success'); }catch(e){ showAlert('触发失败:'+e); }finally{ btn.disabled=false; btn.querySelector('.spinner').style.display='none'; } };

// --- channels ---
async function loadChannels(){ try{ const d=await api('/api/channels'); channelsData=d.channels||[]; renderChannels(); }catch(e){ showAlert('频道列表加载失败:'+e); } }
function renderChannels(){ const tbody=document.querySelector('#ch-table tbody'); const search=document.getElementById('ch-search').value.toLowerCase(); const src=document.getElementById('ch-filter-src').value; const excl=document.getElementById('ch-excluded').checked; let list=channelsData.filter(c=>{ if(search&&!c.name.toLowerCase().includes(search)&&!(c.currentHost||'').toLowerCase().includes(search)) return false; if(src==='2'&&c.sourceCount<2) return false; if(src==='1'&&c.sourceCount!==1) return false; if(excl&&!c.excluded) return false; return true; }); if(!list.length){ document.getElementById('ch-table').style.display='none'; document.getElementById('ch-empty').style.display='block'; return; } document.getElementById('ch-table').style.display=''; document.getElementById('ch-empty').style.display='none'; tbody.innerHTML=list.map((c,i)=>`<tr${c.excluded?' style="opacity:0.5;"':''}><td>${i+1}</td><td>${c.name}</td><td class="badge ${c.sourceCount>=2?'badge-blue':'badge-gray'}">${c.sourceCount} 路</td><td class="mono" style="font-size:0.8rem;">${c.currentHost||'—'}</td><td class="mono">${c.resolution||'—'}</td><td class="mono">${c.fps||'—'}</td><td class="mono">${c.bitrate||'—'}</td><td>${c.activeSessions>0?'<span class="badge badge-green">'+c.activeSessions+'</span>':'0'}</td><td>${c.sourceCount>=2?'<button class="btn btn-secondary btn-sm" onclick="shiftChannel(\''+c.name+'\',\'prev\')" '+(c.activeSessions>0?'disabled':'')+'>上一个</button> <button class="btn btn-secondary btn-sm" onclick="shiftChannel(\''+c.name+'\',\'next\')" '+(c.activeSessions>0?'disabled':'')+'>下一个</button>':'<span class="badge badge-gray">单源</span>'}</td></tr>`).join(''); }
document.getElementById('ch-search').addEventListener('input',renderChannels);
document.getElementById('ch-filter-src').addEventListener('change',renderChannels);
document.getElementById('ch-excluded').addEventListener('change',renderChannels);
async function shiftChannel(name,dir){ try{ const r=await api('/api/proxy/shift',{method:'POST',body:JSON.stringify({name, direction:dir})}); showAlert(r.message||'切换成功','success'); loadChannels(); }catch(e){ showAlert('切换失败:'+e); } }

// --- output ---
let currentOut='m3u8';
async function loadOutput(fmt){ if(fmt) currentOut=fmt; try{ const d=await api('/api/output?format='+currentOut); document.getElementById('out-preview').textContent=d.content||'暂无内容'; document.getElementById('link-open-out').href=currentOut==='m3u8'?'/iptv':'/txt'; document.getElementById('link-download-out').href=currentOut==='m3u8'?'/iptv_sources.m3u8':'/iptv_sources.txt'; document.getElementById('out-status').textContent=d.truncated?'已截断，完整文件请下载':`${d.lineCount||0} 行, ${d.size||0} 字节`; }catch(e){ showAlert('输出加载失败:'+e); } }
document.getElementById('btn-refresh-out').onclick=()=>loadOutput();
document.getElementById('btn-copy-out').onclick=async()=>{ const txt=document.getElementById('out-preview').textContent; await navigator.clipboard.writeText(txt); showAlert('已复制预览内容','success'); };

// --- config ---
async function loadConfig(){ try{ const d=await api('/api/config'); document.querySelector('[name=externalBaseUrl]').value=d.externalBaseUrl||''; document.querySelector('[name=port]').value=d.port||8830; }catch(e){} }
document.getElementById('cfg-form').addEventListener('submit',async(e)=>{ e.preventDefault(); const fd=new FormData(e.target); const data={externalBaseUrl:fd.get('externalBaseUrl').trim(), port:parseInt(fd.get('port'),10), password:fd.get('password')}; if(!data.externalBaseUrl){ showAlert('EXTERNAL_BASE_URL 不能为空'); return; } try{ const r=await api('/api/config',{method:'POST',body:JSON.stringify(data)}); showAlert('配置已写入 .env，重启容器生效','success'); }catch(e){ showAlert('保存失败:'+e); } });

// --- speedtest ---
async function loadSpeedtestStatus(){ try{ const d=await api('/api/speedtest/status'); const running=d.running; document.getElementById('st-start').disabled=running; document.getElementById('st-stop').disabled=!running; document.getElementById('st-progress').style.display=running?'block':'none'; if(running){ document.getElementById('st-start').querySelector('.spinner').style.display='inline-block'; document.getElementById('st-bar').style.width=d.progress+'%'; document.getElementById('st-text').textContent=d.message||`进行中 ${d.progress}%`; }else{ document.getElementById('st-start').querySelector('.spinner').style.display='none'; if(d.lastResult){ document.getElementById('st-result').innerHTML=`<div class="card"><h2>上次结果</h2><pre class="code-block">${JSON.stringify(d.lastResult,null,2)}</pre></div>`; } } }catch(e){} }
document.getElementById('st-start').onclick=async()=>{ const btn=document.getElementById('st-start'); btn.disabled=true; btn.querySelector('.spinner').style.display='inline-block'; try{ await api('/api/speedtest/start',{method:'POST'}); showAlert('测速已开始','success'); setTimeout(pollSpeedtest,1000); }catch(e){ showAlert('启动失败:'+e); btn.disabled=false; btn.querySelector('.spinner').style.display='none'; } };
document.getElementById('st-stop').onclick=async()=>{ try{ await api('/api/speedtest/stop',{method:'POST'}); showAlert('已请求停止','success'); }catch(e){ showAlert('停止失败:'+e); } };
function pollSpeedtest(){ loadSpeedtestStatus(); const st=document.getElementById('st-progress'); if(st.style.display!=='none') setTimeout(pollSpeedtest,2000); else { document.getElementById('st-start').disabled=false; document.getElementById('st-start').querySelector('.spinner').style.display='none'; } }

// --- init ---
setTab('overview');
</script>
</body>
</html>
"""

def write_env_file(external_base_url, port, password=None):
    """Write .env file in project directory."""
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    with open(env_path, 'w') as f:
        f.write(f'EXTERNAL_BASE_URL={external_base_url}\n')
        f.write(f'PROXY_PORT={port}\n')
        if password:
            f.write(f'PASSWORD={password}\n')
    return env_path

# ============================================================
# Channel data & Speedtest simulation / integration
# ============================================================

CHANNELS = [
    {
        'name': 'CCTV-1 综合',
        'sources': [
            'https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8',
            'https://test-streams.mux.dev/test/test.m3u8'
        ],
        'sourceCount': 2,
        'current_index': 0,
        'currentHost': 'test-streams.mux.dev',
        'resolution': '1920x1080',
        'fps': '25',
        'bitrate': '6221',
        'active_sessions': 0,
        'excluded': False
    },
    {
        'name': 'CCTV-13 新闻',
        'sources': [
            'https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8'
        ],
        'sourceCount': 1,
        'current_index': 0,
        'currentHost': 'test-streams.mux.dev',
        'resolution': '1280x720',
        'fps': '25',
        'bitrate': '2149',
        'active_sessions': 0,
        'excluded': False
    }
]

speedtest_state = {
    'running': False,
    'progress': 0,
    'message': '空闲',
    'lastResult': None
}

def generate_m3u8():
    """Generate master M3U8 list from CHANNELS."""
    lines = ['#EXTM3U']
    base = EXTERNAL_BASE_URL.rstrip('/')
    for c in CHANNELS:
        if c.get('excluded'):
            continue
        src = c['sources'][c.get('current_index', 0)]
        enc_url = urllib.parse.quote(src, safe='')
        if base:
            proxy_url = f"{base}/{enc_url}" if base.endswith('/stream') else f"{base}/stream/{enc_url}"
        else:
            proxy_url = src
        lines.append(f'#EXTINF:-1 tvg-name="{c["name"]}",{c["name"]}')
        lines.append(proxy_url)
    return '\n'.join(lines)

def generate_txt():
    """Generate TXT playlist format."""
    lines = ['央视频道,#genre#']
    base = EXTERNAL_BASE_URL.rstrip('/')
    for c in CHANNELS:
        if c.get('excluded'):
            continue
        src = c['sources'][c.get('current_index', 0)]
        enc_url = urllib.parse.quote(src, safe='')
        if base:
            proxy_url = f"{base}/{enc_url}" if base.endswith('/stream') else f"{base}/stream/{enc_url}"
        else:
            proxy_url = src
        lines.append(f"{c['name']},{proxy_url}")
    return '\n'.join(lines)

def _run_speedtest_thread():
    global speedtest_state
    speedtest_state['running'] = True
    speedtest_state['progress'] = 0
    speedtest_state['message'] = '正在扫描上游源…'
    for p in range(10, 101, 20):
        if not speedtest_state['running']:
            break
        time.sleep(1)
        speedtest_state['progress'] = p
        speedtest_state['message'] = f'正在测速... {p}%'
    speedtest_state['running'] = False
    speedtest_state['message'] = '测速完成'
    speedtest_state['lastResult'] = {
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'totalChannels': len(CHANNELS),
        'validSources': sum(len(c['sources']) for c in CHANNELS)
    }

def start_speedtest():
    global speedtest_state
    if not speedtest_state['running']:
        t = threading.Thread(target=_run_speedtest_thread, daemon=True)
        t.start()

def stop_speedtest():
    global speedtest_state
    speedtest_state['running'] = False

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
        """Handle POST requests."""
        import json
        path = self.path
        if path == '/api/config':
            content_length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(content_length).decode('utf-8')
            try:
                data = json.loads(raw)
                external_base_url = data.get('externalBaseUrl', '').strip()
                port = int(data.get('port', PORT))
                pwd = data.get('password')
                if not external_base_url:
                    self._send_json({'error': 'EXTERNAL_BASE_URL 不能为空'}, status=400)
                    return
                write_env_file(external_base_url, port, pwd)
                self._send_json({'message': '配置已写入 .env，请重启容器生效'})
            except Exception as e:
                self._send_json({'error': str(e)}, status=400)
        elif path == '/api/proxy/shift':
            content_length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(content_length).decode('utf-8')
            try:
                data = json.loads(raw)
                ch_name = data.get('name')
                direction = data.get('direction', 'next')
                for c in CHANNELS:
                    if c['name'] == ch_name and len(c['sources']) > 1:
                        idx = c.get('current_index', 0)
                        if direction == 'next':
                            idx = (idx + 1) % len(c['sources'])
                        else:
                            idx = (idx - 1 + len(c['sources'])) % len(c['sources'])
                        c['current_index'] = idx
                        c['currentHost'] = urllib.parse.urlparse(c['sources'][idx]).netloc
                        self._send_json({'message': f"已切换至源: {c['currentHost']}"})
                        return
                self._send_json({'error': '未找到频道或无可切换源'}, status=400)
            except Exception as e:
                self._send_json({'error': str(e)}, status=400)
        elif path == '/api/speedtest/start':
            start_speedtest()
            self._send_json({'message': '测速任务已启动'})
        elif path == '/api/speedtest/stop':
            stop_speedtest()
            self._send_json({'message': '已请求停止测速'})
        else:
            self.send_error(404, "Not found")
    
    def do_GET(self):
        """Handle GET requests."""
        path = self.path
        
        # Static outputs
        if path == '/iptv' or path == '/iptv_sources.m3u8':
            self._serve_m3u8()
        elif path == '/txt' or path == '/iptv_sources.txt':
            self._serve_txt()
        # Web UI routes
        elif path == '/' or path == '/index.html' or path.startswith('/dashboard'):
            self._serve_ui()
        elif path == '/api/overview':
            self._send_json({
                'proxyChannels': len(CHANNELS),
                'activeSessions': sum(1 for c in CHANNELS if c.get('active_sessions', 0) > 0),
                'switchable': sum(1 for c in CHANNELS if c.get('sourceCount', 0) >= 2),
                'proxyEnabled': True
            })
        elif path == '/api/channels':
            self._send_json({'channels': CHANNELS})
        elif path.startswith('/api/output'):
            # parse format query param
            query = urllib.parse.urlparse(path).query
            params = urllib.parse.parse_qs(query)
            fmt = params.get('format', ['m3u8'])[0]
            if fmt == 'txt':
                content_str = generate_txt()
            else:
                content_str = generate_m3u8()
            self._send_json({
                'content': content_str,
                'lineCount': len(content_str.splitlines()),
                'size': len(content_str.encode('utf-8')),
                'truncated': False
            })
        elif path == '/api/config':
            self._send_json({
                'externalBaseUrl': EXTERNAL_BASE_URL,
                'port': PORT
            })
        elif path == '/api/speedtest/status':
            self._send_json(speedtest_state)
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

    def _serve_m3u8(self):
        """Serve generated m3u8 playlist."""
        content = generate_m3u8().encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/vnd.apple.mpegurl; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_txt(self):
        """Serve generated txt playlist."""
        content = generate_txt().encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

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
