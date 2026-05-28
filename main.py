import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta

import cloudscraper
import requests
from flask import Flask, Response

app = Flask(__name__)

# ─── Cola TV config ───────────────────────────────────────────────────────────
COLATV_FRONTEND_URL = os.environ.get("COLATV_FRONTEND", "https://colatv48.live")
COLATV_KNOWN_API_URL = os.environ.get("COLATV_API", "https://api.cltvlv.com/api/matches")

# ─── Hội Quán TV config ───────────────────────────────────────────────────────
HOIQUAN_FRONTEND_URL = os.environ.get("HOIQUAN_FRONTEND", "https://sv2.hoiquan4.live")
HOIQUAN_KNOWN_API_BASE = os.environ.get("HOIQUAN_API", "https://sv.hoiquantv.xyz/api/v1/external")

# ─── Shared config ────────────────────────────────────────────────────────────
VN_TZ = timezone(timedelta(hours=7))
SELF_PING_INTERVAL   = 240   # seconds — beat Replit / Render idle timeout
PREFETCH_INTERVAL    = 300   # seconds — refresh cache every 5 minutes
API_DISCOVERY_TTL    = 3600  # seconds — re-discover API URL every 1 hour

COLATV_FINISHED_STATUS_INT  = {3}
FINISHED_STATUS_STRINGS     = {"finished", "end", "ended", "complete", "completed"}
# A match that started more than this many seconds ago and is NOT flagged live
# is considered over and dropped from the list.
MATCH_MAX_AGE_SECONDS = int(os.environ.get("MATCH_MAX_DURATION", 7200))  # 2 h

# ─── Sport logos (Twemoji via jsDelivr — stable, no auth needed) ──────────────
_CDN = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
SPORT_LOGOS = {
    "football":   f"{_CDN}/26bd.png",   # ⚽
    "tennis":     f"{_CDN}/1f3be.png",  # 🎾
    "basketball": f"{_CDN}/1f3c0.png",  # 🏀
    "volleyball": f"{_CDN}/1f3d0.png",  # 🏐
    "billiards":  f"{_CDN}/1f3b1.png",  # 🎱
    "badminton":  f"{_CDN}/1f3f8.png",  # 🏸
    "default":    f"{_CDN}/1f3c6.png",  # 🏆
}

# ─── API URL caches ───────────────────────────────────────────────────────────
_colatv_api_cache  = {"url": COLATV_KNOWN_API_URL,  "discovered_at": 0}
_hoiquan_api_cache = {"url": HOIQUAN_KNOWN_API_BASE, "discovered_at": 0}

# ─── Playlist content cache ───────────────────────────────────────────────────
_playlist_cache = {
    "combined": {"content": None, "built_at": 0, "lock": threading.Lock()},
    "cola":     {"content": None, "built_at": 0, "lock": threading.Lock()},
    "hoiquan":  {"content": None, "built_at": 0, "lock": threading.Lock()},
}
_last_counts = {"cola": 0, "hoiquan": 0, "refreshed_at": 0, "last_error": ""}


# ══════════════════════════════════════════════════════════════════════════════
#  Sport logo helpers
# ══════════════════════════════════════════════════════════════════════════════

def _logo_from_text(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["tennis"]):
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


def _hoiquan_logo(fixture: dict) -> str:
    sport = fixture.get("sport") or {}
    # Use the icon URL the API already provides if available
    icon = sport.get("iconUrl", "")
    if icon:
        return icon
    parts = " ".join([sport.get("name", ""), sport.get("slug", "")])
    return _logo_from_text(parts)


# ══════════════════════════════════════════════════════════════════════════════
#  Cola TV — API discovery
# ══════════════════════════════════════════════════════════════════════════════

