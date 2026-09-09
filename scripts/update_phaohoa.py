#!/usr/bin/env python3
"""Refresh the Pháo Hoa TV block embedded in xemtv.m3u.

The API is intentionally queried at update time so the GitHub raw playlist
stays usable by clients that do not call the Render/Replit server endpoint.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API_URL = os.environ.get("PHAOHOA_API", "https://khandai3.link/api/matches/")
OUTPUT = Path(os.environ.get("PHAOHOA_OUTPUT", "xemtv.m3u"))
FRONTEND = os.environ.get("PHAOHOA_FRONTEND", "https://khandai3.link").rstrip("/")
GROUP = "Pháo Hoa TV"
START_MARKER = "# === PHAO HOA TV AUTO-UPDATE BEGIN ==="
END_MARKER = "# === PHAO HOA TV AUTO-UPDATE END ==="
VN_TZ = timezone(timedelta(hours=7))
HEADERS = {
    "Accept": "application/json",
    "Referer": FRONTEND + "/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
}


def fetch_matches() -> list[dict]:
    matches: list[dict] = []
    seen: set[str] = set()
    for status in ("live", "scheduled"):
        response = requests.get(
            API_URL,
            params={"status": status, "ordering": "start_time"},
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise RuntimeError("Pháo Hoa API returned an invalid results list")
        for match in results:
            key = str(match.get("id") or match.get("slug") or "")
            if key and key not in seen:
                seen.add(key)
                matches.append(match)

    if not matches:
        raise RuntimeError("Pháo Hoa API returned no live or scheduled matches")
    return sorted(matches, key=lambda match: match.get("start_time") or "")


def stream_for(match: dict) -> tuple[str, str]:
    for commentator in match.get("commentators") or []:
        if not isinstance(commentator, dict):
            continue
        stream = (commentator.get("stream_url") or commentator.get("streamUrl") or "").strip()
        name = (commentator.get("name") or commentator.get("nickname") or "").strip()
        if stream:
            return stream, name
    for key in ("primary_stream_url", "backup_stream_url"):
        stream = (match.get(key) or "").strip()
        if stream:
            return stream, ""
    return "", ""


def absolute_url(value: str) -> str:
    if value.startswith("/"):
        return FRONTEND + value
    return value


def render_block(matches: list[dict]) -> str:
    lines = [START_MARKER, "# Generated from Pháo Hoa API — do not edit this block manually."]
    for match in matches:
        stream, commentator = stream_for(match)
        slug = str(match.get("slug") or "").strip()
        if not stream or not slug:
            continue
        start_time = str(match.get("start_time") or "")
        try:
            dt = datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(VN_TZ)
            when = dt.strftime("%H:%M - %d/%m")
        except ValueError:
            when = "--:-- - --/--"
        home = str(match.get("home_team_name") or "Home").strip()
        away = str(match.get("away_team_name") or "Away").strip()
        tournament = str(match.get("tournament_name") or "").strip()
        suffix = f" | {commentator}" if commentator else ""
        title = f"{when} | {home} VS {away}"
        if tournament:
            title += f" ({tournament})"
        title += suffix
        title = title.replace("\n", " ").replace("\r", " ").replace('"', "'")
        logo = absolute_url(str(match.get("sport_icon_url") or ""))
        logo_attr = f' tvg-logo="{logo}"' if logo else ""
        lines.append(f'#EXTINF:-1{logo_attr} group-title="{GROUP}",{title}')
        lines.append(
            stream
            + "|Referer="
            + FRONTEND
            + "/&User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        )
    lines.append(END_MARKER)
    if len(lines) <= 3:
        raise RuntimeError("No Pháo Hoa match had a usable stream URL")
    return "\n".join(lines)


def update_file(path: Path, block: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
        flags=re.DOTALL,
    )
    if pattern.search(text):
        updated = pattern.sub(block, text, count=1)
    else:
        updated = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(updated, encoding="utf-8")


def main() -> int:
    try:
        matches = fetch_matches()
        block = render_block(matches)
        update_file(OUTPUT, block)
        count = block.count("#EXTINF")
        print(f"Updated {OUTPUT}: {count} Pháo Hoa entries")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
