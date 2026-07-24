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



# ─── Pháo Hoa TV config ──────────────────────────────────────────────────────
PHAOHOA_FRONTEND_URL   = os.environ.get("PHAOHOA_FRONTEND", "https://phaohoa1.live")
PHAOHOA_API_URL        = os.environ.get("PHAOHOA_API",      "https://phaohoa1.live/api/matches/")

# ─── Dekiki (GitHub-hosted static list) + EPG ────────────────────────────────
DEKIKI_M3U_URL = os.environ.get(
    "DEKIKI_M3U_URL",
    "https://raw.githubusercontent.com/Bacbenny/Bongda/refs/heads/main/xemtv.m3u",
)
EPG_URL = os.environ.get("EPG_URL", "https://vnepg.site/epg.xml")

# ─── Tiếu Lâm TV (live, nguồn tinhlagi.pro) ──────────────────────────────────
TINHLAGI_M3U_URL = os.environ.get("TINHLAGI_M3U_URL", "https://tinhlagi.pro/s.m3u")

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
_phaohoa_api_cache  = {"url": PHAOHOA_API_URL,  "discovered_at": 0}

# ─── Auto domain resolution ───────────────────────────────────────────────────
def _resolve_base_url(url: str, timeout: int = 8) -> str:
    """Follow HTTP 3xx redirects và trả về scheme+host cuối cùng.
    Tự động phát hiện khi domain đổi (vd: phaohoa1.live → phaohoa2.live).
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
    """Tự động cập nhật PHAOHOA _FRONTEND_URL bằng cách follow redirect."""
    global PHAOHOA_FRONTEND_URL
    sources = {
        "Pháo Hoa TV": ("PHAOHOA", PHAOHOA_FRONTEND_URL),
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {pool.submit(_resolve_base_url, cfg[1]): (name, cfg) for name, cfg in sources.items()}
        for fut in as_completed(futures):
            (name, (key, original)) = futures[fut]
            try:
                resolved = fut.result()
            except Exception:
                resolved = original
            if resolved != original.rstrip("/"):
                print(f"[domain-resolve] {name}: {original} → {resolved}", flush=True)
            if key == "PHAOHOA":
                PHAOHOA_FRONTEND_URL = resolved



# ─── Playlist content cache ───────────────────────────────────────────────────
# Each entry stores: raw bytes, gzip bytes, md5 etag, and build timestamp.
def _empty_entry():
    return {"content": None, "gz": None, "etag": None, "built_at": 0,
            "lock": threading.Lock()}

_playlist_cache = {
    "combined": _empty_entry(),
    "tieulam":  _empty_entry(),
    "cola":     _empty_entry(),
    "phaohoa":  _empty_entry(),
    "dekiki":   _empty_entry(),
}

_last_counts = {
    "tieulam": 0, "cola": 0, "phaohoa": 0, "dekiki": 0,
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
                lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="CoLa TV",{display}')
                lines.append(stream_url)
        else:
            stream_url = match.get("videoUrl", "")
            if not stream_url:
                continue
            display = f"{time_str} - {date_str} | {home} VS {away} ({competition})"
            lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="CoLa TV",{display}')
            lines.append(stream_url)
    return lines

# ══════════════════════════════════════════════════════════════════════════════
#  Pháo Hoa TV — fetch từ phaohoa1.live/api/matches (Django REST, không token)
#  API  : https://phaohoa1.live/api/matches/?page=N
#  Schema: {count, next, previous, results: [{id, sport_name, sport_icon_url,
#            tournament_name, home_team_name, home_team_logo, away_team_name,
#            away_team_logo, start_time, status, primary_stream_url,
#            backup_stream_url, commentators}]}
# ══════════════════════════════════════════════════════════════════════════════

_PHAOHOA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://phaohoa1.live/",
    "Accept": "application/json",
}

def _fetch_phaohoa_matches() -> list:
    """Fetch trận đang live + sắp diễn ra từ phaohoa1.live.
    Dùng requests (không cloudscraper) vì API Django REST trả JSON
    khi có header Accept: application/json.
    """
    results = []
    base = PHAOHOA_API_URL.rstrip("/") + "/"
    sep  = "&" if "?" in base else "?"
    for status in ("live", "scheduled"):
        url = base + sep + f"status={status}&ordering=start_time"
        for _ in range(5):
            try:
                resp = requests.get(url, headers=_PHAOHOA_HEADERS, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                break
            results.extend(data.get("results", []))
            next_url = data.get("next")
            if not next_url:
                break
            url = next_url
    return results

def _phaohoa_is_active(match: dict) -> bool:
    """Trận hợp lệ nếu chưa kết thúc.
    Không yêu cầu stream URL — link sẽ lấy theo thời gian thực khi user mở kênh."""
    status = str(match.get("status") or "").lower().strip()
    if status in FINISHED_STATUS_STRINGS:
        return False
    # Chỉ áp dụng giới hạn tuổi cho trận đã bắt đầu (live/active),
    # không cho scheduled sắp diễn ra trong tương lai.
    if status not in ("scheduled", "upcoming", ""):
        start_str = match.get("start_time", "")
        if start_str:
            try:
                dt      = datetime.fromisoformat(start_str)
                elapsed = time.time() - dt.timestamp()
                if elapsed > MATCH_MAX_AGE_SECONDS:
                    return False
            except Exception:
                pass
    return True

def _pick_phaohoa_stream(match: dict) -> tuple:
    """Trả về (stream_url, commentator_name)."""
    # Ưu tiên commentators trước
    for c in (match.get("commentators") or []):
        url = (c.get("stream_url") or c.get("streamUrl") or "").strip()
        name = (c.get("nickname") or c.get("name") or "").strip()
        if url:
            return url, name
    # Fallback: primary rồi backup stream
    primary = (match.get("primary_stream_url") or "").strip()
    if primary:
        return primary, ""
    backup = (match.get("backup_stream_url") or "").strip()
    if backup:
        return backup, ""
    return "", ""

def _phaohoa_logo(match: dict) -> str:
    """Logo dựa trên sport_icon_url (có sẵn từ API) hoặc sport_name."""
    icon = (match.get("sport_icon_url") or "").strip()
    if icon:
        # Nếu là relative path thì prepend domain
        if icon.startswith("/"):
            icon = PHAOHOA_FRONTEND_URL.rstrip("/") + icon
        return icon
    return _logo_from_text(match.get("sport_name") or match.get("sport_slug") or "")

def _get_server_base_url() -> str:
    """Lấy base URL của server để tạo proxy URL tuyệt đối."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render_url:
        return render_url.rstrip("/")
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        return f"https://{domains.split(',')[0].strip()}"
    app_url = os.environ.get("APP_URL", "")
    if app_url:
        return app_url.rstrip("/")
    return f"http://localhost:{os.environ.get('PORT', 5000)}"