def _discover_colatv_api(scraper) -> str:
    try:
        r = scraper.get(COLATV_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            return COLATV_KNOWN_API_URL
        js_url = COLATV_FRONTEND_URL.rstrip("/") + js_files[0]
        js = scraper.get(js_url, timeout=15).text
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
        url = _discover_colatv_api(scraper)
        _colatv_api_cache["url"] = url
        _colatv_api_cache["discovered_at"] = now
    return _colatv_api_cache["url"]


# ══════════════════════════════════════════════════════════════════════════════
#  Cola TV — fetch & filter
# ══════════════════════════════════════════════════════════════════════════════

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

        logo      = _cola_logo(match)
        match_time  = match.get("matchTime", 0)
        home       = match.get("homeTeamName", "Home")
        away       = match.get("awayTeamName", "Away")
        competition = match.get("competitionName", "")

        dt = datetime.fromtimestamp(match_time, tz=VN_TZ)
        time_str = dt.strftime("%H:%M")
        date_str = dt.strftime("%d/%m")

        anchors = match.get("anchorAppointmentVoList", [])
        if anchors:
            for anchor in anchors:
                stream_url = (
                    anchor.get("playStreamAddress2") or anchor.get("playStreamAddress", "")
                )
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
#  Hội Quán TV — API discovery
# ══════════════════════════════════════════════════════════════════════════════

def _discover_hoiquan_api(scraper) -> str:
    try:
        r = scraper.get(HOIQUAN_FRONTEND_URL, timeout=10)
        js_files = re.findall(r'src="(/assets/js/[^"]+\.js)"', r.text)
        if not js_files:
            js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if not js_files:
            return HOIQUAN_KNOWN_API_BASE
        js_url = HOIQUAN_FRONTEND_URL.rstrip("/") + js_files[0]
        js = scraper.get(js_url, timeout=15).text
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
        url = _discover_hoiquan_api(scraper)
        _hoiquan_api_cache["url"] = url
        _hoiquan_api_cache["discovered_at"] = now
    return _hoiquan_api_cache["url"]


# ══════════════════════════════════════════════════════════════════════════════
#  Hội Quán TV — fetch & filter
# ══════════════════════════════════════════════════════════════════════════════

_HQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


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


def _hoiquan_is_active(fixture: dict) -> bool:
    """
    Drop a fixture if any of these is true:
    1. status string signals it's over
    2. isFinished / isEnd flag is set
    3. Not live AND started more than MATCH_MAX_AGE_SECONDS ago
    4. Not live, status == "active" (= was started) AND started > 90 min ago
       — covers matches the API server is slow to mark as finished
    """
    # 1. Status string
    status = str(fixture.get("status") or "").lower().strip()
    if status in FINISHED_STATUS_STRINGS:
        return False

    # 2. Boolean flags
    if fixture.get("isFinished") or fixture.get("isEnd"):
        return False

    is_live = bool(fixture.get("isLive"))
    start_time_str = fixture.get("startTime", "")

    if start_time_str and not is_live:
        try:
            dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            elapsed = time.time() - dt.timestamp()

            # 3. Hard cap: started more than 2 h ago and not live → drop
            if elapsed > MATCH_MAX_AGE_SECONDS:
                return False

            # 4. Soft cap for "active" (started) matches:
            #    if the server says it was started but it's no longer live
            #    and 90 min have passed, assume it's over.
            if status == "active" and elapsed > 5400:  # 90 min
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


def _build_hoiquan_lines(fixtures: list) -> list:
    # Sort chronologically
    try:
        fixtures = sorted(fixtures, key=lambda f: f.get("startTime") or "")
    except Exception:
        pass

    lines = []
    for fixture in fixtures:
        if not _hoiquan_is_active(fixture):
            continue

        logo      = _hoiquan_logo(fixture)
        start_str = fixture.get("startTime", "")
        home      = fixture.get("homeTeam", {}).get("name", "Home").strip()
        away      = fixture.get("awayTeam", {}).get("name", "Away").strip()
        league    = fixture.get("league", {}).get("name", "")

        try:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            dt_vn = dt.astimezone(VN_TZ)
            time_str = dt_vn.strftime("%H:%M")
            date_str = dt_vn.strftime("%d/%m")
        except Exception:
            time_str = "--:--"
            date_str = "--/--"

        for entry in fixture.get("fixtureCommentators", []):
            commentator_obj = entry.get("commentator", {})
            name = (
                commentator_obj.get("nickname") or commentator_obj.get("name") or ""
            ).strip()
            stream_url = _pick_best_stream(commentator_obj.get("streams", []))
            if not stream_url:
                continue
            display = f"{time_str} - {date_str} | {home} VS {away} ({league}) | {name}"
            lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="Hội Quán TV",{display}')
            lines.append(stream_url)

    return lines


# ══════════════════════════════════════════════════════════════════════════════
#  Background pre-fetch & cache
# ══════════════════════════════════════════════════════════════════════════════

def _refresh_all_playlists():
    cola_lines, hq_lines = [], []
    cola_count = hq_count = 0
    errors = []

    try:
        matches = _fetch_colatv_matches()
        cola_lines = _build_colatv_lines(matches)
        cola_count = sum(1 for l in cola_lines if l.startswith("#EXTINF"))
    except Exception as e:
        errors.append(f"Cola TV: {e}")

    try:
        fixtures = _fetch_hoiquan_fixtures()
        hq_lines = _build_hoiquan_lines(fixtures)
        hq_count = sum(1 for l in hq_lines if l.startswith("#EXTINF"))
    except Exception as e:
        errors.append(f"Hội Quán TV: {e}")

    now = time.time()
    err_str = "; ".join(errors)

    combined = "\n".join(["#EXTM3U"] + cola_lines + hq_lines)
    if err_str:
        combined += f"\n# Errors: {err_str}"

    cola_only = "\n".join(["#EXTM3U"] + cola_lines)
    hq_only   = "\n".join(["#EXTM3U"] + hq_lines)

    with _playlist_cache["combined"]["lock"]:
        _playlist_cache["combined"]["content"]  = combined
        _playlist_cache["combined"]["built_at"] = now

    with _playlist_cache["cola"]["lock"]:
        _playlist_cache["cola"]["content"]  = cola_only
        _playlist_cache["cola"]["built_at"] = now

    with _playlist_cache["hoiquan"]["lock"]:
        _playlist_cache["hoiquan"]["content"]  = hq_only
        _playlist_cache["hoiquan"]["built_at"] = now

    _last_counts["cola"]         = cola_count
    _last_counts["hoiquan"]      = hq_count
    _last_counts["refreshed_at"] = now
    _last_counts["last_error"]   = err_str


def _prefetch_loop():
    time.sleep(3)
    while True:
        try:
            _refresh_all_playlists()
        except Exception:
            pass
        time.sleep(PREFETCH_INTERVAL)


def _get_cached(key: str):
    entry = _playlist_cache[key]
    with entry["lock"]:
        return entry["content"]


# ══════════════════════════════════════════════════════════════════════════════
#  Flask routes
# ══════════════════════════════════════════════════════════════════════════════

def _m3u_response(content: str, filename: str) -> Response:
    return Response(
        content,
        mimetype="application/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _serve(key: str, filename: str) -> Response:
    content = _get_cached(key)
    if content is None:
        try:
            _refresh_all_playlists()
            content = _get_cached(key) or "#EXTM3U\n# No data yet"
        except Exception as e:
            return Response(f"Error: {e}", status=500, mimetype="text/plain")
    return _m3u_response(content, filename)


@app.route("/live.m3u")
def live_m3u():
    return _serve("combined", "live.m3u")


@app.route("/cola.m3u")
def cola_m3u():
    return _serve("cola", "cola.m3u")


@app.route("/hoiquan.m3u")
def hoiquan_m3u():
    return _serve("hoiquan", "hoiquan.m3u")


@app.route("/ping")
def ping():
    return Response("OK", mimetype="text/plain")


@app.route("/")
def index():
    refreshed_at = _last_counts.get("refreshed_at", 0)
    if refreshed_at:
        dt_str  = datetime.fromtimestamp(refreshed_at, tz=VN_TZ).strftime("%H:%M:%S %d/%m/%Y")
        next_s  = max(int(PREFETCH_INTERVAL - (time.time() - refreshed_at)), 0)
        next_str = f"{next_s}s"
    else:
        dt_str  = "chưa có dữ liệu"
        next_str = "đang khởi động..."

    err = _last_counts.get("last_error", "")
    err_html = f'<p style="color:red">⚠️ {err}</p>' if err else ""

    return (
        "<h2>🎬 IPTV M3U Server</h2>"
        "<h3>📋 Playlist</h3><ul>"
        "<li><a href='/live.m3u'>/live.m3u</a> — Cola TV + Hội Quán TV (gộp)</li>"
        "<li><a href='/cola.m3u'>/cola.m3u</a> — Cola TV only</li>"
        "<li><a href='/hoiquan.m3u'>/hoiquan.m3u</a> — Hội Quán TV only</li>"
        "</ul>"
        "<h3>📊 Trạng thái</h3>"
        f"<p>🕐 Cập nhật lần cuối: <strong>{dt_str}</strong></p>"
        f"<p>⏳ Cập nhật tiếp theo trong: <strong>{next_str}</strong></p>"
        f"<p>📺 Cola TV: <strong>{_last_counts.get('cola', 0)} kênh</strong>"
        f"&nbsp;|&nbsp; API: <code>{_colatv_api_cache['url']}</code></p>"
        f"<p>📺 Hội Quán TV: <strong>{_last_counts.get('hoiquan', 0)} kênh</strong>"
        f"&nbsp;|&nbsp; API: <code>{_hoiquan_api_cache['url']}</code></p>"
        f"{err_html}"
        "<h3>⚙️ Lọc trận kết thúc</h3>"
        f"<p>Xoá trận không live và bắt đầu trước "
        f"<strong>{MATCH_MAX_AGE_SECONDS // 3600}h</strong>. "
        "Xoá trận 'active' không live sau 90 phút.</p>"
        f"<p>🔁 Cache làm mới mỗi <strong>{PREFETCH_INTERVAL // 60} phút</strong>.</p>"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Keep-alive self-ping
# ══════════════════════════════════════════════════════════════════════════════

def _get_ping_url() -> str:
    # Replit production domain
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        return f"https://{domains.split(',')[0].strip()}/"
    # Render automatically sets this on deployed services
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render_url:
        return render_url.rstrip("/") + "/"
    # Custom domain (set APP_URL env var on any platform)
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
