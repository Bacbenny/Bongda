import gzip
import hashlib
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urljoin, urlparse, unquote

import cloudscraper
import requests
from flask import Flask, Response, request

app = Flask(__name__)

# ─── TieuLam TV config ────────────────────────────────────────────────────────
TIEULAM_FRONTEND_URL   = os.environ.get("TIEULAM_FRONTEND",  "https://sv1.tieulamwc3.com")
TIEULAM_KNOWN_API_BASE = os.environ.get("TIEULAM_API",       "https://api.tlap17062026.com")
TIEULAM_STREAM_CDN     = os.environ.get("TIEULAM_CDN",       "https://live.lilive2.eu.cc")
# GitHub Actions cache — cập nhật mỗi 30 phút
TIEULAM_CACHE_URL      = os.environ.get(
    "TIEULAM_CACHE_URL",
    "https://raw.githubusercontent.com/Bacbenny/Verceliptv/main/data/tieulam_cache.json",
)
TIEULAM_CACHE_MAX_AGE  = int(os.environ.get("TIEULAM_CACHE_MAX_AGE", "2100"))  # 35 min
# Relay URL (Cloudflare Worker / Replit) — bỏ qua Cloudflare IP-block
TIEULAM_RELAY_URL      = os.environ.get("TIEULAM_RELAY_URL",   "")
TIEULAM_RELAY_URL_2    = os.environ.get("TIEULAM_RELAY_URL_2", "")
TIEULAM_RELAY_SECRET   = os.environ.get("RELAY_SECRET",        "")

# ─── PA3: Proxy rotation (SOCKS5 / HTTP) ─────────────────────────────────────
# Thêm nhiều proxy cách nhau bằng dấu phẩy:
#   SOCKS5_PROXY=socks5://user:pass@host:1080,socks5://user:pass@host2:1080
#   HTTP_PROXY=http://user:pass@host:8080
_PROXY_LIST: list = []

def _load_proxies() -> None:
    """Đọc SOCKS5_PROXY và HTTP_PROXY từ env, nạp vào pool rotation."""
    global _PROXY_LIST
    pool = []
    socks_str = os.environ.get("SOCKS5_PROXY", "")
    if socks_str:
        for p in socks_str.split(","):
            p = p.strip()
            if not p:
                continue
            if p.startswith("socks5://"):
                p = "socks5h" + p[len("socks5"):]  # DNS qua proxy
            pool.append(p)
    for key in ("HTTP_PROXY", "HTTPS_PROXY"):
        http_str = os.environ.get(key, "")
        if http_str:
            for p in http_str.split(","):
                p = p.strip()
                if p:
                    pool.append(p)
    # Deduplicate
    seen = set()
    _PROXY_LIST = [x for x in pool if not (x in seen or seen.add(x))]

_load_proxies()

def _pick_proxy() -> dict | None:
    """Trả về dict proxies ngẫu nhiên từ pool, hoặc None nếu không có proxy."""
    if not _PROXY_LIST:
        return None
    chosen = random.choice(_PROXY_LIST)
    return {"http": chosen, "https": chosen}

# ─── PA2: Proxy session ───────────────────────────────────────────────────────
_M3U8_CACHE: dict = {}   # url_key → (content_str, timestamp)
_TS_CACHE: dict   = {}   # url → (bytes, timestamp)
_M3U8_TTL = 5    # seconds
_TS_TTL   = 10   # seconds

_proxy_session = requests.Session()
_proxy_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"})

def _parse_hq_params() -> dict:
    """Parse h_<Key>=<Value> query params thành dict headers."""
    return {
        unquote(k[2:]).replace("_", "-"): unquote(v)
        for k, v in request.args.items()
        if k.lower().startswith("h_")
    }

