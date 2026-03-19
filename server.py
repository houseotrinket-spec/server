"""
Facebook Video URL Extractor + Async Compressor — Render Worker
===============================================================
Endpoints:
  GET  /health                → 200 { "status": "ok" }

  POST /extract               { "url": "<facebook_url>" }
  → 200                       { "url": "<direct_mp4_url>" }
  → 500                       { "error": "..." }

  POST /compress/start        { "url": "<direct_mp4_url>" }
  → 200                       { "job_id": "<uuid>" }
    Kicks off FFmpeg in a background thread and returns immediately.
    Render's 30-second proxy timeout is avoided because this returns in ~1ms.

  GET  /compress/result/<id>
  → 202                       { "status": "processing" }   (still running)
  → 200                       <raw mp4 bytes>              (done — content-type: video/mp4)
  → 500                       { "error": "..." }           (FFmpeg failed)
    After a 200 the job is deleted from memory automatically.

FFmpeg strategy:
  - Single-pass CRF 28, libx264, 720p max, AAC 64k.
  - Bitrate ceiling calculated from actual clip duration to hit TARGET_MB.
  - If source is already ≤ TARGET_MB it is returned as-is (no re-encode).
  - Jobs are kept in memory; the worker is stateless across restarts.
    If the worker restarts while a job is running, the next poll returns 404
    and Script 2 treats that as a failure and falls back to Vimeo.

Deploy on Render (free tier, Web Service, Python):
  Build command : pip install -r requirements.txt
  Start command : python server.py

Environment variables:
  FB_COOKIES   (optional) Netscape cookie string for logged-in FB Reels
  TARGET_MB    (optional) file size target in MB, default 7
"""
import os
os.environ["PATH"] = "/opt/ffmpeg:" + os.environ.get("PATH", "")

import io
import json
import uuid
import tempfile
import threading
import subprocess
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT         = int(os.environ.get("PORT", 8080))
TARGET_MB    = float(os.environ.get("TARGET_MB", "7"))
TARGET_BYTES = int(TARGET_MB * 1024 * 1024)
COOKIES_FILE = None

# ── In-memory job store ───────────────────────────────────────────────────────
# { job_id: { "status": "processing"|"done"|"error",
#             "data": bytes|None,
#             "error": str|None } }
JOBS = {}
JOBS_LOCK = threading.Lock()


# ─── STARTUP ──────────────────────────────────────────────────────────────────

def setup_cookies():
    global COOKIES_FILE
    cookies_env = os.environ.get("FB_COOKIES", "").strip()
    if cookies_env:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        tmp.write(cookies_env)
        tmp.close()
        COOKIES_FILE = tmp.name
        print(f"[startup] Cookies written to {COOKIES_FILE}")
    else:
        print("[startup] No FB_COOKIES — Reels may fail without login")


# ─── /extract ─────────────────────────────────────────────────────────────────

def extract_url(fb_url: str) -> dict:
    cmd = ["yt-dlp", "--get-url",
           "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
           "--no-playlist"]
    if COOKIES_FILE:
        cmd += ["--cookies", COOKIES_FILE]
    cmd.append(fb_url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            urls = [u.strip() for u in result.stdout.strip().splitlines() if u.strip()]
            if urls:
                return {"url": urls[0]}
            return {"error": "yt-dlp returned no URLs"}
        return {"error": f"yt-dlp failed: {result.stderr.strip()[:500]}"}
    except subprocess.TimeoutExpired:
        return {"error": "yt-dlp timed out after 30s"}
    except FileNotFoundError:
        return {"error": "yt-dlp not found"}
    except Exception as e:
        return {"error": str(e)}


# ─── /compress internals ──────────────────────────────────────────────────────

def get_duration(input_path: str) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "format=duration", "-of", "json", input_path],
            capture_output=True, text=True, timeout=15)
        return float(json.loads(result.stdout)["format"]["duration"])
    except Exception as e:
        print(f"[ffprobe] duration error: {e}")
        return 0.0


