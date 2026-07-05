"""
VN Stream Relay — chạy trên VPS/máy nhà có IP Việt Nam.
Render/CF bị asynccdn chặn (403); relay này fetch từ IP VN rồi trả playlist/segment.

Deploy:
  pip install -r requirements.txt
  RELAY_SECRET=your-secret TIEULAM_FRONTEND=https://sv2.tieulam.info \
    gunicorn app:app --bind 0.0.0.0:8080

Render env:
  TIEULAM_VN_RELAY_URL=https://your-vn-vps.example.com
  TIEULAM_VN_RELAY_SECRET=your-secret
"""
import os
import re
from urllib.parse import quote, unquote, urljoin

import requests
from flask import Flask, Response, request

app = Flask(__name__)

SECRET   = os.environ.get("RELAY_SECRET", "")
FRONTEND = os.environ.get("TIEULAM_FRONTEND", "https://sv2.tieulam.info").rstrip("/")
UA       = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
ALLOWED  = ("asynccdn.com", "lilive1.eu.cc", "lilive3.eu.cc")


def _auth_ok() -> bool:
    return not SECRET or request.headers.get("X-Relay-Token", "") == SECRET


def _allowed(url: str) -> bool:
    return url.startswith(("http://", "https://")) and any(h in url for h in ALLOWED)


def _cdn_headers() -> dict:
    return {
        "User-Agent": UA,
        "Referer":    FRONTEND + "/",
        "Origin":     FRONTEND,
        "Accept":     "*/*",
    }


def _rewrite_m3u8(body: str, upstream: str, root: str) -> str:
    base = upstream.rsplit("/", 1)[0] + "/"

    def prox(raw: str) -> str:
        abs_u = raw if raw.startswith("http") else urljoin(base, raw.strip())
        return f"{root}/fetch?u={quote(abs_u, safe='')}"

    out: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                line = line.replace(m.group(0), f'URI="{prox(m.group(1))}"')
            out.append(line)
        else:
            out.append(prox(s))
    return "\n".join(out) + "\n"


@app.route("/healthz")
def healthz():
    return Response("OK", mimetype="text/plain")


@app.route("/fetch")
def fetch_proxy():
    if not _auth_ok():
        return Response("Unauthorized", status=401)
    url = (request.args.get("u") or "").strip()
    if not _allowed(url):
        return Response("Bad URL", status=400)
    try:
        r = requests.get(url, headers=_cdn_headers(), timeout=15)
    except Exception as e:
        return Response(str(e), status=502)
    ct = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    if r.status_code == 200 and b"#EXTM3U" in r.content[:256]:
        root = request.url_root.rstrip("/")
        text = _rewrite_m3u8(r.text, url, root)
        return Response(text, mimetype="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache"})
    mt = ct or ("video/mp2t" if url.endswith(".ts") else "application/octet-stream")
    return Response(r.content, status=r.status_code, mimetype=mt,
                    headers={"Cache-Control": "no-cache"})


@app.route("/test")
def test():
    if not _auth_ok():
        return Response("Unauthorized", status=401)
    sample = request.args.get("u", "")
    if not sample:
        return {"error": "pass ?u=asynccdn_url"}
    try:
        r = requests.get(sample, headers=_cdn_headers(), timeout=12)
        return {
            "url": sample,
            "status": r.status_code,
            "is_m3u8": b"#EXTM3U" in r.content[:256],
            "preview": r.text[:120] if r.text else "",
        }
    except Exception as e:
        return {"error": str(e)}