def _get_public_url() -> str:
    """Trả về base URL công khai của server này (không có dấu / cuối)."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render_url:
        return render_url.rstrip("/")
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        return f"https://{domains.split(',')[0].strip()}"
    vercel_url = os.environ.get("VERCEL_URL", "")
    if vercel_url:
        return f"https://{vercel_url}"
    app_url = os.environ.get("APP_URL", "")
    if app_url:
        return app_url.rstrip("/")
    port = os.environ.get("PORT", "5000")
    return f"http://localhost:{port}"

@app.route("/proxy/m3u")
def route_proxy_m3u():
    """PA2: Proxy M3U8 playlist — fetch với headers đúng, rewrite segment URLs."""
    url = request.args.get("url", "").strip()
    if not url:
        return Response("Missing url param", status=400, mimetype="text/plain")

    # Build cache key từ URL + headers
    extra = _parse_hq_params()
    cache_key = url + "|" + str(sorted(extra.items()))
    now = time.time()
    cached = _M3U8_CACHE.get(cache_key)
    if cached and (now - cached[1]) < _M3U8_TTL:
        return Response(cached[0], content_type="application/vnd.apple.mpegurl")

    tl_ref = TIEULAM_FRONTEND_URL.rstrip("/") + "/"
    headers = {
        "User-Agent": extra.get("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Referer":    extra.get("Referer",    tl_ref),
        "Origin":     extra.get("Origin",     TIEULAM_FRONTEND_URL),
        **extra,
    }

    try:
        proxies = _pick_proxy()
        resp = _proxy_session.get(url, headers=headers, timeout=15,
                                  allow_redirects=True, proxies=proxies)
        resp.raise_for_status()
    except Exception as e:
        return Response(f"Proxy fetch error: {e}", status=502, mimetype="text/plain")

    content   = resp.text
    final_url = resp.url
    parsed    = urlparse(final_url)
    base      = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rsplit('/', 1)[0]}/"

    # Encode headers vào query string để truyền cho /proxy/ts
    hq_parts = [f"h_{quote(k)}={quote(v)}" for k, v in headers.items()]
    hq = "&".join(hq_parts)
    pub = _get_public_url()

    lines = []
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            seg = urljoin(base, line)
            line = f"{pub}/proxy/ts?url={quote(seg, safe='')}&{hq}"
        lines.append(line)

    result = "\n".join(lines)
    _M3U8_CACHE[cache_key] = (result, now)
    return Response(result, content_type="application/vnd.apple.mpegurl")


@app.route("/proxy/ts")
def route_proxy_ts():
    """PA2: Proxy TS segment — fetch với headers đúng, trả về raw bytes."""
    url = request.args.get("url", "").strip()
    if not url:
        return Response("Missing url param", status=400, mimetype="text/plain")

    now = time.time()
    cached = _TS_CACHE.get(url)
    if cached and (now - cached[1]) < _TS_TTL:
        return Response(cached[0], content_type="video/mp2t")

    headers = _parse_hq_params()
    try:
        proxies = _pick_proxy()
        resp = _proxy_session.get(url, headers=headers, timeout=20,
                                  allow_redirects=True, proxies=proxies)
        resp.raise_for_status()
        data = resp.content
        _TS_CACHE[url] = (data, now)
        # Giữ cache nhỏ (tối đa 200 segment)
        if len(_TS_CACHE) > 200:
            oldest = sorted(_TS_CACHE.items(), key=lambda x: x[1][1])[:50]
            for k, _ in oldest:
                _TS_CACHE.pop(k, None)
        return Response(data, content_type="video/mp2t")
    except Exception as e:
        return Response(f"TS proxy error: {e}", status=502, mimetype="text/plain")

# ─── Cola TV config ───────────────────────────────────────────────────────────
COLATV_FRONTEND_URL   = os.environ.get("COLATV_FRONTEND", "https://colatv48.live")
COLATV_KNOWN_API_URL  = os.environ.get("COLATV_API",      "https://api.cltvlv.com/api/matches")

# ─── Hội Quán TV config ───────────────────────────────────────────────────────
HOIQUAN_FRONTEND_URL  = os.environ.get("HOIQUAN_FRONTEND", "https://sv2.hoiquan4.live")
HOIQUAN_KNOWN_API_BASE= os.environ.get("HOIQUAN_API",      "https://sv.hoiquantv.xyz/api/v1/external")

# ─── Khán Đài A config ───────────────────────────────────────────────────────
KHANDAIA_FRONTEND_URL   = os.environ.get("KHANDAIA_FRONTEND", "https://tructiep.khandaia.link")
KHANDAIA_KNOWN_API_BASE = os.environ.get("KHANDAIA_API",      "https://sv.khandai-a.xyz/api/v1/external")

# ─── Vòng Cấm TV config ──────────────────────────────────────────────────────
VONGCAM_FRONTEND_URL   = os.environ.get("VONGCAM_FRONTEND", "https://sv2.vongcam3.live")
VONGCAM_KNOWN_API_BASE = os.environ.get("VONGCAM_API",      "https://sv.bugiotv.xyz/internal/api/matches")
VONGCAM_FALLBACK_TOKEN = os.environ.get("VONGCAM_TOKEN",    "AB321C")

# ─── Dekiki (GitHub-hosted static list) + EPG ────────────────────────────────
DEKIKI_M3U_URL = os.environ.get(
    "DEKIKI_M3U_URL",
    "https://raw.githubusercontent.com/Bacbenny/Bongda/refs/heads/main/xemtv.m3u",
)
EPG_URL = os.environ.get("EPG_URL", "https://vnepg.site/epg.xml")

# ─── Shared config ────────────────────────────────────────────────────────────
VN_TZ                = timezone(timedelta(hours=7))
SELF_PING_INTERVAL   = 240   # seconds
PREFETCH_INTERVAL    = 1800   # seconds — refresh cache every 30 minutes
API_DISCOVERY_TTL    = 3600  # seconds — re-discover API URL every 1 hour

COLATV_FINISHED_STATUS_INT = {3}
FINISHED_STATUS_STRINGS    = {"finished", "end", "ended", "complete", "completed"}
MATCH_MAX_AGE_SECONDS      = int(os.environ.get("MATCH_MAX_DURATION", 7200))  # 2 h

# ─── Sport logos (Twemoji via jsDelivr) ───────────────────────────────────────
_CDN = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
SPORT_LOGOS = {
    "football":    f"{_CDN}/26bd.png",
    "tennis":      f"{_CDN}/1f3be.png",
    "basketball":  f"{_CDN}/1f3c0.png",
    "volleyball":  f"{_CDN}/1f3d0.png",
    "billiards":   f"{_CDN}/1f3b1.png",
    "badminton":   f"{_CDN}/1f3f8.png",
    "boxing":      f"{_CDN}/1f94a.png",
    "golf":        f"{_CDN}/26f3.png",
    "esport":      f"{_CDN}/1f3ae.png",
    "motorsport":  f"{_CDN}/1f3ce.png",
    "athletics":   f"{_CDN}/1f3c3.png",
    "swimming":    f"{_CDN}/1f3ca.png",
    "martialarts": f"{_CDN}/1f94b.png",
    "cycling":     f"{_CDN}/1f6b4.png",
    "hockey":      f"{_CDN}/1f3d2.png",
    "default":     f"{_CDN}/1f3c6.png",
}

# ─── API URL caches ───────────────────────────────────────────────────────────
_tieulam_api_cache  = {"url": TIEULAM_KNOWN_API_BASE + "/matches/graph", "discovered_at": 0}
_colatv_api_cache   = {"url": COLATV_KNOWN_API_URL,    "discovered_at": 0}
_hoiquan_api_cache  = {"url": HOIQUAN_KNOWN_API_BASE,  "discovered_at": 0}
_khandaia_api_cache = {"url": KHANDAIA_KNOWN_API_BASE, "discovered_at": 0}
_vongcam_api_cache  = {"url": VONGCAM_KNOWN_API_BASE,  "discovered_at": 0}

# ─── Auto domain resolution ───────────────────────────────────────────────────
def _resolve_base_url(url: str, timeout: int = 8) -> str:
    """Follow HTTP 3xx redirects và trả về scheme+host cuối cùng."""
    try:
        r = requests.get(
            url, timeout=timeout, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        final = r.url or url
    except Exception:
        final = url
    m = re.match(r"(https?://[^/?#]+)", final)
    return m.group(1) if m else url.rstrip("/")


def _resolve_all_frontends() -> None:
    """Tự động cập nhật domain frontend bằng cách follow redirect."""
    global HOIQUAN_FRONTEND_URL, KHANDAIA_FRONTEND_URL, VONGCAM_FRONTEND_URL
    sources = {
        "Hội Quán TV": ("HOIQUAN",  HOIQUAN_FRONTEND_URL),
        "Khán Đài A":  ("KHANDAIA", KHANDAIA_FRONTEND_URL),
        "Vòng Cấm TV": ("VONGCAM",  VONGCAM_FRONTEND_URL),
    }
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_resolve_base_url, cfg[1]): (name, cfg) for name, cfg in sources.items()}
        for fut in as_completed(futures):
            (name, (key, original)) = futures[fut]
            try:
                resolved = fut.result()
            except Exception:
                resolved = original
            if resolved != original.rstrip("/"):
                print(f"[domain-resolve] {name}: {original} → {resolved}", flush=True)
            if key == "HOIQUAN":
                HOIQUAN_FRONTEND_URL = resolved
            elif key == "KHANDAIA":
                KHANDAIA_FRONTEND_URL = resolved
            elif key == "VONGCAM":
                VONGCAM_FRONTEND_URL = resolved


_vongcam_token_cache = {"token": VONGCAM_FALLBACK_TOKEN, "discovered_at": 0}

# ─── Playlist content cache ───────────────────────────────────────────────────
def _empty_entry():
    return {"content": None, "gz": None, "etag": None, "built_at": 0,
            "lock": threading.Lock()}

_playlist_cache = {
    "combined": _empty_entry(),
    "tieulam":  _empty_entry(),
    "cola":     _empty_entry(),
    "hoiquan":  _empty_entry(),
    "khandaia": _empty_entry(),
    "vongcam":  _empty_entry(),
    "dekiki":   _empty_entry(),
}

_last_counts = {
    "tieulam": 0, "cola": 0, "hoiquan": 0, "khandaia": 0, "vongcam": 0, "dekiki": 0,
    "refreshed_at": 0, "last_error": "",
}

# ══════════════════════════════════════════════════════════════════════════════
#  Sport logo helpers
# ══════════════════════════════════════════════════════════════════════════════

def _logo_from_text(text: str) -> str:
    t = text.lower()
    if "tennis" in t:
        return SPORT_LOGOS["tennis"]
    if any(k in t for k in ["basketball", "bóng rổ", "bong ro", "nba", "wnba"]):
        return SPORT_LOGOS["basketball"]
    if any(k in t for k in ["volleyball", "bóng chuyền", "bong chuyen"]):
        return SPORT_LOGOS["volleyball"]
    if any(k in t for k in ["billiard", "bi-a", "bia", "snooker", "pool", "uk open"]):
        return SPORT_LOGOS["billiards"]
    if any(k in t for k in ["badminton", "cầu lông", "cau long"]):
        return SPORT_LOGOS["badminton"]
    if any(k in t for k in ["boxing", "kickbox", "muay", "quyền anh", "quyen anh", "ufc", "mma"]):
        return SPORT_LOGOS["boxing"]
    if any(k in t for k in ["golf"]):
        return SPORT_LOGOS["golf"]
    if any(k in t for k in ["esport", "e-sport", "gaming", "lol", "dota", "valorant", "fifa online"]):
        return SPORT_LOGOS["esport"]
    if any(k in t for k in ["motorsport", "f1", "formula", "motogp", "nascar", "rally", "wtcc", "dtm"]):
        return SPORT_LOGOS["motorsport"]
    if any(k in t for k in ["athletics", "điền kinh", "dien kinh", "marathon"]):
        return SPORT_LOGOS["athletics"]
    if any(k in t for k in ["swimming", "bơi", "boi loi"]):
        return SPORT_LOGOS["swimming"]
    if any(k in t for k in ["martial", "judo", "taekwondo", "karate", "wrestling", "wwe"]):
        return SPORT_LOGOS["martialarts"]
    if any(k in t for k in ["cycling", "xe đạp", "xe dap", "tour de"]):
        return SPORT_LOGOS["cycling"]
    if any(k in t for k in ["hockey", "ice hockey", "khúc côn cầu"]):
        return SPORT_LOGOS["hockey"]
    return SPORT_LOGOS["football"]

# ══════════════════════════════════════════════════════════════════════════════
#  TieuLam TV — 3-tier fetch (cache → relay → direct API)
# ══════════════════════════════════════════════════════════════════════════════

_TIEULAM_UA  = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_TIEULAM_HTTPX_HEADERS = {
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.7",
    "Content-Type":    "application/json",
    "Referer":         TIEULAM_FRONTEND_URL + "/",
    "Origin":          TIEULAM_FRONTEND_URL,
    "User-Agent":      _TIEULAM_UA,
}

_TIEULAM_SPORT_VI = {
    "FOOTBALL":    ("Bóng đá",   SPORT_LOGOS["football"]),
    "TENNIS":      ("Tennis",    SPORT_LOGOS["tennis"]),
    "BASKETBALL":  ("Bóng rổ",  SPORT_LOGOS["basketball"]),
    "VOLLEYBALL":  ("Bóng chuyền", SPORT_LOGOS["volleyball"]),
    "BADMINTON":   ("Cầu lông",  SPORT_LOGOS["badminton"]),
    "BILLIARDS":   ("Bi-a",      SPORT_LOGOS["billiards"]),
    "BOXING":      ("Boxing",    SPORT_LOGOS["boxing"]),
    "GOLF":        ("Golf",      SPORT_LOGOS["golf"]),
    "ESPORT":      ("Esport",    SPORT_LOGOS["esport"]),
    "MOTORSPORT":  ("Motorsport",SPORT_LOGOS["motorsport"]),
}

def _discover_tieulam_api_base(scraper) -> str:
    """Quét JS bundle của frontend để tìm API base URL hiện tại."""
    try:
        r = scraper.get(TIEULAM_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        for js_path in js_files[:3]:
            js = scraper.get(TIEULAM_FRONTEND_URL.rstrip("/") + js_path, timeout=20).text
            for pat in [
                r'create\(\{baseURL:"(https://[^"]+)"\}',
                r'baseURL:"(https://[^"]{10,60})"',
            ]:
                hits = re.findall(pat, js)
                if hits:
                    return hits[0].rstrip("/")
    except Exception as e:
        print(f"[tieulam] API discovery error: {e}", flush=True)
    return TIEULAM_KNOWN_API_BASE


def _get_tieulam_api_url() -> str:
    now = time.time()
    if now - _tieulam_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        sc = cloudscraper.create_scraper()
        discovered = _discover_tieulam_api_base(sc)
        _tieulam_api_cache["url"] = discovered + "/matches/graph"
        _tieulam_api_cache["discovered_at"] = now
        print(f"[tieulam] API URL updated: {_tieulam_api_cache['url']}", flush=True)
    return _tieulam_api_cache["url"]


def _fetch_tieulam_from_cache() -> list:
    """Tầng 1: GitHub Actions cache (cập nhật mỗi 30 phút)."""
    if not TIEULAM_CACHE_URL:
        raise ValueError("TIEULAM_CACHE_URL not set")
    r = requests.get(TIEULAM_CACHE_URL, timeout=10)
    r.raise_for_status()
    payload = r.json()
    fetched_at = payload.get("fetched_at", 0)
    age = int(time.time()) - fetched_at
    if age > TIEULAM_CACHE_MAX_AGE:
        raise ValueError(f"Cache quá cũ: {age}s (max {TIEULAM_CACHE_MAX_AGE}s)")
    data = payload.get("data") or payload.get("matches") or []
    if not data:
        raise ValueError("Cache rỗng")
    return data


def _fetch_tieulam_via_relay() -> list:
    """Tầng 2: Relay URL (Cloudflare Workers / Replit)."""
    headers: dict = {}
    if TIEULAM_RELAY_SECRET:
        headers["X-Relay-Token"] = TIEULAM_RELAY_SECRET
    last_err: Exception = ValueError("No relay URL configured")
    for url in filter(None, [TIEULAM_RELAY_URL, TIEULAM_RELAY_URL_2]):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            rdata = resp.json()
            if "error" in rdata:
                raise ValueError(f"Relay error: {rdata['error']}")
            if "data" not in rdata:
                raise ValueError(f"Relay format error: {list(rdata.keys())[:5]}")
            return rdata["data"]
        except Exception as e:
            print(f"[tieulam] Relay {url} failed: {e}", flush=True)
            last_err = e
    raise last_err


def _fetch_tieulam_matches() -> list:
    """3-tier fetch: cache → relay → direct API."""
    # Tầng 1 — GitHub Actions cache
    try:
        data = _fetch_tieulam_from_cache()
        print(f"[tieulam] ✅ Cache: {len(data)} matches", flush=True)
        return data
    except Exception as e:
        print(f"[tieulam] ⚠️ Cache miss: {e}", flush=True)

    # Tầng 2 — Relay
    if TIEULAM_RELAY_URL or TIEULAM_RELAY_URL_2:
        try:
            data = _fetch_tieulam_via_relay()
            print(f"[tieulam] ✅ Relay: {len(data)} matches", flush=True)
            return data
        except Exception as e:
            print(f"[tieulam] ⚠️ Relay failed: {e}", flush=True)

    # Tầng 3 — Direct API (dùng cloudscraper để bypass Cloudflare)
    now = time.time()
    cutoff     = (datetime.now(timezone.utc) - timedelta(seconds=7200)).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_end = (datetime.now(timezone.utc) + timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S")
    payload = {
        "queries": [
            {"field": "start_date", "type": "gte", "value": cutoff},
            {"field": "start_date", "type": "lte", "value": cutoff_end},
        ],
        "query_and": True, "limit": 100, "page": 1, "order_asc": "start_date",
    }
    scraper = cloudscraper.create_scraper()
    fallback_apis = [
        _get_tieulam_api_url(),
        "https://api.tlap17062026.com/matches/graph",
        "https://api.tlap12062026.xyz/matches/graph",
    ]
    for api_url in fallback_apis:
        try:
            resp = scraper.post(api_url, json=payload, headers=_TIEULAM_HTTPX_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if data:
                print(f"[tieulam] ✅ Direct API ({api_url}): {len(data)} matches", flush=True)
                return data
        except Exception as e:
            print(f"[tieulam] ⚠️ Direct API {api_url} failed: {e}", flush=True)
            _tieulam_api_cache["discovered_at"] = 0  # force rediscovery

    print("[tieulam] ❌ All 3 tiers failed", flush=True)
    return []


def _tieulam_logo(match: dict) -> str:
    desc = (match.get("desc") or "").upper()
    sport_info = _TIEULAM_SPORT_VI.get(desc)
    if sport_info:
        return sport_info[1]
    return _logo_from_text(desc + " " + match.get("league", ""))


def _tieulam_sport_label(match: dict) -> str:
    desc = (match.get("desc") or "").upper()
    sport_info = _TIEULAM_SPORT_VI.get(desc)
    if sport_info:
        return sport_info[0]
    return desc.capitalize() if desc else ""


def _build_tieulam_lines(matches: list) -> list:
    """Tạo M3U lines cho TieuLam TV — stream URLs đi qua /proxy/m3u (PA2+PA3)."""
    lines  = []
    pub    = _get_public_url()
    tl_ref = TIEULAM_FRONTEND_URL.rstrip("/") + "/"

    # Header params để proxy truyền đúng Referer/Origin đến CDN
    hq_params = (
        f"h_{quote('User-Agent')}={quote(_TIEULAM_UA)}"
        f"&h_{quote('Referer')}={quote(tl_ref)}"
        f"&h_{quote('Origin')}={quote(TIEULAM_FRONTEND_URL)}"
    )

    for match in matches:
        source_live = (match.get("source_live") or "").strip()
        blv         = (match.get("blv") or "").strip()
        stream_key  = (match.get("stream_key") or "").strip()

        if source_live:
            raw_url = source_live
        elif blv and stream_key:
            raw_url = f"{TIEULAM_STREAM_CDN}/live/{stream_key}/playlist.m3u8"
        else:
            continue

        # Lọc theo thời gian
        start_str = match.get("start_date", "")
        is_live   = bool(match.get("is_live"))
        if start_str and not is_live:
            try:
                dt_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if dt_start.tzinfo is None:
                    dt_start = dt_start.replace(tzinfo=timezone.utc)
                elapsed = time.time() - dt_start.timestamp()
                if blv:
                    if elapsed < -259200:  # chưa đến 72h trước
                        continue
                else:
                    if elapsed < 0:
                        continue
                if elapsed > MATCH_MAX_AGE_SECONDS:
                    continue
            except Exception:
                pass

        logo   = _tieulam_logo(match)
        team1  = match.get("team_1", "Home").strip()
        team2  = match.get("team_2", "Away").strip()
        league = match.get("league", "").strip()
        blv    = (match.get("blv") or "").strip()
        sport  = _tieulam_sport_label(match)

        try:
            dt_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if dt_start.tzinfo is None:
                dt_start = dt_start.replace(tzinfo=timezone.utc)
            dt_vn    = dt_start.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"

        suffix = blv if blv else sport
        if suffix:
            display = f"{time_str} - {date_str} | {team1} VS {team2} ({league}) | {suffix}"
        else:
            display = f"{time_str} - {date_str} | {team1} VS {team2} ({league})"

        # ── PA2: Wrap qua proxy nội bộ → CDN nhận đúng Referer ──
        proxied_url = f"{pub}/proxy/m3u?url={quote(raw_url, safe='')}&{hq_params}"

        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="TieuLam TV",{display}')
        lines.append(proxied_url)
    return lines

# ══════════════════════════════════════════════════════════════════════════════
#  Cola TV — API discovery + fetch
# ══════════════════════════════════════════════════════════════════════════════

def _discover_colatv_api(scraper) -> str:
    try:
        r = scraper.get(COLATV_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            return COLATV_KNOWN_API_URL
        js = scraper.get(COLATV_FRONTEND_URL.rstrip("/") + js_files[0], timeout=15).text
        hits = re.findall(r'https://[a-z0-9\-\.]+/api/match[^"\'`\s]{0,30}', js)
        for hit in hits:
            base = re.match(r'(https://[a-z0-9\-\.]+)/api/', hit)
            if base:
                return base.group(1) + "/api/matches"
    except Exception:
        pass
    return COLATV_KNOWN_API_URL

def _get_colatv_api_url(scraper) -> str:
    now = time.time()
    if now - _colatv_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        _colatv_api_cache["url"] = _discover_colatv_api(scraper)
        _colatv_api_cache["discovered_at"] = now
    return _colatv_api_cache["url"]

def _fetch_colatv_matches() -> dict:
    scraper = cloudscraper.create_scraper()
    api_url = _get_colatv_api_url(scraper)
    try:
        resp = scraper.get(api_url, timeout=15)
        resp.raise_for_status()
    except Exception:
        _colatv_api_cache["discovered_at"] = 0
        api_url = _get_colatv_api_url(scraper)
        resp = scraper.get(api_url, timeout=15)
        resp.raise_for_status()
    return resp.json().get("data", {})

def _colatv_is_active(match: dict) -> bool:
    if match.get("matchStatus") in COLATV_FINISHED_STATUS_INT:
        return False
    for field in ("match_status", "status", "matchStatusStr"):
        if str(match.get(field, "")).lower().strip() in FINISHED_STATUS_STRINGS:
            return False
    if match.get("isEnd") or match.get("isFinished"):
        return False
    match_time = match.get("matchTime", 0)
    is_live = bool(match.get("isLive") or match.get("living"))
    if match_time and not is_live:
        if (time.time() - match_time) > MATCH_MAX_AGE_SECONDS:
            return False
    return True

def _cola_logo(match: dict) -> str:
    parts = [
        match.get("competitionName", ""),
        match.get("homeTeamName", ""),
        match.get("awayTeamName", ""),
    ]
    return _logo_from_text(" ".join(p for p in parts if p))

def _build_colatv_lines(matches: dict) -> list:
    lines = []
    for match in matches.values():
        if not _colatv_is_active(match):
            continue
        logo        = _cola_logo(match)
        match_time  = match.get("matchTime", 0)
        home        = match.get("homeTeamName", "Home")
        away        = match.get("awayTeamName", "Away")
        competition = match.get("competitionName", "")
        dt          = datetime.fromtimestamp(match_time, tz=VN_TZ)
        time_str    = dt.strftime("%H:%M")
        date_str    = dt.strftime("%d/%m")
        anchors = match.get("anchorAppointmentVoList", [])
        if anchors:
            for anchor in anchors:
                stream_url = anchor.get("playStreamAddress2") or anchor.get("playStreamAddress", "")
                if not stream_url:
                    continue
                commentator = anchor.get("nickName", "").strip()
                display = f"{time_str} - {date_str} | {home} VS {away} ({competition}) | {commentator}"
                lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="Cola tv",{display}')
                lines.append(stream_url)
        else:
            stream_url = match.get("videoUrl", "")
            if not stream_url:
                continue
            display = f"{time_str} - {date_str} | {home} VS {away} ({competition})"
            lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="Cola tv",{display}')
            lines.append(stream_url)
    return lines

# ══════════════════════════════════════════════════════════════════════════════
#  Hội Quán TV — API discovery + fetch
# ══════════════════════════════════════════════════════════════════════════════

_HQ_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
_HQ_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9",
    "User-Agent": _HQ_UA,
}

def _discover_hoiquan_api(scraper) -> str:
    try:
        r = scraper.get(HOIQUAN_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            js_files = re.findall(r'src="(/assets/js/[^"]+\.js)"', r.text)
        if not js_files:
            return HOIQUAN_KNOWN_API_BASE
        js = scraper.get(HOIQUAN_FRONTEND_URL.rstrip("/") + js_files[0], timeout=15).text
        hits = re.findall(r'VITE_SERVER_API_BASE_URL:"(https://[^"]+)"', js)
        if hits:
            return hits[0]
        hits = re.findall(r'https://sv\.[a-z0-9\-\.]+/api/v1/external', js)
        if hits:
            return hits[0]
    except Exception:
        pass
    return HOIQUAN_KNOWN_API_BASE

def _get_hoiquan_api_base(scraper) -> str:
    now = time.time()
    if now - _hoiquan_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        _hoiquan_api_cache["url"] = _discover_hoiquan_api(scraper)
        _hoiquan_api_cache["discovered_at"] = now
    return _hoiquan_api_cache["url"]

def _fetch_hoiquan_fixtures() -> list:
    scraper = cloudscraper.create_scraper()
    api_base = _get_hoiquan_api_base(scraper)
    url = api_base.rstrip("/") + "/fixtures/unfinished"
    headers = {**_HQ_HEADERS, "Referer": HOIQUAN_FRONTEND_URL + "/"}
    try:
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception:
        _hoiquan_api_cache["discovered_at"] = 0
        api_base = _get_hoiquan_api_base(scraper)
        url = api_base.rstrip("/") + "/fixtures/unfinished"
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        return []
    return data.get("data", [])

# ══════════════════════════════════════════════════════════════════════════════
#  Khán Đài A — API discovery + fetch
# ══════════════════════════════════════════════════════════════════════════════

def _discover_khandaia_api(scraper) -> str:
    try:
        r = scraper.get(KHANDAIA_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            return KHANDAIA_KNOWN_API_BASE
        for js_path in js_files:
            js = scraper.get(KHANDAIA_FRONTEND_URL.rstrip("/") + js_path, timeout=20).text
            chunk_paths = re.findall(r'assets/queries[^"\']+\.js', js)
            for cp in chunk_paths[:2]:
                chunk = scraper.get(KHANDAIA_FRONTEND_URL.rstrip("/") + "/" + cp, timeout=15).text
                hits = re.findall(r'https://sv\.[a-z0-9\-\.]+/api/v1/external', chunk)
                if hits:
                    return hits[0]
            hits = re.findall(r'https://sv\.[a-z0-9\-\.]+/api/v1/external', js)
            if hits:
                return hits[0]
    except Exception:
        pass
    return KHANDAIA_KNOWN_API_BASE

def _get_khandaia_api_base(scraper) -> str:
    now = time.time()
    if now - _khandaia_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        _khandaia_api_cache["url"] = _discover_khandaia_api(scraper)
        _khandaia_api_cache["discovered_at"] = now
    return _khandaia_api_cache["url"]

def _fetch_khandaia_fixtures() -> list:
    scraper = cloudscraper.create_scraper()
    api_base = _get_khandaia_api_base(scraper)
    url = api_base.rstrip("/") + "/fixtures/unfinished"
    headers = {**_HQ_HEADERS, "Referer": KHANDAIA_FRONTEND_URL + "/"}
    try:
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception:
        _khandaia_api_cache["discovered_at"] = 0
        api_base = _get_khandaia_api_base(scraper)
        url = api_base.rstrip("/") + "/fixtures/unfinished"
        resp = scraper.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        return []
    return data.get("data", [])

# ══════════════════════════════════════════════════════════════════════════════
#  Vòng Cấm TV — token discovery + fetch
# ══════════════════════════════════════════════════════════════════════════════

def _discover_vongcam_token(scraper) -> str:
    try:
        r = scraper.get(VONGCAM_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        for js_path in js_files[:3]:
            js = scraper.get(VONGCAM_FRONTEND_URL.rstrip("/") + js_path, timeout=15).text
            hits = re.findall(r'(?:token|TOKEN|apiToken)["\s:=]+["\']([A-Za-z0-9]{4,32})["\']', js)
            if hits:
                return hits[0]
    except Exception:
        pass
    return VONGCAM_FALLBACK_TOKEN

def _get_vongcam_token(scraper) -> str:
    now = time.time()
    if now - _vongcam_token_cache["discovered_at"] > API_DISCOVERY_TTL:
        _vongcam_token_cache["token"] = _discover_vongcam_token(scraper)
        _vongcam_token_cache["discovered_at"] = now
    return _vongcam_token_cache["token"]

def _fetch_vongcam_matches() -> list:
    scraper = cloudscraper.create_scraper()
    token = _get_vongcam_token(scraper)
    headers = {
        **_HQ_HEADERS,
        "Authorization": f"Bearer {token}",
        "Referer": VONGCAM_FRONTEND_URL,
    }
    api_url = _vongcam_api_cache["url"]
    try:
        resp = scraper.get(api_url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception:
        _vongcam_token_cache["discovered_at"] = 0
        token = _get_vongcam_token(scraper)
        headers["Authorization"] = f"Bearer {token}"
        resp = scraper.get(api_url, headers=headers, timeout=15)
        resp.raise_for_status()
    data = resp.json()
    return data.get("data", data) if isinstance(data, dict) else data

def _vongcam_is_active(match: dict) -> bool:
    status = str(match.get("status") or "").lower().strip()
    if status in FINISHED_STATUS_STRINGS:
        return False
    if match.get("isFinished") or match.get("isEnd"):
        return False
    start_str = match.get("startTime", "")
    is_live   = bool(match.get("isLive"))
    if start_str and not is_live:
        try:
            if "+" not in start_str and not start_str.endswith("Z"):
                start_str += "+07:00"
            dt = datetime.fromisoformat(start_str)
            elapsed = time.time() - dt.timestamp()
            if elapsed > MATCH_MAX_AGE_SECONDS:
                return False
        except Exception:
            pass
    return True

def _pick_vongcam_stream(commentator: dict) -> str:
    for q in ("fhd", "hd", "sd"):
        for s in (commentator.get("streams") or []):
            if s.get("name", "").lower() == q and s.get("sourceUrl"):
                return s["sourceUrl"]
    for s in (commentator.get("streams") or []):
        if s.get("sourceUrl"):
            return s["sourceUrl"]
    return ""

def _vongcam_logo(match: dict) -> str:
    parts = [
        match.get("tournamentName", ""),
        match.get("homeClub", {}).get("name", ""),
        match.get("awayClub", {}).get("name", ""),
        match.get("title", ""),
        match.get("slug", ""),
    ]
    tags = match.get("tags") or []
    if isinstance(tags, list):
        parts.extend(str(t) for t in tags)
    return _logo_from_text(" ".join(p for p in parts if p))

def _build_vongcam_lines(matches: list) -> list:
    lines = []
    try:
        matches = sorted(matches, key=lambda m: m.get("startTime") or "")
    except Exception:
        pass
    for match in matches:
        if not _vongcam_is_active(match):
            continue
        home       = match.get("homeClub", {}).get("name", "Home").strip()
        away       = match.get("awayClub", {}).get("name", "Away").strip()
        tournament = match.get("tournamentName", "")
        logo       = _vongcam_logo(match)
        start_str  = match.get("startTime", "")
        try:
            if "+" not in start_str and not start_str.endswith("Z"):
                start_str += "+07:00"
            dt       = datetime.fromisoformat(start_str)
            dt_vn    = dt.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"
        commentator = match.get("commentator")
        if not commentator:
            continue
        stream_url = _pick_vongcam_stream(commentator)
        if not stream_url:
            continue
        nickname = (commentator.get("nickname") or "").strip()
        display  = f"{time_str} - {date_str} | {home} VS {away} ({tournament}) | {nickname}"
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="Vòng Cấm TV",{display}')
        _vc_ref = VONGCAM_FRONTEND_URL.rstrip("/") + "/"
        _vc_url = stream_url + (f"|Referer={_vc_ref}&User-Agent=Mozilla/5.0" if "|" not in stream_url else "")
        lines.append(_vc_url)
    return lines

# ══════════════════════════════════════════════════════════════════════════════
#  Dekiki — static GitHub M3U fetch + parse
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_dekiki_lines() -> list:
    """Download the GitHub-hosted M3U, strip its header, return raw lines."""
    resp = requests.get(DEKIKI_M3U_URL, timeout=20)
    resp.raise_for_status()
    lines = []
    for line in resp.text.splitlines():
        stripped = line.rstrip()
        if not stripped or stripped.startswith("#EXTM3U"):
            continue
        lines.append(stripped)
    return lines

# ══════════════════════════════════════════════════════════════════════════════
#  Shared fixture helpers  (Hội Quán TV + Khán Đài A use same schema)
# ══════════════════════════════════════════════════════════════════════════════

def _fixture_is_active(fixture: dict) -> bool:
    status = str(fixture.get("status") or "").lower().strip()
    if status in FINISHED_STATUS_STRINGS:
        return False
    if fixture.get("isFinished") or fixture.get("isEnd"):
        return False
    is_live        = bool(fixture.get("isLive"))
    start_time_str = fixture.get("startTime", "")
    if start_time_str and not is_live:
        try:
            dt      = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            elapsed = time.time() - dt.timestamp()
            if elapsed > MATCH_MAX_AGE_SECONDS:
                return False
            if status == "active" and elapsed > 5400:
                return False
        except Exception:
            pass
    return True

def _pick_best_stream(streams: list) -> str:
    for quality in ("fhd", "hd", "sd"):
        for s in streams:
            if s.get("name", "").lower() == quality:
                url = s.get("sourceUrl", "")
                if url:
                    return url
    for s in streams:
        url = s.get("sourceUrl", "")
        if url:
            return url
    return ""

def _hq_kda_logo(fixture: dict) -> str:
    parts = [
        fixture.get("league", {}).get("name", ""),
        fixture.get("homeTeam", {}).get("name", ""),
        fixture.get("awayTeam", {}).get("name", ""),
    ]
    return _logo_from_text(" ".join(p for p in parts if p))

def _build_fixture_lines(fixtures: list, group_title: str) -> list:
    try:
        fixtures = sorted(fixtures, key=lambda f: f.get("startTime") or "")
    except Exception:
        pass
    lines = []
    _referer_map = {
        "Hội Quán TV": HOIQUAN_FRONTEND_URL.rstrip("/") + "/",
        "Khán Đài A":  KHANDAIA_FRONTEND_URL.rstrip("/") + "/",
    }
    for fixture in fixtures:
        if not _fixture_is_active(fixture):
            continue
        logo      = _hq_kda_logo(fixture)
        start_str = fixture.get("startTime", "")
        home      = fixture.get("homeTeam", {}).get("name", "Home").strip()
        away      = fixture.get("awayTeam", {}).get("name", "Away").strip()
        league    = fixture.get("league", {}).get("name", "")
        try:
            dt      = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            dt_vn   = dt.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"
        for entry in fixture.get("fixtureCommentators", []):
            commentator_obj = entry.get("commentator", {})
            name = (commentator_obj.get("nickname") or commentator_obj.get("name") or "").strip()
            stream_url = _pick_best_stream(commentator_obj.get("streams", []))
            if not stream_url:
                continue
            display = f"{time_str} - {date_str} | {home} VS {away} ({league}) | {name}"
            lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group_title}",{display}')
            _ref = _referer_map.get(group_title, "")
            _final_url = stream_url + (f"|Referer={_ref}&User-Agent=Mozilla/5.0" if _ref and "|" not in stream_url else "")
            lines.append(_final_url)
    return lines

# ══════════════════════════════════════════════════════════════════════════════
#  Cache helpers — build compressed + ETag
# ══════════════════════════════════════════════════════════════════════════════

def _pack(text: str) -> dict:
    raw  = text.encode("utf-8")
    gz   = gzip.compress(raw, compresslevel=6)
    etag = '"' + hashlib.md5(raw).hexdigest() + '"'
    return {"content": raw, "gz": gz, "etag": etag, "built_at": time.time()}

def _store(key: str, text: str):
    packed = _pack(text)
    entry  = _playlist_cache[key]
    with entry["lock"]:
        entry.update(packed)

# ══════════════════════════════════════════════════════════════════════════════
#  Background pre-fetch (parallel, 6 sources)
# ══════════════════════════════════════════════════════════════════════════════

def _refresh_all_playlists():
    # Tự động follow redirect để cập nhật domain thực tế
    _resolve_all_frontends()
    errors = []

    def fetch_tieulam():
        return _build_tieulam_lines(_fetch_tieulam_matches())

    def fetch_cola():
        return _build_colatv_lines(_fetch_colatv_matches())

    def fetch_hq():
        return _build_fixture_lines(_fetch_hoiquan_fixtures(), "Hội Quán TV")

    def fetch_kda():
        return _build_fixture_lines(_fetch_khandaia_fixtures(), "Khán Đài A")

    def fetch_vc():
        return _build_vongcam_lines(_fetch_vongcam_matches())

    def fetch_dekiki():
        return _fetch_dekiki_lines()

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(fetch_tieulam): "tieulam",
            ex.submit(fetch_cola):    "cola",
            ex.submit(fetch_hq):      "hoiquan",
            ex.submit(fetch_kda):     "khandaia",
            ex.submit(fetch_vc):      "vongcam",
            ex.submit(fetch_dekiki):  "dekiki",
        }
        results = {}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = []
                errors.append(f"{key}: {e}")

    tieulam_lines = results.get("tieulam",  [])
    cola_lines    = results.get("cola",     [])
    hq_lines      = results.get("hoiquan",  [])
    kda_lines     = results.get("khandaia", [])
    vc_lines      = results.get("vongcam",  [])
    dekiki_lines  = results.get("dekiki",   [])

    err_str = "; ".join(errors)

    def count(lines):
        return sum(1 for l in lines if l.startswith("#EXTINF"))

    epg_header = f'#EXTM3U url-tvg="{EPG_URL}" x-tvg-url="{EPG_URL}"'

    _store("tieulam",  epg_header + "\n" + "\n".join(tieulam_lines))
    _store("cola",     epg_header + "\n" + "\n".join(cola_lines))
    _store("hoiquan",  epg_header + "\n" + "\n".join(hq_lines))
    _store("khandaia", epg_header + "\n" + "\n".join(kda_lines))
    _store("vongcam",  epg_header + "\n" + "\n".join(vc_lines))
    _store("dekiki",   epg_header + "\n" + "\n".join(dekiki_lines))

    # TieuLam đứng đầu combined playlist
    all_lines = tieulam_lines + cola_lines + hq_lines + kda_lines + vc_lines + dekiki_lines
    combined_text = epg_header + "\n" + "\n".join(all_lines)
    if err_str:
        combined_text += f"\n# Errors: {err_str}"
    _store("combined", combined_text)

    _last_counts.update({
        "tieulam":      count(tieulam_lines),
        "cola":         count(cola_lines),
        "hoiquan":      count(hq_lines),
        "khandaia":     count(kda_lines),
        "vongcam":      count(vc_lines),
        "dekiki":       count(dekiki_lines),
        "refreshed_at": time.time(),
        "last_error":   err_str,
    })

def _prefetch_loop():
    time.sleep(3)
    while True:
        try:
            _refresh_all_playlists()
        except Exception:
            pass
        time.sleep(PREFETCH_INTERVAL)

def _get_entry(key: str):
    entry = _playlist_cache[key]
    with entry["lock"]:
        return dict(entry)

# ══════════════════════════════════════════════════════════════════════════════
#  Flask routes
# ══════════════════════════════════════════════════════════════════════════════

def _m3u_response(key: str, filename: str) -> Response:
    entry = _get_entry(key)

    if entry["content"] is None:
        try:
            _refresh_all_playlists()
            entry = _get_entry(key)
        except Exception as e:
            return Response(f"Error: {e}", status=500, mimetype="text/plain")

    etag = entry["etag"]
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)

    accept_enc = request.headers.get("Accept-Encoding", "")
    use_gzip   = "gzip" in accept_enc and entry["gz"] is not None

    body = entry["gz"] if use_gzip else entry["content"]

    resp = Response(body, mimetype="application/x-mpegurl")
    resp.headers["ETag"]                = etag
    resp.headers["Cache-Control"]       = f"public, max-age={PREFETCH_INTERVAL}"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Vary"]                = "Accept-Encoding"
    if use_gzip:
        resp.headers["Content-Encoding"] = "gzip"
    return resp

@app.route("/live.m3u")
def live_m3u():
    return _m3u_response("combined", "live.m3u")

@app.route("/tieulam.m3u")
def tieulam_m3u():
    return _m3u_response("tieulam", "tieulam.m3u")

@app.route("/cola.m3u")
def cola_m3u():
    return _m3u_response("cola", "cola.m3u")

@app.route("/hoiquan.m3u")
def hoiquan_m3u():
    return _m3u_response("hoiquan", "hoiquan.m3u")

@app.route("/khandaia.m3u")
def khandaia_m3u():
    return _m3u_response("khandaia", "khandaia.m3u")

@app.route("/vongcam.m3u")
def vongcam_m3u():
    return _m3u_response("vongcam", "vongcam.m3u")

@app.route("/dekiki.m3u")
def dekiki_m3u():
    return _m3u_response("dekiki", "dekiki.m3u")

@app.route("/status.json")
def status_json():
    from flask import jsonify
    ra    = _last_counts.get("refreshed_at", 0)
    ra_vn = datetime.fromtimestamp(ra, tz=VN_TZ).strftime("%H:%M:%S %d/%m/%Y") if ra else None
    next_s = max(int(PREFETCH_INTERVAL - (time.time() - ra)), 0) if ra else None
    return jsonify({
        "ok":           True,
        "refreshed_at": ra_vn,
        "next_refresh_in_seconds": next_s,
        "last_error":   _last_counts.get("last_error", ""),
        "proxy_pool":   len(_PROXY_LIST),
        "channels": {
            "total":       sum(_last_counts.get(k, 0) for k in ("tieulam","cola","hoiquan","khandaia","vongcam","dekiki")),
            "tieulam_tv":  _last_counts.get("tieulam",  0),
            "cola_tv":     _last_counts.get("cola",     0),
            "hoiquan_tv":  _last_counts.get("hoiquan",  0),
            "khandai_a":   _last_counts.get("khandaia", 0),
            "vongcam_tv":  _last_counts.get("vongcam",  0),
            "dekiki_tv":   _last_counts.get("dekiki",   0),
        },
        "sources": {
            "tieulam_tv": {"api": _tieulam_api_cache.get("url"),  "status": "ok" if _last_counts.get("tieulam",0) > 0 else "empty"},
            "cola_tv":    {"api": _colatv_api_cache.get("url"),   "status": "ok" if _last_counts.get("cola",0)    > 0 else "empty"},
            "hoiquan_tv": {"api": _hoiquan_api_cache.get("url"),  "status": "ok" if _last_counts.get("hoiquan",0) > 0 else "empty"},
            "khandai_a":  {"api": _khandaia_api_cache.get("url"), "status": "ok" if _last_counts.get("khandaia",0)> 0 else "empty"},
            "vongcam_tv": {"api": _vongcam_api_cache.get("url"),  "token": _vongcam_token_cache.get("token"), "status": "ok" if _last_counts.get("vongcam",0) > 0 else "empty"},
            "dekiki_tv":  {"api": "github-static",                "status": "ok" if _last_counts.get("dekiki",0)  > 0 else "empty"},
        },
    })

@app.route("/ping")
def ping():
    return Response("OK", mimetype="text/plain")

@app.route("/")
def index():
    ra = _last_counts.get("refreshed_at", 0)
    if ra:
        dt_str   = datetime.fromtimestamp(ra, tz=VN_TZ).strftime("%H:%M:%S %d/%m/%Y")
        next_s   = max(int(PREFETCH_INTERVAL - (time.time() - ra)), 0)
        next_str = f"{next_s}s"
    else:
        dt_str   = "chưa có dữ liệu"
        next_str = "đang khởi động..."

    err      = _last_counts.get("last_error", "")
    err_html = f"<p style='color:red'>⚠️ Lỗi: {err}</p>" if err else ""

    tieulam_count = _last_counts.get("tieulam",  0)
    cola_count    = _last_counts.get("cola",     0)
    hq_count      = _last_counts.get("hoiquan",  0)
    kda_count     = _last_counts.get("khandaia", 0)
    vc_count      = _last_counts.get("vongcam",  0)
    dekiki_count  = _last_counts.get("dekiki",   0)
    total         = tieulam_count + cola_count + hq_count + kda_count + vc_count + dekiki_count

    return Response(
        f"<h2>📺 IPTV Server</h2>"
        f"<p>🕐 Cập nhật lúc: <strong>{dt_str}</strong> | Làm mới sau: <strong>{next_str}</strong></p>"
        f"<p>📊 Tổng: <strong>{total} trận</strong></p>"
        f"<p>🎯 TieuLam TV: <strong>{tieulam_count} trận</strong>"
        f"&nbsp;|&nbsp; API: <code>{_tieulam_api_cache['url']}</code>"
        f"&nbsp;|&nbsp; Frontend: <code>{TIEULAM_FRONTEND_URL}</code></p>"
        f"<p>🥤 Cola TV: <strong>{cola_count} trận</strong>"
        f"&nbsp;|&nbsp; <code>{_colatv_api_cache['url']}</code></p>"
        f"<p>🏠 Hội Quán TV: <strong>{hq_count} trận</strong>"
        f"&nbsp;|&nbsp; <code>{_hoiquan_api_cache['url']}</code></p>"
        f"<p>🏟 Khán Đài A: <strong>{kda_count} trận</strong>"
        f"&nbsp;|&nbsp; <code>{_khandaia_api_cache['url']}</code></p>"
        f"<p>⚽ Vòng Cấm TV: <strong>{vc_count} trận</strong>"
        f"&nbsp;|&nbsp; <code>{_vongcam_api_cache['url']}</code>"
        f"&nbsp;|&nbsp; token: <code>{_vongcam_token_cache['token']}</code></p>"
        f"<p>📡 Kênh TV (dekiki): <strong>{dekiki_count} kênh</strong></p>"
        f"<p>🔀 Proxy pool: <strong>{len(_PROXY_LIST)} proxy</strong> (PA3 - rotation)</p>"
        f"<p>📻 EPG: <a href='{EPG_URL}' target='_blank'>{EPG_URL}</a></p>"
        f"{err_html}"
        "<h3>📋 Playlist endpoints</h3><ul>"
        "<li><a href='/live.m3u'>/live.m3u</a> — Tất cả nguồn</li>"
        "<li><a href='/tieulam.m3u'>/tieulam.m3u</a> — TieuLam TV (qua proxy)</li>"
        "<li><a href='/cola.m3u'>/cola.m3u</a> — Cola TV</li>"
        "<li><a href='/hoiquan.m3u'>/hoiquan.m3u</a> — Hội Quán TV</li>"
        "<li><a href='/khandaia.m3u'>/khandaia.m3u</a> — Khán Đài A</li>"
        "<li><a href='/vongcam.m3u'>/vongcam.m3u</a> — Vòng Cấm TV</li>"
        "<li><a href='/dekiki.m3u'>/dekiki.m3u</a> — Kênh TV tĩnh</li>"
        "</ul>"
        "<h3>⚙️ Tối ưu băng thông</h3><ul>"
        "<li>Gzip nén tự động (giảm ~70% dữ liệu truyền)</li>"
        "<li>ETag + HTTP 304 — client có cache không cần tải lại</li>"
        f"<li>Cache-Control: public, max-age={PREFETCH_INTERVAL}s</li>"
        "<li>1 worker process + 8 threads — cache dùng chung, không fetch trùng lặp</li>"
        "<li>6 nguồn fetch song song (ThreadPoolExecutor)</li>"
        f"<li>Làm mới cache mỗi <strong>{PREFETCH_INTERVAL // 60} phút</strong></li>"
        "<li>PA2: Stream TieuLam qua proxy nội bộ (đúng Referer/UA đến CDN)</li>"
        "<li>PA3: Proxy rotation pool (SOCKS5/HTTP) để bypass IP block</li>"
        "</ul>",
        mimetype="text/html",
    )

# ══════════════════════════════════════════════════════════════════════════════
#  Keep-alive self-ping
# ══════════════════════════════════════════════════════════════════════════════

def _get_ping_url() -> str:
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        return f"https://{domains.split(',')[0].strip()}/"
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render_url:
        return render_url.rstrip("/") + "/"
    app_url = os.environ.get("APP_URL", "")
    if app_url:
        return app_url.rstrip("/") + "/"
    return f"http://localhost:{os.environ.get('PORT', 5000)}/"

def _self_ping():
    url = _get_ping_url()
    while True:
        time.sleep(SELF_PING_INTERVAL)
        try:
            requests.get(url, timeout=15)
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
#  Startup
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    threading.Thread(target=_prefetch_loop, daemon=True).start()
    threading.Thread(target=_self_ping,     daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
