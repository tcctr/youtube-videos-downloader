#!/usr/bin/env python3
"""YouTube Videos Downloader: a small local web UI for yt-dlp. Run: python3 app.py (add --no-browser to skip opening a tab)."""
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import sys
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "settings.json"
YTDLP = shutil.which("yt-dlp") or "yt-dlp"
SYSTEM = platform.system()  # "Darwin", "Windows" or "Linux"

DEFAULT_SETTINGS = {
    "download_dir": str(Path.home() / "Downloads" / "yt-dlp"),
    "organise": True,        # Channel/Playlist/ subfolders
    "length_folders": False, # 30min/, 45min/ ... subfolders
    "skip_existing": True,   # remember finished downloads and skip them next time
    "subs": False,           # download and embed subtitles
    "sub_langs": "en.*",
    "embed": True,           # embed thumbnail, chapters and metadata
}

FORMATS = {
    "best": ["-f", "bv*+ba/b", "--merge-output-format", "mp4"],
    "2160": ["-f", "bv*[height<=2160]+ba/b[height<=2160]", "--merge-output-format", "mp4"],
    "1440": ["-f", "bv*[height<=1440]+ba/b[height<=1440]", "--merge-output-format", "mp4"],
    "1080": ["-f", "bv*[height<=1080]+ba/b[height<=1080]", "--merge-output-format", "mp4"],
    "720": ["-f", "bv*[height<=720]+ba/b[height<=720]", "--merge-output-format", "mp4"],
    "480": ["-f", "bv*[height<=480]+ba/b[height<=480]", "--merge-output-format", "mp4"],
    "mp3": ["-x", "--audio-format", "mp3", "--audio-quality", "0"],
    "m4a": ["-f", "ba[ext=m4a]/ba", "-x", "--audio-format", "m4a"],
}
AUDIO_FORMATS = {"mp3", "m4a"}

jobs = {}
procs = {}
jobs_lock = threading.Lock()
download_slots = threading.Semaphore(3)  # max simultaneous downloads


def load_settings():
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text())}
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


def save_settings(new):
    s = load_settings()
    s.update({k: v for k, v in new.items() if k in DEFAULT_SETTINGS})
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))
    return s


def get_info(url):
    out = subprocess.run(
        [YTDLP, "-J", "--flat-playlist", "--no-warnings", url],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "yt-dlp failed")
    data = json.loads(out.stdout)
    info = {
        "title": data.get("title"),
        "uploader": data.get("uploader") or data.get("channel"),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
        "view_count": data.get("view_count"),
        "upload_date": data.get("upload_date"),
        "description": (data.get("description") or "")[:600],
        "extractor": data.get("extractor_key"),
        "is_playlist": data.get("_type") == "playlist",
        "heights": sorted({f.get("height") for f in data.get("formats") or [] if f.get("height")}, reverse=True),
    }
    if info["is_playlist"]:
        info["entries"] = [
            {"title": e.get("title"), "duration": e.get("duration"),
             "url": e.get("url") or e.get("webpage_url"),
             "uploader": e.get("uploader") or e.get("channel"),
             "view_count": e.get("view_count"),
             "kind": "playlist" if e.get("ie_key") == "YoutubeTab" or "list=" in (e.get("url") or "") else "video",
             "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url")}
            for e in (data.get("entries") or [])
        ]
        if not info["thumbnail"] and info["entries"]:
            info["thumbnail"] = info["entries"][0]["thumbnail"]
    return info


def folder_name(text):
    """Make a playlist title safe to use as a folder inside a yt-dlp output template."""
    text = re.sub(r'[\\/:*?"<>|]+', "-", text).strip(" .") or "Untitled"
    return text.replace("%", "%%")


def length_bucket(seconds):
    """Round to the nearest 5 minutes: 28:40 -> 30min, 54:10 -> 55min."""
    return f"{max(5, 5 * round(seconds / 300))}min"


