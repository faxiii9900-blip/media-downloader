import os
import glob
import tempfile
import uuid
import re
import time
import zipfile
import threading
import subprocess
from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import shutil

app = Flask(__name__)

# Detect system ffmpeg (in cloud containers / local) or imageio fallback
FFMPEG_EXE = shutil.which("ffmpeg")
if not FFMPEG_EXE:
    try:
        import imageio_ffmpeg
        FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG_EXE = "ffmpeg"

CACHE_DIR = os.path.join(tempfile.gettempdir(), "video_downloader_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Delete temporary files after 15 minutes (900 seconds)
EXPIRATION_SECONDS = 15 * 60

def cleanup_worker():
    while True:
        try:
            now = time.time()
            for filepath in glob.glob(os.path.join(CACHE_DIR, "*")):
                try:
                    if os.path.isfile(filepath):
                        if (now - os.path.getmtime(filepath)) > EXPIRATION_SECONDS:
                            os.remove(filepath)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(60)

threading.Thread(target=cleanup_worker, daemon=True).start()

def transcode_to_quicktime(raw_path: str, final_path: str):
    cmd = [
        FFMPEG_EXE,
        "-y",
        "-i", raw_path,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "18",
        "-preset", "faster",
        "-c:a", "aac",
        "-b:a", "320k",
        "-movflags", "+faststart",
        final_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg transcode error: {res.stderr}")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/download", methods=["POST"])
def download_media():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    mode = data.get("mode", "video") # "video" or "image"

    if not url:
        return jsonify({"error": "Please provide a valid URL."}), 400

    job_id = str(uuid.uuid4())[:8]

    if mode == "image":
        # Image download mode (thumbnails, instagram photos/carousels)
        img_template = os.path.join(CACHE_DIR, f"{job_id}_img_%(autonumber)02d.%(ext)s")
        ydl_opts = {
            "skip_download": True,
            "writethumbnail": True,
            "outtmpl": img_template,
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "images")

            img_files = glob.glob(os.path.join(CACHE_DIR, f"{job_id}_img_*"))
            if not img_files:
                return jsonify({"error": "No images found for this link."}), 404

            clean_title = re.sub(r'[^\w\s-]', '', title).strip() or f"images_{job_id}"

            if len(img_files) == 1:
                single_file = img_files[0]
                ext = os.path.splitext(single_file)[1] or ".jpg"
                return send_file(
                    single_file,
                    as_attachment=True,
                    download_name=f"{clean_title}{ext}"
                )
            else:
                zip_path = os.path.join(CACHE_DIR, f"{job_id}_{clean_title}.zip")
                with zipfile.ZipFile(zip_path, 'w') as zipf:
                    for idx, img in enumerate(img_files, start=1):
                        ext = os.path.splitext(img)[1] or ".jpg"
                        zipf.write(img, arcname=f"{clean_title}_{idx:02d}{ext}")
                return send_file(
                    zip_path,
                    as_attachment=True,
                    download_name=f"{clean_title}.zip",
                    mimetype="application/zip"
                )
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    else:
        # High-res video mode (QuickTime compatible)
        raw_template = os.path.join(CACHE_DIR, f"{job_id}_raw.%(ext)s")
        final_output_path = os.path.join(CACHE_DIR, f"{job_id}_final.mp4")

        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": raw_template,
            "ffmpeg_location": FFMPEG_EXE,
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                video_title = info.get("title", "video")

            raw_candidates = glob.glob(os.path.join(CACHE_DIR, f"{job_id}_raw.*"))
            if not raw_candidates:
                return jsonify({"error": "Failed to download media stream."}), 500

            raw_path = raw_candidates[0]
            transcode_to_quicktime(raw_path, final_output_path)

            try:
                if os.path.exists(raw_path):
                    os.remove(raw_path)
            except Exception:
                pass

            clean_title = re.sub(r'[^\w\s-]', '', video_title).strip() or f"video_{job_id}"
            return send_file(
                final_output_path,
                as_attachment=True,
                download_name=f"{clean_title}.mp4",
                mimetype="video/mp4"
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
