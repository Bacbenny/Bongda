import gzip
import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

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
VONGCAM_ACCESS_TOKEN   = os.environ.get("VONGCAM_TOKEN",    "AB321C")

# ─── Dekiki (GitHub-hosted static list) + EPG ────────────────────────────────
DEKIKI_M3U_URL = os.environ.get(
    "DEKIKI_M3U_URL",
    "https://raw.githubusercontent.com/blvbatman/iptv/refs/heads/main/iptv.m3u",
)
EPG_URL = os.environ.get("EPG_URL", "https://vnepg.site/epg.xml")

# ─── Shared config ────────────────────────────────────────────────────────────
VN_TZ                = timezone(timedelta(hours=7))
SELF_PING_INTERVAL   = 240   # seconds
PREFETCH_INTERVAL    = 300   # seconds — refresh cache every 5 minutes
API_DISCOVERY_TTL    = 3600  # seconds — re-discover API URL every 1 hour

COLATV_FINISHED_STATUS_INT = {3}
FINISHED_STATUS_STRINGS    = {"finished", "end", "ended", "complete", "completed"}
MATCH_MAX_AGE_SECONDS      = int(os.environ.get("MATCH_MAX_DURATION", 7200))  # 2 h

# ─── Sport logos (Twemoji via jsDelivr) ───────────────────────────────────────
_CDN = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
SPORT_LOGOS = {
    "football":   f"{_CDN}/26bd.png",
    "tennis":     f"{_CDN}/1f3be.png",
    "basketball": f"{_CDN}/1f3c0.png",
    "volleyball": f"{_CDN}/1f3d0.png",
    "billiards":  f"{_CDN}/1f3b1.png",
    "badminton":  f"{_CDN}/1f3f8.png",
    "default":    f"{_CDN}/1f3c6.png",
}

# ─── API URL caches ───────────────────────────────────────────────────────────
_colatv_api_cache   = {"url": COLATV_KNOWN_API_URL,    "discovered_at": 0}
_hoiquan_api_cache  = {"url": HOIQUAN_KNOWN_API_BASE,  "discovered_at": 0}
_khandaia_api_cache = {"url": KHANDAIA_KNOWN_API_BASE, "discovered_at": 0}
_vongcam_api_cache  = {"url": VONGCAM_KNOWN_API_BASE,  "discovered_at": 0}

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
    "dekiki":   _empty_entry(),
}

_last_counts = {
    "cola": 0, "hoiquan": 0, "khandaia": 0, "vongcam": 0, "dekiki": 0,
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
#  Vòng Cấm TV — API discovery + fetch
#  Frontend : https://sv2.vongcam3.live
#  Matches  : https://sv.bugiotv.xyz/internal/api/matches
#  Auth     : Access-Token header (static token discovered from JS bundle)
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


def _fetch_vongcam_matches() -> list:
    scraper = cloudscraper.create_scraper()
    api_url = _get_vongcam_api_base(scraper)
    headers = {
        **_HQ_HEADERS,
        "Referer":      VONGCAM_FRONTEND_URL + "/",
        "Origin":       VONGCAM_FRONTEND_URL,
        "Access-Token": VONGCAM_ACCESS_TOKEN,
    }
    try:
        resp = scraper.get(api_url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception:
        _vongcam_api_cache["discovered_at"] = 0
        api_url = _get_vongcam_api_base(scraper)
        resp = scraper.get(api_url, headers=headers, timeout=15)
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
                start_str += "+00:00"
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
        logo       = _logo_from_text(tournament)
        tournament = match.get("tournamentName", "")
        start_str  = match.get("startTime", "")
        try:
            if "+" not in start_str and not start_str.endswith("Z"):
                start_str += "+00:00"
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
        lines.append(stream_url)
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
            lines.append(stream_url)
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
    errors = []

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

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {
            ex.submit(fetch_cola):   "cola",
            ex.submit(fetch_hq):     "hoiquan",
            ex.submit(fetch_kda):    "khandaia",
            ex.submit(fetch_vc):     "vongcam",
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
    _store("dekiki",   epg_header + "\n" + "\n".join(dekiki_lines))

    # Combined — live sports first, then static TV channels
    all_lines = cola_lines + hq_lines + kda_lines + vc_lines + dekiki_lines
    combined_text = epg_header + "\n" + "\n".join(all_lines)
    if err_str:
        combined_text += f"\n# Errors: {err_str}"
    _store("combined", combined_text)

    _last_counts.update({
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


@app.route("/dekiki.m3u")
def dekiki_m3u():
    return _m3u_response("dekiki", "dekiki.m3u")


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
        f"&nbsp;|&nbsp; <code>{_vongcam_api_cache['url']}</code></p>"
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
