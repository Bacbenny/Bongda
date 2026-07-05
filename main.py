import gzip
import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urljoin

import cloudscraper
import requests
from flask import Flask, Response, request

app = Flask(__name__)

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

# ─── TieuLam Relay (Bongda/Render acts as relay for GitHub Actions) ─────────
RELAY_SECRET           = os.environ.get("RELAY_SECRET", "")
TIEULAM_FRONTEND_URL   = os.environ.get("TIEULAM_FRONTEND",  "https://sv2.tieulam1.xyz")
TIEULAM_KNOWN_API_BASE = os.environ.get("TIEULAM_API",        "https://api.tlap17062026.com")
TIEULAM_STREAM_CDN     = os.environ.get("TIEULAM_STREAM_CDN", "https://live.secufun.xyz").rstrip("/")
PUBLIC_BASE_URL        = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
TIEULAM_CF_STREAM_RELAY = os.environ.get(
    "TIEULAM_CF_STREAM_RELAY",
    "https://tieulam-relay.bacbenny95.workers.dev",
).rstrip("/")

# Referrer cho CDN asynccdn — khớp TIEULAM_FRONTEND (render.yaml: sv2.tieulam.info).
TIEULAM_UA          = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

def _tieulam_stream_referrer() -> str:
    return TIEULAM_FRONTEND_URL.rstrip("/") + "/"

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
_colatv_api_cache   = {"url": COLATV_KNOWN_API_URL,    "discovered_at": 0}
_hoiquan_api_cache  = {"url": HOIQUAN_KNOWN_API_BASE,  "discovered_at": 0}
_khandaia_api_cache = {"url": KHANDAIA_KNOWN_API_BASE, "discovered_at": 0}
_vongcam_api_cache  = {"url": VONGCAM_KNOWN_API_BASE,  "discovered_at": 0}

# ─── Auto domain resolution ───────────────────────────────────────────────────
def _resolve_base_url(url: str, timeout: int = 8) -> str:
    """Follow HTTP 3xx redirects và trả về scheme+host cuối cùng.
    Tự động phát hiện khi domain đổi (vd: khandaia.link → khandaia4.link).
    """
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
    """Tự động cập nhật HOIQUAN/KHANDAIA/VONGCAM _FRONTEND_URL bằng cách follow redirect.
    Gọi lúc đầu mỗi chu kỳ refresh để luôn dùng domain thực tế.
    """
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
_tieulam_bongda_cache = {"url": TIEULAM_KNOWN_API_BASE, "discovered_at": 0.0}
_tieulam_relay_cache  = {"data": None, "ts": 0.0}
_tieulam_stream_cache = {}  # match_id -> {upstream, hd1, hd2, hd3, probed_ok, ts}
_tieulam_scraper_local = threading.local()

# ─── Playlist content cache ───────────────────────────────────────────────────
# Each entry stores: raw bytes, gzip bytes, md5 etag, and build timestamp.
def _empty_entry():
    return {"content": None, "gz": None, "etag": None, "built_at": 0,
            "lock": threading.Lock()}

_playlist_cache = {
    "combined": _empty_entry(),
    "cola":     _empty_entry(),
    "hoiquan":  _empty_entry(),
    "khandaia": _empty_entry(),
    "vongcam":  _empty_entry(),
    "tieulam":  _empty_entry(),
    "dekiki":   _empty_entry(),
}