def probe_duration(path):
    """Read a file's length with ffprobe, for sites that don't report one."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0


def build_command(job, settings):
    base = Path(settings["download_dir"]).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    parts = []
    if settings["organise"]:
        parts.append("%(channel,uploader|Unknown channel)s")
        if job["group"]:
            parts.append(folder_name(job["group"]))
        elif job["kind"] == "playlist":
            parts.append("%(playlist_title|Playlist)s")
    parts.append("%(title)s [%(id)s].%(ext)s")

    cmd = [
        YTDLP, "--newline", "--no-warnings", "--progress",
        "--progress-template", "download:PROG %(progress._percent_str)s %(progress._speed_str)s %(progress._eta_str)s",
        "--print", "after_move:DONE\t%(filepath)s\t%(duration|0)s",
        "-o", str(base.joinpath(*parts)),
        *FORMATS.get(job["format"], FORMATS["best"]),
    ]
    if settings["skip_existing"]:
        cmd += ["--download-archive", str(base / ".downloaded.txt")]
    if settings["embed"]:
        cmd += ["--embed-metadata", "--embed-thumbnail", "--embed-chapters"]
    if settings["subs"] and job["format"] not in AUDIO_FORMATS:
        cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", settings["sub_langs"] or "en.*", "--embed-subs"]
    return cmd + [job["url"]]


def run_download(job_id):
    with download_slots:
        with jobs_lock:
            job = jobs.get(job_id)
            if not job or job["status"] == "cancelled":
                return
            job["status"] = "running"
        _download(job_id)


def _download(job_id):
    settings = load_settings()
    with jobs_lock:
        cmd = build_command(jobs[job_id], settings)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    with jobs_lock:
        procs[job_id] = proc
    last_lines, files = [], 0
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("DONE\t"):
            _, path, duration = (line.split("\t") + ["", ""])[:3]
            path = Path(path)
            try:
                secs = float(duration)
            except ValueError:
                secs = 0
            if settings["length_folders"] and not secs and path.exists():
                secs = probe_duration(path)
            if settings["length_folders"] and secs and path.exists():
                dest = path.parent / length_bucket(secs) / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), dest)
                path = dest
            files += 1
            with jobs_lock:
                jobs[job_id]["file"] = str(path)
                jobs[job_id]["files"] = files
            continue
        with jobs_lock:
            job = jobs[job_id]
            if line.startswith("PROG "):
                parts = line.split()
                job["percent"] = parts[1] if len(parts) > 1 else ""
                job["speed"] = parts[2] if len(parts) > 2 else ""
                job["eta"] = parts[3] if len(parts) > 3 else ""
            elif line.strip():
                last_lines = (last_lines + [line.strip()])[-5:]
    proc.wait()
    with jobs_lock:
        procs.pop(job_id, None)
        job = jobs[job_id]
        if job["status"] == "cancelled":
            return
        if proc.returncode != 0:
            job["status"] = "error"
            job["error"] = "\n".join(last_lines) or "yt-dlp failed"
        elif files == 0:
            job["status"] = "skipped"  # everything was already in the download archive
        else:
            job["status"] = "done"
            job["percent"] = "100%"


def start_job(url, title, fmt, kind="video", group=""):
    job_id = uuid.uuid4().hex[:8]
    with jobs_lock:
        jobs[job_id] = {"id": job_id, "url": url, "title": title or url, "format": fmt or "best",
                        "kind": kind or "video", "group": group or "", "status": "queued",
                        "percent": "0%", "speed": "", "eta": "", "file": "", "files": 0, "error": ""}
    threading.Thread(target=run_download, args=(job_id,), daemon=True).start()
    return job_id


def update_ytdlp():
    """Update yt-dlp the same way it was installed (Homebrew, pip or standalone binary)."""
    real = os.path.realpath(YTDLP)
    if "/Cellar/" in real or "/homebrew/" in real.lower():
        cmd = ["brew", "upgrade", "yt-dlp"]
    elif "site-packages" in real or Path(real).parent == Path(sys.executable).parent:
        cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]
    else:
        cmd = [YTDLP, "-U"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    text = (out.stdout + out.stderr).strip()
    if out.returncode != 0 and "already installed" not in text:
        raise RuntimeError(text.splitlines()[-1] if text else "Update failed")
    return ytdlp_version()


def open_folder(folder):
    if SYSTEM == "Darwin":
        subprocess.Popen(["open", str(folder)])
    elif SYSTEM == "Windows":
        os.startfile(folder)
    else:
        subprocess.Popen(["xdg-open", str(folder)])


def choose_folder():
    """Show the system folder picker. Returns the chosen path, or None if cancelled."""
    if SYSTEM == "Darwin":
        out = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose where downloads are saved")'],
            capture_output=True, text=True,
        )
        return out.stdout.strip().rstrip("/") if out.returncode == 0 else None
    # Windows / Linux: tkinter ships with most Python installs
    script = ("import tkinter as t, tkinter.filedialog as f; r = t.Tk(); r.withdraw(); "
              "r.attributes('-topmost', True); print(f.askdirectory(title='Choose where downloads are saved'))")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("No folder picker available. Edit download_dir in settings.json instead.")
    return out.stdout.strip() or None


def ytdlp_version():
    out = subprocess.run([YTDLP, "--version"], capture_output=True, text=True)
    return out.stdout.strip() or "unknown"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/jobs":
            with jobs_lock:
                self.send_json({"jobs": list(jobs.values())})
        elif self.path == "/api/settings":
            self.send_json({**load_settings(), "version": ytdlp_version()})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "bad json"}, 400)
        url = (payload.get("url") or "").strip()
        needs_url = self.path in ("/api/info", "/api/download")
        if needs_url and not url.startswith(("http://", "https://")):
            return self.send_json({"error": "Please paste a valid http(s) link"}, 400)

        if self.path == "/api/info":
            try:
                self.send_json(get_info(url))
            except Exception as e:
                self.send_json({"error": str(e)}, 400)
        elif self.path == "/api/download":
            job_id = start_job(url, payload.get("title"), payload.get("format"),
                               payload.get("kind"), payload.get("group"))
            self.send_json({"id": job_id})
        elif self.path == "/api/cancel":
            with jobs_lock:
                job = jobs.get(payload.get("id"))
                if job and job["status"] in ("queued", "running"):
                    job["status"] = "cancelled"
                    proc = procs.get(job["id"])
                    if proc:
                        proc.terminate()
            self.send_json({"ok": True})
        elif self.path == "/api/retry":
            with jobs_lock:
                old = jobs.pop(payload.get("id"), None)
            if not old:
                return self.send_json({"error": "No such download"}, 404)
            self.send_json({"id": start_job(old["url"], old["title"], old["format"], old["kind"], old["group"])})
        elif self.path == "/api/clear":
            with jobs_lock:
                for job_id in [j for j, job in jobs.items() if job["status"] not in ("queued", "running")]:
                    del jobs[job_id]
            self.send_json({"ok": True})
        elif self.path == "/api/settings":
            self.send_json(save_settings(payload))
        elif self.path == "/api/choose-folder":
            try:
                folder = choose_folder()
            except RuntimeError as e:
                return self.send_json({"error": str(e)}, 400)
            if not folder:
                return self.send_json({"cancelled": True})
            self.send_json(save_settings({"download_dir": folder}))
        elif self.path == "/api/update":
            try:
                self.send_json({"version": update_ytdlp()})
            except Exception as e:
                self.send_json({"error": str(e)}, 400)
        elif self.path == "/api/open-folder":
            folder = Path(load_settings()["download_dir"]).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            open_folder(folder)
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    for tool in ("yt-dlp", "ffmpeg"):
        if not shutil.which(tool):
            print(f"Warning: {tool} was not found on your PATH. See README.md for install steps.")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"YouTube Videos Downloader running at {url}  (downloads -> {load_settings()['download_dir']})")
    print("Press Ctrl+C to stop.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