def compress_video(source_url: str) -> tuple:
    """
    Download + compress. Returns (bytes, None) on success or (None, error_str).
    Runs in a background thread — no HTTP timeout constraints.
    """
    print(f"[compress] downloading: {source_url[:100]}")
    try:
        req = urllib.request.Request(source_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw_bytes = resp.read()
    except Exception as e:
        return None, f"download failed: {e}"

    raw_size = len(raw_bytes)
    print(f"[compress] downloaded {raw_size / 1024 / 1024:.2f} MB")

    if raw_size <= TARGET_BYTES:
        print(f"[compress] already ≤ {TARGET_MB} MB — returning as-is")
        return raw_bytes, None

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f_in:
        f_in.write(raw_bytes)
        input_path = f_in.name
    output_path = input_path.replace(".mp4", "_out.mp4")

    try:
        duration = get_duration(input_path)
        if duration > 0:
            total_kbps = int((TARGET_BYTES * 8) / duration / 1000)
            video_kbps = max(total_kbps - 64, 100)
            print(f"[compress] duration={duration:.1f}s → video bitrate: {video_kbps} kbps")
        else:
            video_kbps = 400
            print(f"[compress] unknown duration — using {video_kbps} kbps")

        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", "scale='min(1440,iw)':'min(900,ih)':force_original_aspect_ratio=decrease,"
                   "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-crf", "28",
            "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
            "-c:a", "aac", "-b:a", "64k",
            "-movflags", "+faststart", "-preset", "fast",
            output_path
        ]
        print("[compress] running FFmpeg...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            err = result.stderr.strip()[-600:]
            print(f"[ffmpeg error] {err}")
            return None, f"FFmpeg failed: {err}"

        with open(output_path, "rb") as f:
            compressed = f.read()

        print(f"[compress] output: {len(compressed)/1024/1024:.2f} MB (was {raw_size/1024/1024:.2f} MB)")
        return compressed, None

    except subprocess.TimeoutExpired:
        return None, "FFmpeg timed out after 300s"
    except Exception as e:
        return None, str(e)
    finally:
        for p in (input_path, output_path):
            try:
                os.unlink(p)
            except Exception:
                pass


def run_compress_job(job_id: str, source_url: str):
    """Background thread target — runs compress_video and stores result in JOBS."""
    print(f"[job:{job_id}] started")
    data, error = compress_video(source_url)
    with JOBS_LOCK:
        if error:
            JOBS[job_id] = {"status": "error", "data": None, "error": error}
            print(f"[job:{job_id}] failed: {error}")
        else:
            JOBS[job_id] = {"status": "done", "data": data, "error": None}
            print(f"[job:{job_id}] done — {len(data)/1024/1024:.2f} MB")


# ─── HTTP HANDLER ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[http] {self.address_string()} — {format % args}")

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})

        elif self.path.startswith("/compress/result/"):
            job_id = self.path.split("/compress/result/")[-1].strip("/")
            with JOBS_LOCK:
                job = JOBS.get(job_id)

            if job is None:
                # Worker may have restarted — caller should treat as failure
                self._json(404, {"error": "job not found (worker may have restarted)"})

            elif job["status"] == "processing":
                self._json(202, {"status": "processing"})

            elif job["status"] == "error":
                with JOBS_LOCK:
                    JOBS.pop(job_id, None)
                self._json(500, {"error": job["error"]})

            else:  # done
                data = job["data"]
                with JOBS_LOCK:
                    JOBS.pop(job_id, None)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        else:
            self._json(404, {"error": "not found"})

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return

        if self.path == "/extract":
            url = data.get("url", "").strip()
            if not url:
                self._json(400, {"error": "missing 'url'"}); return
            print(f"[extract] {url[:100]}")
            result = extract_url(url)
            self._json(200 if "url" in result else 500, result)

        elif self.path == "/compress/start":
            url = data.get("url", "").strip()
            if not url:
                self._json(400, {"error": "missing 'url'"}); return

            job_id = str(uuid.uuid4())
            with JOBS_LOCK:
                JOBS[job_id] = {"status": "processing", "data": None, "error": None}

            t = threading.Thread(target=run_compress_job, args=(job_id, url), daemon=True)
            t.start()

            print(f"[compress/start] job {job_id} started for: {url[:80]}")
            self._json(200, {"job_id": job_id})

        else:
            self._json(404, {"error": "not found"})

    def _json(self, status: int, body: dict):
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
    print(f"[startup] Listening on port {PORT}  |  target: {TARGET_MB} MB")
    server.serve_forever()
