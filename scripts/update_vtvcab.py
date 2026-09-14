#!/usr/bin/env python3
"""Safely replace the four VTVcab stream blocks in xemtv.m3u."""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE_URL = os.environ.get("FILM4K_BASE_URL", "https://film4k.net").rstrip("/")
TV_PATH = os.environ.get("FILM4K_TV_PATH", "/api/tv/")
LOGIN_PATH = os.environ.get("FILM4K_LOGIN_PATH", "/api/auth/signin")
TIMEOUT = int(os.environ.get("FILM4K_API_TIMEOUT", "20"))

TARGETS = {
    "onfootball": {"onfootball", "onfootballtv", "onfootballhd"},
    "onsports": {"onsports", "onsport", "onsportstv"},
    "onsportsplus": {"onsportsplus", "onsportplus", "onsportscong"},
    "onsportsnews": {"onsportsnews", "onsportsnewstv"},
}

STREAM_KEYS = (
    "url", "stream_url", "streamUrl", "stream", "manifest", "manifest_url",
    "manifestUrl", "mpd", "m3u8", "play_url", "playUrl", "sourceUrl", "src",
    "playback_url", "playbackUrl",
)
LICENSE_KEY_KEYS = ("license_key", "licenseKey", "clearkey", "clearKey", "drm_key", "drmKey")
LICENSE_URL_KEYS = ("license_url", "licenseUrl", "drm_url", "drmUrl")
LICENSE_TYPE_KEYS = ("license_type", "licenseType", "drm_type", "drmType")


def norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def iter_objects(value: object, hint: str = ""):
    if isinstance(value, dict):
        yield value, hint
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                yield from iter_objects(child, str(key))
    elif isinstance(value, list):
        for child in value:
            yield from iter_objects(child, hint)


def scalar(value: object) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def nested_dicts(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nested_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_dicts(child)


def matches_channel(obj: dict, hint: str, target: str) -> bool:
    aliases = TARGETS[target] | {target}
    candidates = [hint]
    for key in (
        "id", "channelId", "channel_id", "slug", "code", "tvgId", "tvg_id",
        "name", "title", "displayName", "display_name", "channelName", "channel_name",
    ):
        if key in obj:
            candidates.append(str(obj[key]))
    return any(norm(candidate) in aliases for candidate in candidates)


def valid_url(value: object) -> bool:
    text = scalar(value)
    return text.startswith(("http://", "https://"))


def find_stream(obj: dict) -> str:
    for current in nested_dicts(obj):
        for key in STREAM_KEYS:
            value = current.get(key)
            if valid_url(value):
                return scalar(value)
    return ""


def find_drm(obj: dict) -> dict[str, str]:
    drm: dict[str, str] = {}
    for current in nested_dicts(obj):
        for key in LICENSE_KEY_KEYS:
            value = scalar(current.get(key))
            if value:
                drm["license_key"] = value
        for key in LICENSE_URL_KEYS:
            value = scalar(current.get(key))
            if value and valid_url(value):
                drm["license_url"] = value
        for key in LICENSE_TYPE_KEYS:
            value = scalar(current.get(key))
            if value:
                drm["license_type"] = value
    return drm


def get_channel(payload: object, target: str) -> tuple[str, dict[str, str]]:
    for obj, hint in iter_objects(payload):
        if matches_channel(obj, hint, target):
            stream = find_stream(obj)
            if stream:
                return urljoin(BASE_URL + "/", stream), find_drm(obj)
    raise RuntimeError(f"Film4K API không có stream hợp lệ cho {target}")


def load_payload(session: requests.Session) -> object:
    email = os.environ.get("FILM4K_USERNAME", "")
    password = os.environ.get("FILM4K_PASSWORD", "")
    if not email or not password:
        raise RuntimeError("Thiếu FILM4K_USERNAME hoặc FILM4K_PASSWORD trong GitHub Secrets")

    login = session.post(
        urljoin(BASE_URL + "/", LOGIN_PATH.lstrip("/")),
        json={"email": email, "password": password},
        timeout=TIMEOUT,
    )
    login.raise_for_status()
    try:
        login_payload = login.json()
    except ValueError:
        login_payload = {}
    if isinstance(login_payload, dict):
        token = login_payload.get("token") or login_payload.get("accessToken")
        if token:
            session.headers["Authorization"] = f"Bearer {token}"

    paths = [TV_PATH]
    if TV_PATH.rstrip("/") == "/api/tv":
        paths.extend(["/api/tv/channels", "/api/tv/events"])

    failures = []
    for path in dict.fromkeys(paths):
        response = session.get(
            urljoin(BASE_URL + "/", path.lstrip("/")),
            timeout=TIMEOUT,
        )
        if response.status_code in {404, 405, 500, 502, 503, 504}:
            failures.append(f"{path}: HTTP {response.status_code}")
            continue
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            failures.append(f"{path}: response không phải JSON")

    detail = "; ".join(failures) if failures else "không có endpoint khả dụng"
    raise RuntimeError(f"Film4K TV API không khả dụng: {detail}")


def replace_block(block: list[str], stream: str, drm: dict[str, str]) -> list[str]:
    output = [line for line in block if not line.strip().startswith("http://") and not line.strip().startswith("https://")]
    if drm:
        output = [
            line for line in output
            if not line.startswith("#KODIPROP:inputstream.adaptive.license_")
        ]
        insert_at = len(output)
        for index, line in enumerate(output):
            if line.startswith("#KODIPROP:"):
                insert_at = index + 1
        props = []
        if drm.get("license_type"):
            props.append(f"#KODIPROP:inputstream.adaptive.license_type={drm['license_type']}")
        if drm.get("license_key"):
            props.append(f"#KODIPROP:inputstream.adaptive.license_key={drm['license_key']}")
        if drm.get("license_url"):
            props.append(f"#KODIPROP:inputstream.adaptive.license_url={drm['license_url']}")
        output[insert_at:insert_at] = props
    output.append(stream)
    return output


def update_playlist(path: Path, channels: dict[str, tuple[str, dict[str, str]]]) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines()
    result: list[str] = []
    seen: dict[str, int] = {target: 0 for target in TARGETS}
    index = 0
    tvg_re = re.compile(r'tvg-id="([^"]+)"', re.IGNORECASE)

    while index < len(lines):
        if not lines[index].startswith("#EXTINF:"):
            result.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("#EXTINF:"):
            end += 1
        block = lines[index:end]
        match = tvg_re.search(block[0])
        target = norm(match.group(1)) if match else ""
        if target in channels:
            seen[target] += 1
            block = replace_block(block, *channels[target])
        result.extend(block)
        index = end

    missing = [target for target, count in seen.items() if count != 1]
    if missing:
        raise RuntimeError("Playlist phải có đúng một block cho mỗi kênh: " + ", ".join(missing))

    new_text = "\n".join(result) + "\n"
    old_text = path.read_text(encoding="utf-8")
    if new_text != old_text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "xemtv.m3u")
    if not path.is_file():
        raise RuntimeError(f"Không tìm thấy playlist: {path}")

    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "Bongda-VTVcab-Updater/1.0"})
    payload = load_payload(session)
    channels = {target: get_channel(payload, target) for target in TARGETS}
    changed = update_playlist(path, channels)
    print(json.dumps({"ok": True, "changed": changed, "channels_updated": sorted(channels)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