_last_counts = {
    "cola": 0, "hoiquan": 0, "khandaia": 0, "vongcam": 0, "tieulam": 0, "dekiki": 0,
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
    if any(k in t for k in ["formula", "f1 ", " f1", "motogp", "moto gp", "đua xe", "dua xe", "motorsport", "superbike", "wtcc"]):
        return SPORT_LOGOS["motorsport"]
    if any(k in t for k in ["athletics", "điền kinh", "dien kinh", "marathon", "chạy", "cha y"]):
        return SPORT_LOGOS["athletics"]
    if any(k in t for k in ["swim", "bơi lội", "boi loi", "aquatic"]):
        return SPORT_LOGOS["swimming"]
    if any(k in t for k in ["karate", "judo", "taekwondo", "wushu", "võ thuật", "vo thuat",
                              "wrestling", "kung fu", "wwe", "smackdown", "raw", "aew",
                              "impact", "muay thai", "kickboxing", "bjj"]):
        return SPORT_LOGOS["martialarts"]
    if any(k in t for k in ["cycl", "xe đạp", "xe dap", "velo"]):
        return SPORT_LOGOS["cycling"]
    if any(k in t for k in ["hockey", "khúc côn", "khuc con"]):
        return SPORT_LOGOS["hockey"]
    return SPORT_LOGOS["football"]

def _cola_logo(match: dict) -> str:
    parts = " ".join([
        match.get("competitionName", ""),
        match.get("sportType", ""),
        match.get("sport", ""),
        str(match.get("sportId", "")),
    ])
    return _logo_from_text(parts)

def _hq_kda_logo(fixture: dict) -> str:
    sport = fixture.get("sport") or {}
    icon = sport.get("iconUrl", "")
    if icon:
        return icon
    parts = " ".join([sport.get("name", ""), sport.get("slug", "")])
    return _logo_from_text(parts)

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

_HQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
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
#  Khán Đài A — API discovery + fetch  (same schema as Hội Quán TV)
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
#  Vòng Cấm TV — API discovery + token discovery + fetch
#  Frontend : https://sv2.vongcam3.live
#  Matches  : https://sv.bugiotv.xyz/internal/api/matches
#  Auth     : Access-Token header (token discovered from JS bundle, re-discovered hourly or on 401)
#  Schema   : {code, message, data: [{id, title, tournamentName,
#               homeClub:{name,logoUrl}, awayClub:{name,logoUrl},
#               startTime, isLive,
#               commentator:{nickname, streamSourceSd,
#                            streamSourceHd, streamSourceFhd}}]}
# ══════════════════════════════════════════════════════════════════════════════

def _discover_vongcam_api(scraper) -> str:
    """Re-discover bugiotv API base URL from the Vòng Cấm TV JS bundle."""
    try:
        r = scraper.get(VONGCAM_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            return VONGCAM_KNOWN_API_BASE
        for js_path in js_files:
            js = scraper.get(VONGCAM_FRONTEND_URL.rstrip("/") + js_path, timeout=20).text
            hits = re.findall(r'https?://[a-z0-9\-\.]+/internal/api/matches', js)
            if hits:
                return hits[0]
    except Exception:
        pass
    return VONGCAM_KNOWN_API_BASE

def _get_vongcam_api_base(scraper) -> str:
    now = time.time()
    if now - _vongcam_api_cache["discovered_at"] > API_DISCOVERY_TTL:
        _vongcam_api_cache["url"] = _discover_vongcam_api(scraper)
        _vongcam_api_cache["discovered_at"] = now
    return _vongcam_api_cache["url"]

def _discover_vongcam_token(scraper) -> str:
    """Re-discover the Vòng Cấm TV Access-Token from the JS bundle.
    Looks for common patterns like "AB321C" near access-token / accessToken keys,
    or a capitalized hex/alphanumeric literal embedded near the API call.
    Returns the discovered token, or the fallback if nothing found.
    """
    token_patterns = [
        r'["\']?(?:access[-_]?token|accessToken|token)["\']?\s*[:=]\s*["\']([A-Za-z0-9]{4,32})["\']',
        r'headers\s*\.\s*append\s*\(\s*["\']access-token["\']\s*,\s*["\']([A-Za-z0-9]{4,32})["\']',
        r'["\']Access-Token["\']\s*:\s*["\']([A-Za-z0-9]{4,32})["\']',
        r'["\']access-token["\']\s*:\s*["\']([A-Za-z0-9]{4,32})["\']',
    ]
    try:
        r = scraper.get(VONGCAM_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        for js_path in js_files:
            try:
                js = scraper.get(VONGCAM_FRONTEND_URL.rstrip("/") + js_path, timeout=20).text
            except Exception:
                continue
            for pat in token_patterns:
                hits = re.findall(pat, js, flags=re.IGNORECASE)
                for hit in hits:
                    # Skip obvious false positives (pure digits or too long)
                    if hit.isalpha() or hit.isalnum() and 4 <= len(hit) <= 32:
                        return hit
    except Exception:
        pass
    return VONGCAM_FALLBACK_TOKEN

def _get_vongcam_token(scraper) -> str:
    now = time.time()
    if now - _vongcam_token_cache["discovered_at"] > API_DISCOVERY_TTL:
        new_token = _discover_vongcam_token(scraper)
        if new_token:
            _vongcam_token_cache["token"] = new_token
        _vongcam_token_cache["discovered_at"] = now
    return _vongcam_token_cache["token"]

def _vongcam_request(scraper, api_url: str, headers: dict):
    """GET with current token; on 401 re-discover token once and retry."""
    resp = scraper.get(api_url, headers=headers, timeout=15)
    if resp.status_code == 401:
        # Force token re-discovery then retry exactly once.
        _vongcam_token_cache["discovered_at"] = 0
        new_token = _get_vongcam_token(scraper)
        headers = {**headers, "Access-Token": new_token}
        resp = scraper.get(api_url, headers=headers, timeout=15)
    return resp

def _fetch_vongcam_matches() -> list:
    scraper = cloudscraper.create_scraper()
    api_url = _get_vongcam_api_base(scraper)
    token   = _get_vongcam_token(scraper)
    headers = {
        **_HQ_HEADERS,
        "Referer":      VONGCAM_FRONTEND_URL + "/",
        "Origin":       VONGCAM_FRONTEND_URL,
        "Access-Token": token,
    }
    try:
        resp = _vongcam_request(scraper, api_url, headers)
        resp.raise_for_status()
    except Exception:
        # Re-discover both URL and token, then retry once.
        _vongcam_api_cache["discovered_at"]   = 0
        _vongcam_token_cache["discovered_at"] = 0
        api_url = _get_vongcam_api_base(scraper)
        token   = _get_vongcam_token(scraper)
        headers = {
            **_HQ_HEADERS,
            "Referer":      VONGCAM_FRONTEND_URL + "/",
            "Origin":       VONGCAM_FRONTEND_URL,
            "Access-Token": token,
        }
        resp = _vongcam_request(scraper, api_url, headers)
        resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        return []
    return data.get("data", [])

def _vongcam_is_active(match: dict) -> bool:
    if bool(match.get("isLive")):
        return True
    start_str = match.get("startTime", "")
    if start_str:
        try:
            if "+" not in start_str and not start_str.endswith("Z"):
                start_str += "+07:00"  # bugiotv API trả startTime theo giờ VN (UTC+7)
            dt      = datetime.fromisoformat(start_str)
            elapsed = time.time() - dt.timestamp()
            if elapsed < MATCH_MAX_AGE_SECONDS:
                return True
        except Exception:
            pass
    return False

def _pick_vongcam_stream(commentator: dict) -> str:
    for key in ("streamSourceFhd", "streamSourceHd", "streamSourceSd"):
        url = (commentator.get(key) or "").strip()
        if url:
            return url
    return ""

def _vongcam_logo(match: dict) -> str:
    """Logo cho Vòng Cấm TV.
    bugiotv API không có sport-type field riêng → ghép tournamentName + title + slug + tags.
    """
    for key in ("sportType", "sport", "sportName", "sportSlug"):
        val = match.get(key)
        if isinstance(val, dict):
            icon = val.get("iconUrl") or val.get("icon", "")
            if icon:
                return icon
            val = val.get("name") or val.get("slug") or val.get("type", "")
        if val and isinstance(val, str) and val.upper() not in ("MANUAL", "AUTO"):
            logo = _logo_from_text(val)
            if logo != SPORT_LOGOS["football"]:
                return logo
    parts = [
        match.get("tournamentName", ""),
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
                start_str += "+07:00"  # bugiotv API trả startTime theo giờ VN (UTC+7)
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
#  TiêuLâm TV — Build M3U lines
#  Tái sử dụng _fetch_tieulam_for_relay() (dùng chung với relay route).
#  Cấu trúc match: team_1/team_2, blv (string), stream_key, source_live, league
#  Stream live: /match/{id}/live → hd_1/hd_2 (BLV tiếng Việt), source (giọng ngoại)
# ══════════════════════════════════════════════════════════════════════════════

def _tieulam_api_headers() -> dict:
    return {
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin":          TIEULAM_FRONTEND_URL,
        "Referer":         TIEULAM_FRONTEND_URL + "/",
    }

def _fetch_tieulam_live_urls(api_base: str, match_id: str) -> tuple[str, str, str, str]:
    """Gọi /match/{id}/live — trả về (hd_1, hd_2, hd_3, source)."""
    empty = ("", "", "", "")
    if not api_base or not match_id:
        return empty
    try:
        sc = cloudscraper.create_scraper()
        r = sc.get(
            f"{api_base.rstrip('/')}/match/{match_id}/live",
            headers=_tieulam_api_headers(),
            timeout=8,
        )
        if r.status_code != 200:
            return empty
        data = r.json()
        out: list[str] = []
        seen: set[str] = set()
        for key in ("hd_1", "hd_2", "hd_3", "source"):
            val = (data.get(key) or "").strip()
            if val and val not in seen:
                seen.add(val)
                out.append(val)
            else:
                out.append("")
        while len(out) < 4:
            out.append("")
        return tuple(out[:4])  # type: ignore[return-value]
    except Exception:
        return empty

def _tieulam_cdn_headers() -> dict:
    """Header gửi lên CDN asynccdn — dùng frontend thực tế trên Render."""
    front = TIEULAM_FRONTEND_URL.rstrip("/")
    return {
        "User-Agent":      TIEULAM_UA,
        "Referer":         front + "/",
        "Origin":          front,
        "Accept":          "*/*",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    }

def _tieulam_needs_proxy(url: str) -> bool:
    return bool(url and "asynccdn.com" in url)

def _tieulam_scraper():
    sc = getattr(_tieulam_scraper_local, "sc", None)
    if sc is None:
        sc = cloudscraper.create_scraper()
        _tieulam_scraper_local.sc = sc
    return sc

def _tieulam_vi_candidates(hd1: str, hd2: str, hd3: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for u in (hd1, hd2, hd3):
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out

def _tieulam_is_playlist(body: bytes) -> bool:
    return bool(body) and b"#EXTM3U" in body[:256]

def _tieulam_cf_match_url(match_id: str) -> str:
    return f"{TIEULAM_CF_STREAM_RELAY}/stream/match/{match_id}.m3u8"

def _tieulam_cf_proxy_url(raw_url: str) -> str:
    return f"{TIEULAM_CF_STREAM_RELAY}/stream/proxy?u={quote(raw_url, safe='')}"

def _tieulam_fetch_upstream(url: str) -> tuple[int, bytes, str]:
    """Fetch CDN — thử Render trực tiếp, fallback CF Worker relay."""
    code, body, ct = 0, b"", ""
    try:
        r  = _tieulam_scraper().get(url, headers=_tieulam_cdn_headers(), timeout=15)
        ct = (r.headers.get("Content-Type") or "").split(";")[0].strip()
        code, body = r.status_code, r.content
        if code == 200 and body:
            return code, body, ct
    except Exception:
        pass
    if TIEULAM_CF_STREAM_RELAY and _tieulam_needs_proxy(url):
        try:
            r = requests.get(
                _tieulam_cf_proxy_url(url),
                timeout=20,
                headers={"User-Agent": TIEULAM_UA},
            )
            ct = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            if r.status_code == 200 and r.content:
                return r.status_code, r.content, ct
        except Exception:
            pass
    return code, body, ct

def _tieulam_probe_upstream(url: str) -> bool:
    code, body, _ = _tieulam_fetch_upstream(url)
    return code == 200 and _tieulam_is_playlist(body)

def _tieulam_probe_best_vi(hd1: str, hd2: str, hd3: str) -> tuple[str, bool]:
    """Thử HD1 → HD2 → HD3, chọn link CDN trả playlist hợp lệ."""
    for url in _tieulam_vi_candidates(hd1, hd2, hd3):
        if _tieulam_probe_upstream(url):
            return url, True
    candidates = _tieulam_vi_candidates(hd1, hd2, hd3)
    return (candidates[0], False) if candidates else ("", False)

def _tieulam_probe_cf_match(match_id: str) -> bool:
    if not TIEULAM_CF_STREAM_RELAY or not match_id:
        return False
    try:
        r = requests.get(
            _tieulam_cf_match_url(match_id),
            timeout=20,
            headers={"User-Agent": TIEULAM_UA},
        )
        return r.status_code == 200 and _tieulam_is_playlist(r.content)
    except Exception:
        return False

def _tieulam_store_stream_cache(
    match_id: str,
    hd1: str,
    hd2: str,
    hd3: str,
    upstream: str,
    probed_ok: bool,
    cf_ok: bool = False,
) -> None:
    if not match_id:
        return
    _tieulam_stream_cache[match_id] = {
        "upstream": upstream,
        "hd1": hd1,
        "hd2": hd2,
        "hd3": hd3,
        "probed_ok": probed_ok,
        "cf_ok": cf_ok,
        "ts": time.time(),
    }

def _tieulam_fetch_vi_playlist(match_id: str) -> tuple[int, bytes, str, str]:
    """Fetch playlist BLV Việt — failover HD1 → HD2 → HD3."""
    now    = time.time()
    cached = _tieulam_stream_cache.get(match_id) or {}
    hd1 = cached.get("hd1", "")
    hd2 = cached.get("hd2", "")
    hd3 = cached.get("hd3", "")

    if not (hd1 or hd2 or hd3) or now - cached.get("ts", 0) >= 120:
        api_base = _get_tieulam_api_bongda()
        hd1, hd2, hd3, _ = _fetch_tieulam_live_urls(api_base, match_id)

    ordered = _tieulam_vi_candidates(hd1, hd2, hd3)
    if not ordered:
        return 0, b"", "", ""

    seen: set[str] = set()
    for url in ordered:
        if url in seen:
            continue
        seen.add(url)
        code, body, ct = _tieulam_fetch_upstream(url)
        if code == 200 and body and (_tieulam_is_playlist(body) or url.endswith(".m3u8")):
            _tieulam_store_stream_cache(match_id, hd1, hd2, hd3, url, True)
            return code, body, ct, url

    _tieulam_store_stream_cache(match_id, hd1, hd2, hd3, ordered[0], False)
    return 0, b"", "", ordered[0]

def _request_root() -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host  = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.host
    return f"{proto}://{host}".rstrip("/")

def _tieulam_proxy_url(raw_url: str, proxy_root: str) -> str:
    return f"{proxy_root}/tieulam-stream/proxy?u={quote(raw_url, safe='')}"

def _tieulam_rewrite_m3u8(body: str, upstream_url: str, proxy_root: str) -> str:
    base = upstream_url.rsplit("/", 1)[0] + "/"

    def to_proxy(raw: str) -> str:
        return _tieulam_proxy_url(urljoin(base, raw.strip()), proxy_root)

    out: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                line = line.replace(m.group(0), f'URI="{to_proxy(m.group(1))}"')
            out.append(line)
        else:
            out.append(to_proxy(stripped))
    return "\n".join(out) + "\n"

def _tieulam_foreign_fallback(match_id: str) -> str:
    for m in _tieulam_relay_cache.get("data") or []:
        if (m.get("id") or "") == match_id:
            u = (m.get("source_live") or "").strip()
            if u:
                return u
    try:
        api_base = _get_tieulam_api_bongda()
        *_, src = _fetch_tieulam_live_urls(api_base, match_id)
        return (src or "").strip()
    except Exception:
        return ""

def _pick_tieulam_stream(
    match: dict,
    hd1: str,
    hd2: str,
    hd3: str,
    live_source: str,
) -> tuple[str, str]:
    """
    Chọn link phát: ưu tiên BLV tiếng Việt (HD1 → HD2 → HD3),
    chỉ dùng source_live / CDN nước ngoài khi không có HD tiếng Việt.
    Trả về (primary_url, fallback_url).
    """
    source_live = (match.get("source_live") or live_source or "").strip()
    stream_key  = (match.get("stream_key") or "").strip()
    cdn_url     = f"{TIEULAM_STREAM_CDN}/live/{stream_key}/playlist.m3u8" if stream_key else ""

    vi_stream = hd1 or hd2 or hd3
    foreign   = source_live

    if vi_stream:
        primary  = vi_stream
        fallback = foreign if foreign and foreign != primary else ""
        if not fallback:
            for alt in (hd2, hd3):
                if alt and alt != primary:
                    fallback = alt
                    break
        if not fallback and cdn_url and cdn_url != primary:
            fallback = cdn_url
        return primary, fallback

    if foreign:
        return foreign, ""
    if cdn_url:
        return cdn_url, ""
    return "", ""

def _tieulam_pipe_url(url: str) -> str:
    """Gắn Referer + User-Agent vào URL (TiviMate/VLC) cho CDN asynccdn."""
    if not url or "|" in url or "asynccdn.com" not in url:
        return url
    ref = _tieulam_stream_referrer()
    return f"{url}|Referer={ref}&User-Agent={TIEULAM_UA}"

def _append_tieulam_entry(
    lines: list, logo: str, display: str, stream_url: str, *, use_vlcopt: bool = True,
) -> None:
    lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="TiêuLâm TV",{display}')
    if use_vlcopt:
        ref = _tieulam_stream_referrer()
        lines.append(f"#EXTVLCOPT:http-user-agent={TIEULAM_UA}")
        lines.append(f"#EXTVLCOPT:http-referrer={ref}")
    lines.append(stream_url)

def _build_tieulam_lines(matches: list, api_base: str = "") -> list:
    """
    Tạo dòng M3U cho nhóm "TiêuLâm TV".
    Với trận live/live_integrated: gọi /match/{id}/live để lấy HD1/HD2 (BLV tiếng Việt).
    """
    lines: list[str] = []
    try:
        matches = sorted(matches, key=lambda m: m.get("start_date") or "")
    except Exception:
        pass

    live_jobs = [
        m for m in matches
        if (m.get("id") or "").strip()
        and (m.get("stream_key") or "").strip()
        and (m.get("is_live") or m.get("live_integrated"))
    ]
    live_urls: dict[str, tuple[str, str, str, str]] = {}
    if api_base and live_jobs:
        def _fetch_live_bundle(m: dict) -> tuple[str, tuple[str, str, str, str]]:
            mid = m["id"]
            urls = _fetch_tieulam_live_urls(api_base, mid)
            hd1, hd2, hd3, _ = urls
            best, ok = _tieulam_probe_best_vi(hd1, hd2, hd3)
            cf_ok = (not ok) and _tieulam_probe_cf_match(mid)
            if best or cf_ok:
                _tieulam_store_stream_cache(mid, hd1, hd2, hd3, best, ok or cf_ok, cf_ok=cf_ok)
            return mid, urls

        with ThreadPoolExecutor(max_workers=min(len(live_jobs), 8)) as pool:
            futures = {pool.submit(_fetch_live_bundle, m): m["id"] for m in live_jobs}
            for fut in as_completed(futures):
                mid = futures[fut]
                try:
                    _, urls = fut.result()
                    live_urls[mid] = urls
                except Exception:
                    live_urls[mid] = ("", "", "", "")

    for match in matches:
        blv_name = (match.get("blv") or "").strip()
        if not blv_name:
            continue
        if match.get("is_finished") or match.get("is_end"):
            continue

        match_id = (match.get("id") or "").strip()
        hd1 = hd2 = hd3 = live_source = ""
        if match_id in live_urls:
            hd1, hd2, hd3, live_source = live_urls[match_id]

        primary, fallback = _pick_tieulam_stream(match, hd1, hd2, hd3, live_source)
        if not primary:
            continue

        vi_cached = _tieulam_stream_cache.get(match_id) or {}
        use_proxy = bool(
            match_id
            and (hd1 or hd2 or hd3)
            and _tieulam_needs_proxy(hd1 or hd2 or hd3 or primary)
            and (match.get("is_live") or match.get("live_integrated"))
        )

        if use_proxy:
            if vi_cached.get("cf_ok") or _tieulam_probe_cf_match(match_id):
                primary = _tieulam_cf_match_url(match_id)
                vi_ok   = True
            else:
                primary = f"/tieulam-stream/{match_id}.m3u8"
                vi_ok   = vi_cached.get("probed_ok", False)
                if not vi_ok and fallback:
                    primary, fallback = fallback, ""
        else:
            primary  = _tieulam_pipe_url(primary)
            fallback = _tieulam_pipe_url(fallback) if fallback else ""

        home   = (match.get("team_1") or "Home").strip()
        away   = (match.get("team_2") or "Away").strip()
        league = (match.get("league") or "").strip()
        logo   = _logo_from_text(league.lower()) if league else SPORT_LOGOS["football"]

        time_str, date_str = "--:--", "--/--"
        start = match.get("start_date") or ""
        if start:
            try:
                if "+" not in start and not start.endswith("Z"):
                    start += "+00:00"
                dt       = datetime.fromisoformat(start)
                dt_vn    = dt.astimezone(VN_TZ)
                time_str = dt_vn.strftime("%H:%M")
                date_str = dt_vn.strftime("%d/%m")
            except Exception:
                pass

        prefix  = "🔴 LIVE | " if match.get("is_live") else ""
        display = f"{prefix}{time_str} - {date_str} | {home} VS {away}"
        if league:
            display += f" ({league})"
        display += f" | {blv_name}"

        _append_tieulam_entry(lines, logo, display, primary, use_vlcopt=not use_proxy)
        if fallback and fallback != primary:
            _append_tieulam_entry(lines, logo, f"{display} [Dự phòng]", fallback, use_vlcopt=True)

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

def _build_fixture_lines(fixtures: list, group_title: str) -> list:
    try:
        fixtures = sorted(fixtures, key=lambda f: f.get("startTime") or "")
    except Exception:
        pass
    lines = []
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
            _referer_map = {
                "Hội Quán TV": HOIQUAN_FRONTEND_URL.rstrip("/") + "/",
                "Khán Đài A":  KHANDAIA_FRONTEND_URL.rstrip("/") + "/",
            }
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
#  Background pre-fetch (parallel, 5 sources)
# ══════════════════════════════════════════════════════════════════════════════

def _refresh_all_playlists():
    # Tự động follow redirect để cập nhật domain thực tế
    _resolve_all_frontends()
    errors = []

    def fetch_cola():
        return _build_colatv_lines(_fetch_colatv_matches())

    def fetch_hq():
        return _build_fixture_lines(_fetch_hoiquan_fixtures(), "Hội Quán TV")

    def fetch_kda():
        return _build_fixture_lines(_fetch_khandaia_fixtures(), "Khán Đài A")

    def fetch_vc():
        return _build_vongcam_lines(_fetch_vongcam_matches())

    def fetch_tl():
        matches  = _fetch_tieulam_for_relay()
        api_base = _tieulam_bongda_cache["url"]
        return _build_tieulam_lines(matches, api_base)

    def fetch_dekiki():
        return _fetch_dekiki_lines()

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {
            ex.submit(fetch_cola):   "cola",
            ex.submit(fetch_hq):     "hoiquan",
            ex.submit(fetch_kda):    "khandaia",
            ex.submit(fetch_vc):     "vongcam",
            ex.submit(fetch_tl):     "tieulam",
            ex.submit(fetch_dekiki): "dekiki",
        }
        results = {}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = []
                errors.append(f"{key}: {e}")

    cola_lines   = results.get("cola",     [])
    hq_lines     = results.get("hoiquan",  [])
    kda_lines    = results.get("khandaia", [])
    vc_lines     = results.get("vongcam",  [])
    tl_lines     = results.get("tieulam",  [])
    dekiki_lines = results.get("dekiki",   [])

    err_str = "; ".join(errors)

    def count(lines):
        return sum(1 for l in lines if l.startswith("#EXTINF"))

    # EPG header — shared across all playlists
    epg_header = f'#EXTM3U url-tvg="{EPG_URL}" x-tvg-url="{EPG_URL}"'

    # Build + store individual playlists
    _store("cola",     epg_header + "\n" + "\n".join(cola_lines))
    _store("hoiquan",  epg_header + "\n" + "\n".join(hq_lines))
    _store("khandaia", epg_header + "\n" + "\n".join(kda_lines))
    _store("vongcam",  epg_header + "\n" + "\n".join(vc_lines))
    _store("tieulam",  epg_header + "\n" + "\n".join(tl_lines))
    _store("dekiki",   epg_header + "\n" + "\n".join(dekiki_lines))

    # Combined — live sports first, then static TV channels
    all_lines = cola_lines + hq_lines + kda_lines + vc_lines + tl_lines + dekiki_lines
    combined_text = epg_header + "\n" + "\n".join(all_lines)
    if err_str:
        combined_text += f"\n# Errors: {err_str}"
    _store("combined", combined_text)

    _last_counts.update({
        "cola":         count(cola_lines),
        "hoiquan":      count(hq_lines),
        "khandaia":     count(kda_lines),
        "vongcam":      count(vc_lines),
        "tieulam":      count(tl_lines),
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
#  TieuLam Relay — Bongda/Render làm proxy cho GitHub Actions
# ══════════════════════════════════════════════════════════════════════════════

def _get_tieulam_api_bongda() -> str:
    now = time.time()
    if now - _tieulam_bongda_cache["discovered_at"] < API_DISCOVERY_TTL:
        return _tieulam_bongda_cache["url"]
    sc = cloudscraper.create_scraper()
    for front in [TIEULAM_FRONTEND_URL, "https://sv1.tieulam1.live"]:
        try:
            html = sc.get(front, timeout=8).text
            for js_path in re.findall(r'src="(/assets/[^"]+\.js)"', html)[:4]:
                try:
                    js = sc.get(front + js_path, timeout=8).text
                    m = re.search(r'create\(\{baseURL:"(https://[^"]{10,80})"\}', js)
                    if m and not re.search(r'cdn|live|pull|stream|secufun|asynccdn', m.group(1)):
                        _tieulam_bongda_cache["url"] = m.group(1).rstrip("/")
                        _tieulam_bongda_cache["discovered_at"] = now
                        return _tieulam_bongda_cache["url"]
                    m2 = re.search(r'"(https://api\.tlap[a-z0-9]{6,12}\.(?:com|xyz))"', js)
                    if m2:
                        _tieulam_bongda_cache["url"] = m2.group(1)
                        _tieulam_bongda_cache["discovered_at"] = now
                        return _tieulam_bongda_cache["url"]
                except Exception:
                    pass
        except Exception:
            pass
    return _tieulam_bongda_cache["url"]

def _fetch_tieulam_for_relay() -> list:
    """Fetch TieuLam matches via cloudscraper — dùng IP của Render."""
    now_ts = time.time()
    if _tieulam_relay_cache["data"] is not None and now_ts - _tieulam_relay_cache["ts"] < 180:
        return _tieulam_relay_cache["data"]
    api_base = _get_tieulam_api_bongda()
    now_dt   = datetime.now(tz=timezone.utc)
    cutoff   = (now_dt - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_e = (now_dt + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    payload  = {
        "queries": [
            {"field": "start_date", "type": "gte",       "value": cutoff},
            {"field": "start_date", "type": "lte",       "value": cutoff_e},
            {"field": "blv",        "type": "not_equal", "value": None},
            {"field": "blv",        "type": "not_equal", "value": ""},
        ],
        "query_and": True, "limit": 50, "page": 1, "order_asc": "start_date",
    }
    hdrs = {
        "Content-Type":    "application/json",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin":          TIEULAM_FRONTEND_URL,
        "Referer":         TIEULAM_FRONTEND_URL + "/",
    }
    sc   = cloudscraper.create_scraper()
    resp = sc.post(f"{api_base}/matches/graph", json=payload, headers=hdrs, timeout=20)
    resp.raise_for_status()
    data = resp.json().get("data") or []
    _tieulam_relay_cache["data"] = data
    _tieulam_relay_cache["ts"]   = now_ts
    return data

# ══════════════════════════════════════════════════════════════════════════════
#  Flask routes
# ══════════════════════════════════════════════════════════════════════════════

def _m3u_response(key: str, filename: str) -> Response:
    entry = _get_entry(key)

    # First request — build synchronously if cache is cold
    if entry["content"] is None:
        try:
            _refresh_all_playlists()
            entry = _get_entry(key)
        except Exception as e:
            return Response(f"Error: {e}", status=500, mimetype="text/plain")

    # ── ETag / conditional GET ────────────────────────────────────────────────
    etag = entry["etag"]
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)

    # ── Choose gzip or plain ──────────────────────────────────────────────────
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

@app.route("/tieulam.m3u")
def tieulam_m3u():
    return _m3u_response("tieulam", "tieulam.m3u")

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
        "channels": {
            "total":    sum(_last_counts.get(k, 0) for k in ("cola","hoiquan","khandaia","vongcam","dekiki")),
            "cola_tv":      _last_counts.get("cola",     0),
            "hoiquan_tv":   _last_counts.get("hoiquan",  0),
            "khandai_a":    _last_counts.get("khandaia", 0),
            "vongcam_tv":   _last_counts.get("vongcam",  0),
            "dekiki_tv":    _last_counts.get("dekiki",   0),
        },
        "sources": {
            "cola_tv":    {"api": _colatv_api_cache.get("url"),   "status": "ok" if _last_counts.get("cola",0)    > 0 else "empty"},
            "hoiquan_tv": {"api": _hoiquan_api_cache.get("url"),  "status": "ok" if _last_counts.get("hoiquan",0) > 0 else "empty"},
            "khandai_a":  {"api": _khandaia_api_cache.get("url"), "status": "ok" if _last_counts.get("khandaia",0)> 0 else "empty"},
            "vongcam_tv": {"api": _vongcam_api_cache.get("url"), "token": _vongcam_token_cache.get("token"), "status": "ok" if _last_counts.get("vongcam",0) > 0 else "empty"},
            "dekiki_tv":  {"api": "github-static",                "status": "ok" if _last_counts.get("dekiki",0)  > 0 else "empty"},
        },
    })

def _tieulam_allowed_upstream(url: str) -> bool:
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    return any(h in host for h in (
        "asynccdn.com", "lilive1.eu.cc", "lilive3.eu.cc", "secufun.xyz", "b-cdn.net",
    ))

@app.route("/tieulam-stream/<match_id>.m3u8")
def tieulam_stream_playlist(match_id: str):
    """Proxy playlist BLV Việt — failover HD1/HD2/HD3 qua cloudscraper."""
    code, body, ct, upstream = _tieulam_fetch_vi_playlist(match_id)
    if code != 200 or not body:
        foreign = _tieulam_foreign_fallback(match_id)
        if foreign:
            return Response("", status=302, headers={"Location": foreign})
        return Response(f"Upstream HTTP {code}", status=502, mimetype="text/plain")

    if _tieulam_is_playlist(body) or upstream.endswith(".m3u8") or "mpegurl" in ct:
        text = body.decode("utf-8", errors="replace")
        rewritten = _tieulam_rewrite_m3u8(text, upstream, _request_root())
        return Response(rewritten, mimetype="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache"})

    mt = ct or "application/octet-stream"
    return Response(body, mimetype=mt, headers={"Cache-Control": "no-cache"})

@app.route("/tieulam-stream/proxy")
def tieulam_stream_proxy():
    """Proxy segment / sub-playlist từ CDN upstream."""
    url = (request.args.get("u") or "").strip()
    if not url.startswith(("http://", "https://")) or not _tieulam_allowed_upstream(url):
        return Response("Bad URL", status=400, mimetype="text/plain")

    code, body, ct = _tieulam_fetch_upstream(url)
    if code != 200 or not body:
        return Response(f"Upstream HTTP {code}", status=502, mimetype="text/plain")

    if b"#EXTM3U" in body or url.endswith(".m3u8") or "mpegurl" in ct:
        text = body.decode("utf-8", errors="replace")
        rewritten = _tieulam_rewrite_m3u8(text, url, _request_root())
        return Response(rewritten, mimetype="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache"})

    if url.endswith(".ts"):
        mt = "video/mp2t"
    elif url.endswith(".key"):
        mt = "application/octet-stream"
    else:
        mt = ct or "application/octet-stream"
    return Response(body, mimetype=mt, headers={"Cache-Control": "no-cache"})

@app.route("/tieulam-stream-test/<match_id>")
def tieulam_stream_test(match_id: str):
    """Diagnostic: probe HD1/HD2/HD3 và test proxy."""
    from flask import jsonify
    api_base = _get_tieulam_api_bongda()
    hd1, hd2, hd3, src = _fetch_tieulam_live_urls(api_base, match_id)
    best, probed = _tieulam_probe_best_vi(hd1, hd2, hd3)
    code, body, ct, used = _tieulam_fetch_vi_playlist(match_id)
    preview = body[:80].decode("utf-8", errors="replace") if body else ""
    cf_ok = _tieulam_probe_cf_match(match_id)
    return jsonify({
        "ok": code == 200 or cf_ok,
        "match_id": match_id,
        "hd1": hd1, "hd2": hd2, "hd3": hd3,
        "probed_best": best,
        "probed_ok": probed,
        "cf_ok": cf_ok,
        "cf_url": _tieulam_cf_match_url(match_id) if TIEULAM_CF_STREAM_RELAY else "",
        "used_upstream": used,
        "http_status": code,
        "content_type": ct,
        "preview": preview,
        "proxy_url": f"{_request_root()}/tieulam-stream/{match_id}.m3u8",
        "public_base_url": _request_root(),
        "foreign_fallback": _tieulam_foreign_fallback(match_id) or src,
    })

@app.route("/tieulam-relay")
def tieulam_relay_route():
    """Relay TieuLam API → GitHub Actions vượt block 403.
    Auth: X-Relay-Token (nếu RELAY_SECRET được set).
    """
    from flask import jsonify
    if RELAY_SECRET:
        if request.headers.get("X-Relay-Token", "") != RELAY_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
    try:
        data     = _fetch_tieulam_for_relay()
        api_base = _tieulam_bongda_cache["url"]
        return jsonify({"data": data, "count": len(data),
                        "api_base": api_base, "relay": "bongda-render"})
    except Exception as e:
        if _tieulam_relay_cache["data"] is not None:
            return jsonify({"data":    _tieulam_relay_cache["data"],
                            "count":   len(_tieulam_relay_cache["data"]),
                            "api_base": _tieulam_bongda_cache["url"],
                            "relay":   "bongda-render", "cached": True,
                            "stale":   True, "error": str(e)})
        return jsonify({"error": str(e), "data": []}), 502

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
    err_html = f'<p style="color:red">⚠️ {err}</p>' if err else ""

    cola_count   = _last_counts.get("cola",    0)
    hq_count     = _last_counts.get("hoiquan", 0)
    kda_count    = _last_counts.get("khandaia",0)
    vc_count     = _last_counts.get("vongcam", 0)
    dekiki_count = _last_counts.get("dekiki",  0)
    total        = cola_count + hq_count + kda_count + vc_count + dekiki_count

    return (
        "<h2>🎬 IPTV M3U Server</h2>"
        "<h3>📋 Playlist</h3><ul>"
        "<li><a href='/live.m3u'>/live.m3u</a> — Tất cả nguồn gộp lại</li>"
        "<li><a href='/cola.m3u'>/cola.m3u</a> — Cola TV only</li>"
        "<li><a href='/hoiquan.m3u'>/hoiquan.m3u</a> — Hội Quán TV only</li>"
        "<li><a href='/khandaia.m3u'>/khandaia.m3u</a> — Khán Đài A only</li>"
        "<li><a href='/vongcam.m3u'>/vongcam.m3u</a> — Vòng Cấm TV only</li>"
        "<li><a href='/tieulam.m3u'>/tieulam.m3u</a> — Tiêu Lâm TV (CF relay + Render proxy)</li>"
        "<li><a href='/dekiki.m3u'>/dekiki.m3u</a> — Kênh TV Việt (dekiki)</li>"
        "</ul>"
        "<h3>📊 Trạng thái</h3>"
        f"<p>📺 Tổng kênh: <strong>{total}</strong>"
        f" &nbsp;(🏆 Live: {cola_count + hq_count + kda_count + vc_count}"
        f" | 📡 TV: {dekiki_count})</p>"
        f"<p>🕐 Cập nhật lần cuối: <strong>{dt_str}</strong></p>"
        f"<p>⏳ Cập nhật tiếp theo: <strong>{next_str}</strong></p>"
        f"<p>🟢 Cola TV: <strong>{cola_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_colatv_api_cache['url']}</code></p>"
        f"<p>🟢 Hội Quán TV: <strong>{hq_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_hoiquan_api_cache['url']}</code></p>"
        f"<p>🟢 Khán Đài A: <strong>{kda_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_khandaia_api_cache['url']}</code></p>"
        f"<p>🟢 Vòng Cấm TV: <strong>{vc_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_vongcam_api_cache['url']}</code>"
        f"&nbsp;|&nbsp; token: <code>{_vongcam_token_cache['token']}</code></p>"
        f"<p>📡 Kênh TV (dekiki): <strong>{dekiki_count} kênh</strong></p>"
        f"<p>📻 EPG: <a href='{EPG_URL}' target='_blank'>{EPG_URL}</a></p>"
        f"{err_html}"
        "<h3>⚙️ Tối ưu băng thông</h3><ul>"
        "<li>Gzip nén tự động (giảm ~70% dữ liệu truyền)</li>"
        "<li>ETag + HTTP 304 — client có cache không cần tải lại</li>"
        f"<li>Cache-Control: public, max-age={PREFETCH_INTERVAL}s</li>"
        "<li>1 worker process + 8 threads — cache dùng chung, không fetch trùng lặp</li>"
        "<li>5 nguồn fetch song song (ThreadPoolExecutor)</li>"
        f"<li>Làm mới cache mỗi <strong>{PREFETCH_INTERVAL // 60} phút</strong></li>"
        "</ul>"
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
