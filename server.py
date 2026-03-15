"""
Facebook Video URL Extractor + Compressor — Render Worker
==========================================================
Endpoints:
  POST /extract    { "url": "<facebook_reel_or_video_url>" }
  → 200            { "url": "<direct_mp4_url>" }
  → 4xx/5xx        { "error": "<message>" }

  POST /compress   { "url": "<any_direct_mp4_url>" }
  → 200            <raw mp4 bytes>   (Content-Type: video/mp4)
  → 4xx/5xx        { "error": "<message>" }   (Content-Type: application/json)

  /compress downloads the video, re-encodes it with FFmpeg targeting a file
  size safely under the Discord 8 MB attachment limit (target: 7 MB), and
  streams the compressed bytes back to the caller.

  FFmpeg strategy:
    - Two-pass is ideal but slow on free Render instances; single-pass CRF
      with a hard bitrate ceiling is fast enough and reliable.
    - Video: libx264, CRF 28, max bitrate capped to keep output ≤ 7 MB for
      clips up to ~3 minutes. For longer clips the bitrate is calculated
      dynamically from the actual duration.
    - Audio: AAC 64k (keeps voice/music clear while spending bits on video).
    - Resolution: scaled down to 720p max (keeps quality high, size low).
    - If the source is already under 7 MB it is returned as-is without
      re-encoding to avoid unnecessary quality loss.

Deploy on Render (free tier, Web Service, Python):
  Build command : pip install -r requirements.txt
  Start command : python server.py
  Instance type : Free  (upgrade to Starter for faster FFmpeg encodes)

Environment variables:
  FB_COOKIES  (optional) Netscape-format cookie string for logged-in Reels
  TARGET_MB   (optional) override the default 7 MB target, e.g. "6"
"""

import io
import os
import json
import math
import tempfile
import subprocess
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT        = int(os.environ.get("PORT", 8080))
TARGET_MB   = float(os.environ.get("TARGET_MB", "7"))
TARGET_BYTES = int(TARGET_MB * 1024 * 1024)
COOKIES_FILE = None


# ─── STARTUP ──────────────────────────────────────────────────────────────────

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


# ─── /extract ─────────────────────────────────────────────────────────────────

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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
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


# ─── /compress ────────────────────────────────────────────────────────────────

def get_duration(input_path: str) -> float:
    """Return video duration in seconds using ffprobe, or 0.0 on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "format=duration",
                "-of", "json",
                input_path
            ],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception as e:
        print(f"[ffprobe] duration error: {e}")
        return 0.0


def compress_video(source_url: str) -> tuple[bytes | None, str | None]:
    """
    Download video from source_url, compress to ≤ TARGET_BYTES with FFmpeg.

    Returns (compressed_bytes, None) on success
         or (None, error_string)    on failure.

    If the source is already under TARGET_BYTES it is returned as-is.
    """
    # ── Download ──────────────────────────────────────────────────────────────
    print(f"[compress] downloading: {source_url[:100]}")
    try:
        req = urllib.request.Request(source_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw_bytes = resp.read()
    except Exception as e:
        return None, f"download failed: {e}"

    raw_size = len(raw_bytes)
    print(f"[compress] downloaded {raw_size / 1024 / 1024:.2f} MB")

    # ── Already small enough — return as-is ──────────────────────────────────
    if raw_size <= TARGET_BYTES:
        print(f"[compress] already under {TARGET_MB} MB — returning as-is")
        return raw_bytes, None

    # ── Write to temp input file ──────────────────────────────────────────────
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f_in:
        f_in.write(raw_bytes)
        input_path = f_in.name

    output_path = input_path.replace(".mp4", "_out.mp4")

    try:
        # ── Probe duration to calculate target bitrate ────────────────────────
        duration = get_duration(input_path)
        if duration > 0:
            # Reserve 64k for audio; give the rest to video.
            # total_bitrate (kbps) = (TARGET_BYTES * 8) / duration / 1000
            total_kbps = int((TARGET_BYTES * 8) / duration / 1000)
            video_kbps = max(total_kbps - 64, 100)  # floor at 100 kbps
            print(f"[compress] duration={duration:.1f}s → video bitrate target: {video_kbps} kbps")
        else:
            # Unknown duration — use a conservative fixed bitrate
            video_kbps = 400
            print(f"[compress] duration unknown — using fixed {video_kbps} kbps")

        # ── FFmpeg single-pass encode ─────────────────────────────────────────
        # -vf scale: scale down to 720p max, preserve aspect ratio, ensure
        #            dimensions are divisible by 2 (required by libx264).
        # -crf 28:   quality floor — won't waste bits if bitrate ceiling allows more.
        # -maxrate / -bufsize: hard bitrate ceiling to guarantee output size.
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vf", "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264",
            "-crf", "28",
            "-maxrate", f"{video_kbps}k",
            "-bufsize", f"{video_kbps * 2}k",
            "-c:a", "aac",
            "-b:a", "64k",
            "-movflags", "+faststart",  # web-optimised: moov atom at front
            "-preset", "fast",          # encode speed vs compression tradeoff
            output_path
        ]

        print(f"[compress] running FFmpeg...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

        if result.returncode != 0:
            err = result.stderr.strip()[-600:]
            print(f"[ffmpeg error] {err}")
            return None, f"FFmpeg failed: {err}"

        with open(output_path, "rb") as f_out:
            compressed = f_out.read()

        out_size = len(compressed)
        print(f"[compress] output: {out_size / 1024 / 1024:.2f} MB  (was {raw_size / 1024 / 1024:.2f} MB)")
        return compressed, None

    except subprocess.TimeoutExpired:
        return None, "FFmpeg timed out after 180s"
    except Exception as e:
        return None, str(e)
    finally:
        # Clean up temp files
        for p in (input_path, output_path):
            try:
                os.unlink(p)
            except Exception:
                pass


# ─── HTTP HANDLER ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[http] {self.address_string()} — {format % args}")

    def do_GET(self):
        if self.path == "/health":
            self._respond_json(200, {"status": "ok"})
        else:
            self._respond_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond_json(400, {"error": "invalid JSON"})
            return

        if self.path == "/extract":
            self._handle_extract(data)
        elif self.path == "/compress":
            self._handle_compress(data)
        else:
            self._respond_json(404, {"error": "not found"})

    def _handle_extract(self, data: dict):
        url = data.get("url", "").strip()
        if not url:
            self._respond_json(400, {"error": "missing 'url' field"})
            return
        print(f"[extract] {url[:100]}")
        result = extract_url(url)
        status = 200 if "url" in result else 500
        self._respond_json(status, result)

    def _handle_compress(self, data: dict):
        url = data.get("url", "").strip()
        if not url:
            self._respond_json(400, {"error": "missing 'url' field"})
            return

        print(f"[compress] request for: {url[:100]}")
        compressed, error = compress_video(url)

        if error:
            self._respond_json(500, {"error": error})
            return

        # Return raw mp4 bytes — caller (Apps Script) reads them directly
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(compressed)))
        self.send_header("X-Original-Size", str(len(compressed)))
        self.end_headers()
        self.wfile.write(compressed)

    def _respond_json(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_cookies()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[startup] Listening on port {PORT}  |  target size: {TARGET_MB} MB")
    server.serve_forever()