def _fetch_phaohoa_match_by_slug(slug: str) -> dict:
    """Fetch chi tiết 1 trận theo slug từ API Pháo Hoa."""
    url = PHAOHOA_API_URL.rstrip("/") + "/" + slug + "/"
    resp = requests.get(url, headers=_PHAOHOA_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()

def _build_phaohoa_lines(matches: list) -> list:
    """Build M3U lines cho Pháo Hoa TV.
    Mỗi kênh dùng proxy URL /ph_stream/<slug> — khi user mở kênh,
    server sẽ fetch link stream thực tế từ API theo thời gian thực.
    """
    lines = []
    try:
        matches = sorted(matches, key=lambda m: m.get("start_time") or "")
    except Exception:
        pass
    base_url = _get_server_base_url()
    for match in matches:
        if not _phaohoa_is_active(match):
            continue
        slug = (match.get("slug") or "").strip()
        if not slug:
            continue
        home       = (match.get("home_team_name") or "Home").strip()
        away       = (match.get("away_team_name") or "Away").strip()
        tournament = (match.get("tournament_name") or "").strip()
        logo       = _phaohoa_logo(match)
        start_str  = match.get("start_time", "")
        status     = str(match.get("status") or "").lower().strip()
        try:
            dt       = datetime.fromisoformat(start_str)
            dt_vn    = dt.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"
        _, commentator = _pick_phaohoa_stream(match)
        status_label = " LIVE" if status == "live" else ""
        if commentator:
            display = f"{time_str} - {date_str} | {home} VS {away} ({tournament}) | {commentator}{status_label}"
        else:
            display = f"{time_str} - {date_str} | {home} VS {away} ({tournament}){status_label}"
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="Pháo Hoa TV",{display}')
        proxy_url = f"{base_url}/ph_stream/{slug}"
        lines.append(proxy_url)
    return lines

# ══════════════════════════════════════════════════════════════════════════════
#  Dekiki — static GitHub M3U fetch + parse
# ══════════════════════════════════════════════════════════════════════════════

def _parse_tinhlagi_tieulam() -> list:
    """Fetch tinhlagi.pro's public M3U and extract only the Tiếu Lâm TV group,
    excluding (HD2) duplicate-quality entries and (Nhà đài) entries."""
    resp = requests.get(TINHLAGI_M3U_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    lines = resp.text.splitlines()
    channels = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF"):
            m_group = re.search(r'group-title="([^"]*)"', line)
            group = m_group.group(1) if m_group else ""
            if "TIẾU LÂM" in group.upper():
                m_logo = re.search(r'tvg-logo="([^"]*)"', line)
                logo = m_logo.group(1) if m_logo else ""
                comma_idx = line.find(",")
                title = line[comma_idx + 1:].strip() if comma_idx >= 0 else ""
                referrer = ""
                url = ""
                j = i + 1
                while j < len(lines) and not lines[j].startswith("#EXTINF") and lines[j].strip() != "":
                    l2 = lines[j]
                    if l2.startswith("#EXTVLCOPT:http-referrer="):
                        referrer = l2.split("=", 1)[1].strip()
                    elif not l2.startswith("#"):
                        url = l2.strip()
                    j += 1
                if url:
                    title_upper = title.upper()
                    if "(HD2)" not in title_upper and "NHÀ ĐÀI" not in title_upper:
                        channels.append({"title": title, "logo": logo, "referrer": referrer, "url": url})
                i = j
                continue
        i += 1
    return channels


_TIEULAM_TITLE_RE = re.compile(
    r'^(?P<time>\d{1,2}:\d{2})\s+(?P<date>\d{1,2}/\d{1,2})\s+'
    r'(?P<home>.+?)\s+vs\s+(?P<away>.+?)\s*'
    r'(?:\((?P<blv>[^)]*)\))?\s*(?:\[geo\])?$',
    re.IGNORECASE,
)


def _format_tieulam_title(title: str) -> str:
    """Chuẩn hoá tiêu đề Tiếu Lâm TV theo định dạng dùng dấu gạch ngang/gạch đứng
    giống Pháo Hoa TV: 'HH:MM - DD/MM | Home VS Away | BLV ...',
    đồng thời bỏ thẻ [geo]."""
    m = _TIEULAM_TITLE_RE.match(title.strip())
    if not m:
        return re.sub(r'\s*\[geo\]\s*', '', title, flags=re.IGNORECASE).strip()
    time_str = m.group("time")
    date_str = m.group("date")
    home     = m.group("home").strip()
    away     = m.group("away").strip()
    blv      = (m.group("blv") or "").strip()
    formatted = f"{time_str} - {date_str} | {home} VS {away}"
    if blv:
        formatted += f" | {blv}"
    return formatted


def _build_tieulam_lines_from_channels(channels: list) -> list:
    lines = []
    for ch in channels:
        raw_title = ch.get("title", "")
        url       = ch.get("url", "")
        if not raw_title or not url:
            continue
        title = _format_tieulam_title(raw_title)
        logo  = _logo_from_text(title)
        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="TieuLam TV",{title}')
        referrer = ch.get("referrer", "")
        if referrer:
            lines.append(f"#EXTVLCOPT:http-referrer={referrer}")
        lines.append(url)
    return lines


def _fetch_tieulam_lines() -> list:
    return _build_tieulam_lines_from_channels(_parse_tinhlagi_tieulam())


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
#  Shared fixture helpers  (Cola TV)
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
    # Tự động follow redirect để cập nhật domain thực tế
    _resolve_all_frontends()
    errors = []

    def fetch_tieulam():
        return _fetch_tieulam_lines()

    def fetch_cola():
        return _build_colatv_lines(_fetch_colatv_matches())

    def fetch_phaohoa():
        return _build_phaohoa_lines(_fetch_phaohoa_matches())

    def fetch_dekiki():
        return _fetch_dekiki_lines()

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {
            ex.submit(fetch_tieulam):  "tieulam",
            ex.submit(fetch_cola):     "cola",
            ex.submit(fetch_phaohoa):  "phaohoa",
            ex.submit(fetch_dekiki):   "dekiki",
        }
        results = {}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = []
                errors.append(f"{key}: {e}")

    tieulam_lines  = results.get("tieulam",  [])
    cola_lines     = results.get("cola",     [])
    phaohoa_lines  = results.get("phaohoa",  [])
    dekiki_lines   = results.get("dekiki",   [])

    err_str = "; ".join(errors)

    def count(lines):
        return sum(1 for l in lines if l.startswith("#EXTINF"))

    # EPG header — shared across all playlists
    epg_header = f'#EXTM3U url-tvg="{EPG_URL}" x-tvg-url="{EPG_URL}"'

    # Build + store individual playlists
    _store("tieulam",  epg_header + "\n" + "\n".join(tieulam_lines))
    _store("cola",     epg_header + "\n" + "\n".join(cola_lines))
    _store("phaohoa",  epg_header + "\n" + "\n".join(phaohoa_lines))
    _store("dekiki",   epg_header + "\n" + "\n".join(dekiki_lines))

    # Combined — Tiếu Lâm TV + live sports first, then static TV channels
    all_lines = tieulam_lines + cola_lines + phaohoa_lines + dekiki_lines
    combined_text = epg_header + "\n" + "\n".join(all_lines)
    if err_str:
        combined_text += f"\n# Errors: {err_str}"
    _store("combined", combined_text)

    _last_counts.update({
        "tieulam":      count(tieulam_lines),
        "cola":         count(cola_lines),
        "phaohoa":      count(phaohoa_lines),
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

@app.route("/tieulam.m3u")
def tieulam_m3u():
    return _m3u_response("tieulam", "tieulam.m3u")

@app.route("/cola.m3u")
def cola_m3u():
    return _m3u_response("cola", "cola.m3u")

@app.route("/phaohoa.m3u")
def phaohoa_m3u():
    return _m3u_response("phaohoa", "phaohoa.m3u")

@app.route("/dekiki.m3u")
def dekiki_m3u():
    return _m3u_response("dekiki", "dekiki.m3u")

@app.route("/ph_stream/<path:slug>")
def ph_stream(slug: str):
    try:
        match = _fetch_phaohoa_match_by_slug(slug)
    except Exception as e:
        return Response(f"Stream not available: {e}", status=502, mimetype="text/plain")
    stream_url, _ = _pick_phaohoa_stream(match)
    if not stream_url:
        return Response("Stream not available yet — match may not have started.",
                        status=404, mimetype="text/plain")
    _ref = PHAOHOA_FRONTEND_URL.rstrip("/") + "/"
    if "|" not in stream_url:
        stream_url += f"|Referer={_ref}&User-Agent=Mozilla/5.0"
    return Response(status=302, headers={"Location": stream_url})

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
            "total":      sum(_last_counts.get(k, 0) for k in ("tieulam","cola","phaohoa","dekiki")),
            "tieulam_tv": _last_counts.get("tieulam", 0),
            "cola_tv":    _last_counts.get("cola",    0),
            "phaohoa_tv": _last_counts.get("phaohoa", 0),
            "dekiki_tv":  _last_counts.get("dekiki",  0),
        },
        "sources": {
            "tieulam_tv": {"api": TINHLAGI_M3U_URL,              "status": "ok" if _last_counts.get("tieulam",0) > 0 else "empty"},
            "cola_tv":    {"api": _colatv_api_cache.get("url"),  "status": "ok" if _last_counts.get("cola",0)    > 0 else "empty"},
            "phaohoa_tv": {"api": PHAOHOA_API_URL,               "status": "ok" if _last_counts.get("phaohoa",0) > 0 else "empty"},
            "dekiki_tv":  {"api": "github-static",               "status": "ok" if _last_counts.get("dekiki",0)  > 0 else "empty"},
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
    err_html = f'<p style="color:red">⚠️ {err}</p>' if err else ""

    tieulam_count  = _last_counts.get("tieulam",  0)
    cola_count     = _last_counts.get("cola",     0)
    phaohoa_count  = _last_counts.get("phaohoa",  0)
    dekiki_count   = _last_counts.get("dekiki",   0)
    total          = tieulam_count + cola_count + phaohoa_count + dekiki_count

    return (
        "<h2>🎬 IPTV M3U Server</h2>"
        "<h3>📋 Playlist</h3><ul>"
        "<li><a href='/live.m3u'>/live.m3u</a> — Tất cả nguồn gộp lại</li>"
        "<li><a href='/tieulam.m3u'>/tieulam.m3u</a> — TieuLam TV only</li>"
        "<li><a href='/cola.m3u'>/cola.m3u</a> — Cola TV only</li>"
        "<li><a href='/phaohoa.m3u'>/phaohoa.m3u</a> — Pháo Hoa TV only</li>"
        "<li><a href='/dekiki.m3u'>/dekiki.m3u</a> — Kênh TV Việt (dekiki)</li>"
        "</ul>"
        "<h3>📊 Trạng thái</h3>"
        f"<p>📺 Tổng kênh: <strong>{total}</strong>"
        f" &nbsp;(🏆 Live: {cola_count + phaohoa_count}"
        f" | 📡 TV: {tieulam_count + dekiki_count})</p>"
        f"<p>🕐 Cập nhật lần cuối: <strong>{dt_str}</strong></p>"
        f"<p>⏳ Cập nhật tiếp theo: <strong>{next_str}</strong></p>"
        f"<p>🟢 TieuLam TV: <strong>{tieulam_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{TINHLAGI_M3U_URL}</code></p>"
        f"<p>🟢 Cola TV: <strong>{cola_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{_colatv_api_cache['url']}</code></p>"
        f"<p>🟢 Pháo Hoa TV: <strong>{phaohoa_count} kênh</strong>"
        f"&nbsp;|&nbsp; <code>{PHAOHOA_API_URL}</code></p>"
        f"<p>📡 Kênh TV (dekiki): <strong>{dekiki_count} kênh</strong></p>"
        f"<p>📻 EPG: <a href='{EPG_URL}' target='_blank'>{EPG_URL}</a></p>"
        f"{err_html}"
        "<h3>⚙️ Tối ưu băng thông</h3><ul>"
        "<li>Gzip nén tự động (giảm ~70% dữ liệu truyền)</li>"
        "<li>ETag + HTTP 304 — client có cache không cần tải lại</li>"
        f"<li>Cache-Control: public, max-age={PREFETCH_INTERVAL}s</li>"
        "<li>1 worker process + 8 threads — cache dùng chung, không fetch trùng lặp</li>"
        "<li>4 nguồn fetch song song (ThreadPoolExecutor)</li>"
        f"<li>Làm mới cache mỗi <strong>{PREFETCH_INTERVAL // 60} phút</strong></li>"
        "</ul>"
        "<h3>🔥 Pháo Hoa TV — Real-time Stream</h3><ul>"
        "<li>Hiển thị tất cả trận theo lịch (scheduled + live)</li>"
        "<li>Link stream lấy theo thời gian thực khi user mở kênh</li>"
        "<li>Proxy endpoint: <code>/ph_stream/&lt;slug&gt;</code> — fetch API → 302 redirect</li>"
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
