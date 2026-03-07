"""
Facebook Video URL Extractor — Render Worker
============================================
Exposes a single endpoint:
  POST /extract   { "url": "<facebook_reel_or_video_url>" }
  → 200           { "url": "<direct_mp4_url>" }
  → 4xx/5xx       { "error": "<message>" }

Deploy on Render (free tier, Web Service, Python):
  Build command : pip install -r requirements.txt
  Start command : python server.py
  Instance type : Free

Set environment variable FB_COOKIES (optional but recommended for Reels):
  Export cookies from a logged-in browser session using a browser extension
  like "Get cookies.txt LOCALLY", paste the Netscape-format content as the
  FB_COOKIES env var value on Render. yt-dlp will use them automatically.

The worker writes FB_COOKIES to a temp file on startup so yt-dlp can read it.
"""

import os
import json
import tempfile
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8080))
COOKIES_FILE = None


def setup_cookies():
    """Write FB_COOKIES env var to a temp file yt-dlp can read."""
    global COOKIES_FILE
    cookies_env = os.environ.get("FB_COOKIES", "").strip()
    if cookies_env:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        tmp.write(cookies_env)
        tmp.close()
        COOKIES_FILE = tmp.name
        print(f"[startup] Cookies written to {COOKIES_FILE}")
    else:
        print("[startup] No FB_COOKIES env var set — Reels may fail without login")


def extract_url(fb_url: str) -> dict:
    """
    Run yt-dlp to extract the best video URL without downloading.
    Returns {"url": "..."} on success or {"error": "..."} on failure.
    """
    cmd = [
        "yt-dlp",
        "--get-url",
        "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--no-playlist",
    ]
    if COOKIES_FILE:
        cmd += ["--cookies", COOKIES_FILE]
    cmd.append(fb_url)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            urls = [u.strip() for u in result.stdout.strip().splitlines() if u.strip()]
            if urls:
                # yt-dlp may return two lines (video + audio) for separate streams.
                # The first line is always the video stream URL — use that.
                return {"url": urls[0]}
            return {"error": "yt-dlp returned no URLs"}
        else:
            err = result.stderr.strip()[:500]
            print(f"[yt-dlp error] {err}")
            return {"error": f"yt-dlp failed: {err}"}
    except subprocess.TimeoutExpired:
        return {"error": "yt-dlp timed out after 30s"}
    except FileNotFoundError:
        return {"error": "yt-dlp not found — check requirements.txt"}
    except Exception as e:
        return {"error": str(e)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[http] {self.address_string()} — {format % args}")

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/extract":
            self._respond(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid JSON"})
            return

        url = data.get("url", "").strip()
        if not url:
            self._respond(400, {"error": "missing 'url' field"})
            return

        print(f"[extract] {url[:100]}")
        result = extract_url(url)
        status = 200 if "url" in result else 500
        self._respond(status, result)

    def _respond(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    setup_cookies()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[startup] Listening on port {PORT}")
    server.serve_forever()
