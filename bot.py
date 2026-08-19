import asyncio
import os
import re
import json
import base64
import glob
import io
import tempfile
import subprocess
import time
from pathlib import Path
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
import anthropic
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TF_NOTION_TOKEN = os.environ["TF_NOTION_TOKEN"]
TF_PAGE_ID = os.environ["TF_PAGE_ID"]
TV_NOTION_TOKEN = os.environ["TV_NOTION_TOKEN"]
TV_PAGE_ID = os.environ["TV_PAGE_ID"]
TB_NOTION_TOKEN = os.environ.get("TB_NOTION_TOKEN", os.environ.get("TV_NOTION_TOKEN", ""))
TB_PAGE_ID = os.environ.get("TB_PAGE_ID", "33cebaa28b9780c8b483f36fcaa542bf")

BASE_DIR = Path(__file__).parent
BRIEF_PROMPT_PATH = BASE_DIR / "BRIEF_PROMPT.md"
CLIENT_PATH = BASE_DIR / "clients" / "deepstash.md"


def extract_urls_and_tag(text):
    urls = re.findall(r'https?://\S+', text)
    tag = None
    if re.search(r'\bTF\b', text):
        tag = 'TF'
    elif re.search(r'\bTV\b', text):
        tag = 'TV'
    elif re.search(r'\bTB\b', text):
        tag = 'TB'
    return urls, tag


def extract_handle_from_url(url: str) -> str:
    parts = re.sub(r'\?.*$', '', url).strip('/').split('/')
    # TikTok: /@username/video/...
    for p in parts:
        if p.startswith('@'):
            return p
    # Instagram: domain/username/reel/... or domain/reel/...
    skip = {'www.instagram.com', 'instagram.com', 'reel', 'p', 'tv', 'stories',
            'www.tiktok.com', 'tiktok.com', 'video', ''}
    for p in parts:
        if p not in skip and not p.startswith('http'):
            return f"@{p}"
    return "@unknown"


def get_proxy() -> str:
    """Return proxy URL from env, e.g. 'http://user:pass@host:port' or 'socks5://host:port'."""
    return os.environ.get("PROXY_URL", "")


def get_tiktok_cookies_file() -> str:
    cookies = os.environ.get("TIKTOK_COOKIES", "")
    if not cookies:
        return None
    path = os.path.join(tempfile.gettempdir(), "tiktok_cookies.txt")
    # Accept Netscape format directly
    if "# Netscape HTTP Cookie File" in cookies or "\t" in cookies:
        with open(path, 'w') as f:
            f.write(cookies if cookies.startswith("# Netscape") else "# Netscape HTTP Cookie File\n" + cookies)
        return path
    # Legacy: parse name=value; name2=value2
    from urllib.parse import unquote
    with open(path, 'w') as f:
        f.write("# Netscape HTTP Cookie File\n")
        for item in cookies.split(';'):
            item = item.strip()
            if '=' in item:
                name, value = item.split('=', 1)
                f.write(f".tiktok.com\tTRUE\t/\tTRUE\t2999999999\t{name.strip()}\t{unquote(value.strip())}\n")
    return path


def get_cookies_file() -> str:
    """Write Instagram cookies to a fixed path file (avoids NamedTemporaryFile threading issues)."""
    from urllib.parse import unquote
    cookies = os.environ.get("INSTAGRAM_COOKIES", "")
    if not cookies:
        return None
    path = os.path.join(tempfile.gettempdir(), "ig_cookies.txt")
    cookie_pairs = {}
    for item in cookies.split(';'):
        item = item.strip()
        if '=' in item:
            name, value = item.split('=', 1)
            cookie_pairs[name.strip()] = unquote(value.strip())
    with open(path, 'w') as f:
        f.write("# Netscape HTTP Cookie File\n")
        for name, value in cookie_pairs.items():
            f.write(f".instagram.com\tTRUE\t/\tTRUE\t2999999999\t{name}\t{value}\n")
    return path


def download_via_embed(url: str, output_path: str) -> bool:
    """Get video URL from Instagram public embed page — no auth needed."""
    try:
        shortcode_match = re.search(r'/(reel|p)/([A-Za-z0-9_-]+)', url)
        if not shortcode_match:
            return False
        shortcode = shortcode_match.group(2)

        proxy = get_proxy()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        video_url = None
        for embed_path in [f"/p/{shortcode}/embed/captioned/", f"/p/{shortcode}/embed/"]:
            try:
                resp = requests.get(
                    f"https://www.instagram.com{embed_path}",
                    timeout=30, headers=headers, proxies=proxies
                )
                print(f"[Embed] {embed_path} status={resp.status_code} len={len(resp.text)}")
                if resp.status_code != 200:
                    continue
                html = resp.text
                patterns = [
                    r'"video_url":"(https?:[^"]+)"',
                    r'"video_url":\s*"(https?:[^"]+)"',
                    r'video_url\\u003D(https?[^\s\\"&]+)',
                    r'"contentUrl"\s*:\s*"(https?:[^"]+)"',
                    r'<meta[^>]+property=["\']og:video:secure_url["\'][^>]+content=["\']([^"\']+)["\']',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video:secure_url["\']',
                    r'<video[^>]+src=["\']([^"\']+)["\']',
                ]
                for pat in patterns:
                    m = re.search(pat, html)
                    if m:
                        video_url = m.group(1).replace('\\/', '/').replace('\\u0026', '&').replace('&amp;', '&')
                        print(f"[Embed] video_url found via pattern {pat[:60]}")
                        break
                if video_url:
                    break
            except Exception as e:
                print(f"[Embed] error fetching {embed_path}: {e}")

        if not video_url:
            print(f"[Embed] no video_url found for {shortcode}")
            return False

        video_resp = requests.get(
            video_url, stream=True, timeout=120,
            headers={"User-Agent": headers["User-Agent"]},
            proxies=proxies
        )
        print(f"[Embed] video download status={video_resp.status_code}")
        if video_resp.status_code != 200:
            return False

        raw_path = output_path.replace(".wav", ".mp4")
        with open(raw_path, "wb") as f:
            for chunk in video_resp.iter_content(chunk_size=8192):
                f.write(chunk)

        subprocess.run(
            ["ffmpeg", "-i", raw_path, "-vn", "-ar", "16000", "-ac", "1", output_path, "-y"],
            capture_output=True, text=True
        )
        result = os.path.exists(output_path)
        print(f"[Embed] output exists={result}")
        return result
    except Exception as e:
        print(f"[Embed] exception: {e}")
        return False


def download_via_instaloader(url: str, output_path: str) -> bool:
    """Download Instagram video using instaloader with saved session."""
    try:
        import instaloader, base64, tempfile as _tempfile
        shortcode_match = re.search(r'/(reel|p)/([A-Za-z0-9_-]+)', url)
        if not shortcode_match:
            return False
        shortcode = shortcode_match.group(2)

        L = instaloader.Instaloader(download_video_thumbnails=False, save_metadata=False, compress_json=False)

        session_b64 = os.environ.get("INSTAGRAM_SESSION", "")
        if session_b64:
            session_data = base64.b64decode(session_b64)
            session_file = _tempfile.mktemp(suffix=".session")
            with open(session_file, "wb") as f:
                f.write(session_data)
            try:
                L.load_session_from_file("3292491077", session_file)
            except Exception:
                L.load_session_from_file("", session_file)

        post = instaloader.Post.from_shortcode(L.context, shortcode)
        video_url = post.video_url
        if not video_url:
            return False

        resp = requests.get(video_url, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
        raw_path = output_path.replace(".wav", ".mp4")
        with open(raw_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        ffmpeg_result = subprocess.run([
            "ffmpeg", "-i", raw_path, "-vn", "-ar", "16000", "-ac", "1", output_path, "-y"
        ], capture_output=True, text=True)
        return os.path.exists(output_path)
    except Exception as e:
        print(f"[Instaloader] exception: {e}")
        raise RuntimeError(f"Instaloader failed: {e}")


def download_via_cobalt(url: str, output_path: str) -> bool:
    """Try to download audio via Cobalt API. Returns True if successful."""
    try:
        # Clean Instagram URL — remove tracking params
        clean_url = re.sub(r'\?.*$', '', url.rstrip('/'))
        resp = requests.post(
            "https://api.cobalt.tools/",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0"
            },
            json={"url": clean_url, "downloadMode": "audio"},
            timeout=30
        )
        print(f"[Cobalt] status={resp.status_code} body={resp.text[:300]}")
        if resp.status_code != 200:
            raise RuntimeError(f"Cobalt HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        download_url = data.get("url")
        if not download_url:
            raise RuntimeError(f"Cobalt no URL: {data}")
        video_resp = requests.get(download_url, timeout=120, stream=True)
        if video_resp.status_code != 200:
            return False
        raw_path = output_path.replace(".wav", ".audio_raw")
        with open(raw_path, "wb") as f:
            for chunk in video_resp.iter_content(chunk_size=8192):
                f.write(chunk)
        ffmpeg_result = subprocess.run([
            "ffmpeg", "-i", raw_path, "-ar", "16000", "-ac", "1", output_path, "-y"
        ], capture_output=True, text=True)
        return os.path.exists(output_path)
    except Exception as e:
        print(f"[Cobalt] exception: {e}")
        raise RuntimeError(f"Cobalt exception: {e}")


def get_fathom_transcript(url: str) -> str:
    """Try to extract transcript text directly from a Fathom share page."""
    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        if resp.status_code != 200:
            return ""
        html = resp.text
        # Fathom embeds transcript in JSON inside a <script> tag
        for pattern in [
            r'"transcript"\s*:\s*"((?:[^"\\]|\\.)+)"',
            r'"body"\s*:\s*"((?:[^"\\]|\\.)+)"',
            r'"text"\s*:\s*"((?:[^"\\]|\\.)+)"',
        ]:
            m = re.search(pattern, html)
            if m:
                text = m.group(1).encode('utf-8').decode('unicode_escape')
                if len(text) > 100:
                    print(f"[Fathom] transcript extracted, len={len(text)}")
                    return text
        # Fallback: look for transcript segments as array
        segments = re.findall(r'"(?:content|text)"\s*:\s*"([^"]{20,})"', html)
        if segments:
            text = " ".join(segments)
            print(f"[Fathom] transcript from segments, len={len(text)}")
            return text
        print("[Fathom] no transcript found in page")
        return ""
    except Exception as e:
        print(f"[Fathom] exception scraping transcript: {e}")
        return ""


def download_audio(url: str, output_path: str) -> str:
    """Download and return path to extracted wav file."""
    import glob as _glob
    import yt_dlp
    tmpdir = os.path.dirname(output_path)

    # Try multiple methods for Instagram
    if "instagram.com" in url:
        if download_via_embed(url, output_path):
            return output_path
        try:
            if download_via_instaloader(url, output_path):
                return output_path
            print("[Instaloader] returned False, trying yt-dlp")
        except Exception as e:
            print(f"[Instaloader] failed, trying yt-dlp: {e}")

    cookies_file = None
    ig_username = os.environ.get("INSTAGRAM_USERNAME", "")
    ig_password = os.environ.get("INSTAGRAM_PASSWORD", "")
    if "instagram.com" in url:
        cookies_file = get_cookies_file()
    elif "tiktok.com" in url:
        cookies_file = get_tiktok_cookies_file()

    proxy = get_proxy() if "instagram.com" in url else ""
    if "instagram.com" in url and not proxy:
        print("[yt-dlp] WARNING: No PROXY_URL set. Instagram downloads from server IPs are usually blocked. Set PROXY_URL env var to fix this.")

    # TikTok: yt-dlp selects DASH streams and uses ffmpeg to merge them.
    # Railway's ffmpeg fails → resulting file has video only, no audio.
    # Fix: force non-DASH (progressive) formats which are pre-muxed with audio.
    if "tiktok.com" in url:
        audio_base = os.path.join(tmpdir, "tiktok")
        # Progressive mp4 from TikTok's playAddr — video+audio in one file, no ffmpeg merge needed
        progressive_formats = [
            "best[protocol!=http_dash_segments][ext=mp4]",
            "best[protocol!=http_dash_segments]",
            "worst[protocol!=http_dash_segments][ext=mp4]",  # lower quality but no DASH
            "best",  # last resort
        ]
        for fmt in progressive_formats:
            # Clean up files from previous attempt
            for f in _glob.glob(audio_base + ".*"):
                try: os.remove(f)
                except: pass

            dl_opts = {
                "outtmpl": audio_base + ".%(ext)s",
                "noplaylist": True,
                "nopart": True,
                "format": fmt,
                "quiet": True,
                "concurrent_fragment_downloads": 1,  # yt-dlp's fragment thread pool deadlocks (Errno 11) on this setup
            }
            if cookies_file:
                dl_opts["cookiefile"] = cookies_file
            try:
                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                print(f"[TikTok] fmt={fmt} failed: {e}")
                continue

            candidates = sorted(
                [f for f in _glob.glob(audio_base + ".*") if os.path.getsize(f) > 1000],
                key=os.path.getsize, reverse=True
            )
            if not candidates:
                print(f"[TikTok] fmt={fmt}: no file")
                continue

            src = candidates[0]
            print(f"[TikTok] fmt={fmt} → {src} ({os.path.getsize(src)} bytes)")

            # Check this file actually has an audio stream before trying Groq
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", src],
                capture_output=True, text=True
            )
            has_audio = bool(probe.stdout.strip())
            print(f"[TikTok] has_audio={has_audio} (probe: {probe.stdout.strip()!r})")

            if not has_audio:
                print(f"[TikTok] fmt={fmt}: no audio stream, skipping")
                continue

            # Try wav (for local whisper fallback)
            subprocess.run(
                ["ffmpeg", "-i", src, "-vn", "-ar", "16000", "-ac", "1",
                 "-acodec", "pcm_s16le", output_path, "-y"],
                capture_output=True, text=True
            )
            if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                return output_path

            # Return raw file — Groq accepts mp4/m4a/webm with audio track
            print(f"[TikTok] wav failed, returning raw: {src}")
            return src

        raise RuntimeError("TikTok: no format with audio track found")

    last_error = None
    for attempt in range(3):
        if attempt > 0:
            time.sleep(20 * attempt)

        # Clean up previous attempt files
        for f in _glob.glob(os.path.join(tmpdir, "dl.*")):
            try:
                os.remove(f)
            except Exception:
                pass

        dl_template = os.path.join(tmpdir, "dl.%(ext)s")
        ydl_opts = {
            "outtmpl": dl_template,
            "noplaylist": True,
            "nopart": True,
            "format": "bestaudio[ext=m4a]+bestvideo[ext=mp4]/best[ext=mp4]/best/bestaudio/best",
            "merge_output_format": "mp4",
            "sleep_requests": 3,
            "quiet": True,
            "concurrent_fragment_downloads": 1,  # yt-dlp's fragment thread pool deadlocks (Errno 11) on this setup
        }
        if proxy:
            ydl_opts["proxy"] = proxy
        if ig_username and ig_password:
            ydl_opts["username"] = ig_username
            ydl_opts["password"] = ig_password
        elif cookies_file:
            ydl_opts["cookiefile"] = cookies_file

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            last_error = str(e)
            if proxy and any(code in last_error for code in ("407", "502", "503", "Unable to connect to proxy")):
                print("[yt-dlp] Proxy 407, retrying without proxy...")
                ydl_opts.pop("proxy", None)
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])
                except Exception as e2:
                    last_error = str(e2)
                    continue
            else:
                continue

        files = _glob.glob(os.path.join(tmpdir, "dl.*"))
        if not files:
            last_error = "File not found after yt-dlp"
            continue
        dl_file = files[0]

        for ffmpeg_args in [
            ["-map", "0:a:0", "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le"],
            ["-vn", "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le"],
        ]:
            ffmpeg_result = subprocess.run(
                ["ffmpeg", "-i", dl_file] + ffmpeg_args + [output_path, "-y"],
                capture_output=True, text=True
            )
            if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                return output_path
        last_error = f"ffmpeg failed: {ffmpeg_result.stderr[-400:]}"

    raise RuntimeError(f"yt-dlp error: {last_error}")


# ── Reference Images ───────────────────────────────────────────────────────────
# Detects photos/screenshots the creator pastes into the video as overlays
# (e.g. a historical photo shown while talking) — not the creator's own
# camera framing. Any failure here is swallowed so it never blocks the
# transcript/brief/publish flow.

def find_downloaded_video(tmpdir: str) -> str | None:
    """download_audio() leaves the source video behind in tmpdir before
    extracting audio — naming varies by which download path succeeded
    (dl.*, tiktok.*, audio.mp4, audio.audio_raw, ...), so rather than
    enumerate every convention, just take the largest non-wav, non-partial
    file in the dir — that's reliably the video, never the extracted audio."""
    candidates = [
        f for f in glob.glob(os.path.join(tmpdir, "*"))
        if os.path.isfile(f) and not f.endswith((".wav", ".part", ".json"))
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getsize)


def extract_scene_frames(video_path: str, out_dir: str, max_frames: int = 16) -> list[tuple[str, float]]:
    """Pulls one frame per scene change (where an inserted image is most
    likely to appear/disappear) plus the very first frame."""
    pattern = os.path.join(out_dir, "frame_%03d.jpg")
    result = subprocess.run(
        ["ffmpeg", "-i", video_path, "-vf",
         "select='eq(n,0)+gt(scene,0.25)',showinfo", "-vsync", "vfr", pattern, "-y"],
        capture_output=True, text=True
    )
    timestamps = [float(m) for m in re.findall(r"pts_time:([\d.]+)", result.stderr)]
    frames = sorted(glob.glob(os.path.join(out_dir, "frame_*.jpg")))
    paired = list(zip(frames, timestamps)) if len(timestamps) == len(frames) else [(f, 0.0) for f in frames]

    if len(paired) > max_frames:
        step = len(paired) / max_frames
        paired = [paired[int(i * step)] for i in range(max_frames)]

    return paired


def detect_overlays(frames: list[tuple[str, float]]) -> tuple[list[dict], list[dict]]:
    """Single vision pass per frame batch — detects both (a) inserted reference
    images/photos overlaid on the video and (b) on-screen text title/header
    cards, in one call instead of two (halves vision-token cost, since each
    frame's image bytes would otherwise be sent twice). Uses Sonnet — Haiku
    was tried here first but hallucinated garbled on-screen text for the
    header pass, so this specific vision read needs the stronger model.
    Returns (image_detections, header_detections)."""
    if not frames:
        return [], []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    image_detections = []
    header_detections = []

    for batch_start in range(0, len(frames), 6):
        batch = frames[batch_start:batch_start + 6]
        content = []
        for i, (path, ts) in enumerate(batch):
            with open(path, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode()
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
            content.append({"type": "text", "text": f"Frame {i} (t={ts:.1f}s)"})
        content.append({"type": "text", "text": (
            "For each numbered frame above, look for TWO separate kinds of overlay — a frame "
            "can have one, both, or neither:\n\n"
            "1) INSERTED REFERENCE IMAGE — a photo, meme, screenshot, or graphic pasted into "
            "the video that is NOT the creator's own camera view. Any size/position/aspect "
            "ratio — a small corner box, a strip with one or two stills, a large centered "
            "image, or a full-frame background collage. Many stills are wide/landscape — trace "
            "their own actual edges, don't assume they match the vertical frame. Classify its "
            "\"type\":\n"
            "   - \"clean\": a self-contained rectangle that can be cropped out on its own "
            "without cutting into anything else.\n"
            "   - \"collage\": a full-frame background graphic the creator's own body/face is "
            "composited ON TOP OF — cropping would always cut into the creator, so it can't be "
            "extracted cleanly. If the creator overlaps the graphic, it's \"collage\" no matter "
            "how big the graphic is.\n"
            "   \"box\" is the TIGHT bounding box around ONLY the inserted image's actual pixel "
            "content (approximate is fine for \"collage\" — it won't be used for cropping) — err "
            "slightly INSIDE the true edge, excluding any decorative card border, letterbox "
            "bars, or sliver of the creator. Coordinates are fractions of the frame's "
            "width/height (0.0 = left/top edge, 1.0 = right/bottom edge). If two stills sit "
            "side by side, one box should cover both, still excluding any shared border.\n\n"
            "2) ON-SCREEN TEXT TITLE/HEADER — big bold text graphics burned into the video (a "
            "title card, numbered-list intro, section header), as opposed to small captions, "
            "UI chrome, or auto-generated word-by-word subtitles.\n\n"
            "Reply with ONLY a JSON object with two arrays (each entry only for a frame that "
            "actually has that kind of overlay — omit frames with neither):\n"
            '{"images": [{"frame": 0, "type": "clean", "box": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 0.3}, '
            '"description": "short description, detailed enough to search for online if needed"}], '
            '"headers": [{"frame": 0, "text": "exact text shown, as written, including any subtitle line under it"}]}\n'
            "If a category is empty across all frames, use an empty array for it."
        )})

        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                messages=[{"role": "user", "content": content}]
            )
            text = next((b.text for b in message.content if b.type == "text"), "")
            match = re.search(r"\{.*\}", text, re.DOTALL)
            parsed = json.loads(match.group(0)) if match else {}
        except Exception as e:
            print(f"  [overlay-detection error] batch {batch_start}: {e}")
            parsed = {}

        for d in parsed.get("images", []):
            idx = d.get("frame")
            if idx is None or not (0 <= idx < len(batch)):
                continue
            path, ts = batch[idx]
            box = d.get("box") or {}
            image_detections.append({
                "path": path, "timestamp": ts,
                "type": d.get("type") if d.get("type") in ("clean", "collage") else "clean",
                "box": (
                    float(box.get("x0", 0.0)), float(box.get("y0", 0.0)),
                    float(box.get("x1", 1.0)), float(box.get("y1", 1.0)),
                ),
                "description": d.get("description", "")
            })

        for d in parsed.get("headers", []):
            idx = d.get("frame")
            if idx is None or not (0 <= idx < len(batch)):
                continue
            _, ts = batch[idx]
            header_detections.append({"timestamp": ts, "text": d.get("text", "")})

    return image_detections, header_detections


def crop_region(image_path: str, box: tuple[float, float, float, float]) -> bytes:
    """box is (x0, y0, x1, y1) as fractions of the frame — cropped exactly, no
    added margin, so nothing outside the inserted image (the creator's own
    camera view, a card border, etc.) bleeds into the crop."""
    from PIL import Image
    img = Image.open(image_path)
    w, h = img.size
    x0, y0, x1, y1 = box
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        x0, y0, x1, y1 = 0.0, 0.0, 1.0, 1.0  # malformed box from the model — fall back to full frame
    # Shrink inward — the model's box tends to run a bit loose, leaving slivers of
    # the creator's own camera view or the video's own edges in the crop. Erring
    # tighter (losing a thin margin of the actual overlay) beats bleed-through.
    # The bottom edge is where the creator's hair/head consistently bleeds in
    # (they're usually framed low in the shot), so it gets cropped harder than
    # the other three sides.
    width, height = x1 - x0, y1 - y0
    erode_side, erode_bottom = width * 0.08, height * 0.20
    ex0, ey0, ex1 = x0 + erode_side, y0 + height * 0.08, x1 - erode_side
    ey1 = y1 - erode_bottom
    if ex1 > ex0 and ey1 > ey0:  # box too small to erode without inverting — use it as-is
        x0, y0, x1, y1 = ex0, ey0, ex1, ey1
    cropped = img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    buf = io.BytesIO()
    cropped.convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def dedupe_detections(detections: list[dict]) -> list[dict]:
    """Collapses repeated detections of the same overlay lingering across
    several consecutive scene cuts into a single representative crop."""
    kept = []
    for d in sorted(detections, key=lambda x: x["timestamp"]):
        dup = next((k for k in kept
                    if abs(k["timestamp"] - d["timestamp"]) < 5
                    and k["description"][:25].lower() == d["description"][:25].lower()), None)
        if not dup:
            kept.append(d)
    return kept


def context_at_timestamp(segments: list[dict], ts: float) -> str:
    """Original-transcript text being spoken at (or nearest to) a given video timestamp."""
    if not segments:
        return ""
    for seg in segments:
        if seg["start"] <= ts <= seg["end"]:
            return seg["text"]
    return min(segments, key=lambda s: min(abs(s["start"] - ts), abs(s["end"] - ts)))["text"]


GENERAL_ELEMENTS_DRIVE_URL = "https://drive.google.com/drive/folders/1T_ssCuSnjQvnSiTzYM1zXnijuo1w5rc6?usp=sharing"


def dedupe_headers(headers: list[dict]) -> list[dict]:
    """Collapses the same title card lingering across several consecutive
    scene cuts into a single representative detection."""
    kept = []
    for h in sorted(headers, key=lambda x: x["timestamp"]):
        dup = next((k for k in kept
                    if abs(k["timestamp"] - h["timestamp"]) < 5
                    and k["text"][:25].lower() == h["text"][:25].lower()), None)
        if not dup:
            kept.append(h)
    return kept


def extract_overlays(video_path: str | None, segments: list[dict] = None) -> tuple[list[dict], list[dict], bool]:
    """Full pipeline: scene-detect candidate frames ONCE, run the combined
    image+header vision pass ONCE, then resolve each detection. "clean"
    overlays (a self-contained rectangle) get cropped directly. "collage"
    overlays (a background graphic the creator's own body is composited over,
    so no crop is ever clean) aren't matched to anything specific — they just
    flip has_collage_background, which gets the creator pointed at the shared
    General Elements Drive instead. Returns
    (reference_images, text_headers, has_collage_background):
      reference_images: [{"type": "clean", "bytes":, "caption":, "timestamp":, "original_context":}, ...]
      text_headers: [{"text":, "timestamp":, "original_context":}, ...]
    Empty/False on any failure (including no video_path) — never blocks the
    main brief/publish flow."""
    if not video_path:
        return [], [], False
    try:
        with tempfile.TemporaryDirectory() as frames_dir:
            frames = extract_scene_frames(video_path, frames_dir)
            image_dets, header_dets = detect_overlays(frames)
            image_dets = dedupe_detections(image_dets)
            header_dets = dedupe_headers(header_dets)

            has_collage_background = any(d["type"] == "collage" for d in image_dets)

            reference_images = [
                {
                    "type": "clean",
                    "bytes": crop_region(d["path"], d["box"]),
                    "caption": d["description"],
                    "timestamp": d["timestamp"],
                    "original_context": context_at_timestamp(segments or [], d["timestamp"]),
                }
                for d in image_dets if d["type"] == "clean"
            ]

            text_headers = [
                {
                    "text": h["text"],
                    "timestamp": h["timestamp"],
                    "original_context": context_at_timestamp(segments or [], h["timestamp"]),
                }
                for h in header_dets
            ]
            return reference_images, text_headers, has_collage_background
    except Exception as e:
        print(f"  [overlays] pipeline failed: {e}")
        return [], [], False


def transcribe_audio(audio_path: str) -> dict:
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        ext = os.path.splitext(audio_path)[1].lower()
        mime = {
            ".mp3": "audio/mpeg", ".mp4": "video/mp4", ".m4a": "audio/m4a",
            ".webm": "audio/webm", ".ogg": "audio/ogg", ".flac": "audio/flac",
            ".wav": "audio/wav", ".mpeg": "audio/mpeg",
        }.get(ext, "audio/wav")
        fsize = os.path.getsize(audio_path)
        print(f"[Groq] file={audio_path} ext={ext} mime={mime} size={fsize}")
        # Groq limit is 25MB; if over, try to re-encode to mp3
        if fsize > 24 * 1024 * 1024:
            mp3_path = audio_path.rsplit(".", 1)[0] + "_small.mp3"
            r = subprocess.run(
                ["ffmpeg", "-i", audio_path, "-vn", "-acodec", "libmp3lame", "-q:a", "7",
                 "-ar", "16000", "-ac", "1", mp3_path, "-y"],
                capture_output=True, text=True
            )
            if os.path.exists(mp3_path) and os.path.getsize(mp3_path) < 24 * 1024 * 1024:
                audio_path, ext, mime = mp3_path, ".mp3", "audio/mpeg"
                print(f"[Groq] re-encoded to mp3: {os.path.getsize(mp3_path)} bytes")
            else:
                raise RuntimeError(f"File too large for Groq ({fsize} bytes) and re-encode failed")
        with open(audio_path, "rb") as f:
            resp = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {groq_key}"},
                files={"file": (os.path.basename(audio_path), f, mime)},
                data={"model": "whisper-large-v3", "language": "en", "response_format": "verbose_json"},
                timeout=120,
            )
        if not resp.ok:
            raise RuntimeError(f"Groq {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        segs = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in data.get("segments", [])]
        return {"content": data["text"].strip(), "lang": "en", "segments": segs}

    # Fallback: local Whisper base (fits in Railway free tier memory)
    from faster_whisper import WhisperModel
    model = WhisperModel("base", device="cpu", compute_type="int8")
    raw_segments, info = model.transcribe(
        audio_path,
        language="en",
        beam_size=5,
        temperature=0,
        condition_on_previous_text=False,
        vad_filter=True,
        no_speech_threshold=0.6,
    )
    segs = []
    texts = []
    for seg in raw_segments:
        texts.append(seg.text)
        segs.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
    return {"content": " ".join(texts).strip(), "lang": info.language, "segments": segs}


DEEPSTASH_INTEGRATION_RULES = """
## DEEPSTASH INTEGRATION RULES (TV scripts only)

The Adapted Script MUST include a natural Deepstash integration. Don't reach for a
stock phrase — actually read this specific script's content, topic, and voice first,
then write the integration that fits THIS video, not a generic one that could be
pasted into any script. Stay inside these constraints while you do:

**WHEN:** In the last 20% of the script, after the problem is fully established —
once the video has committed to a specific struggle/behavior, not before.

**HOW TO INTRODUCE IT:** Write your own transition line into it, in this script's own
voice and rhythm — something that would sound like a natural continuation of what was
just said, not a bolted-on plug. It should read as the creator sharing what actually
worked for them, in their own words for this topic.

**WHAT TO SAY ABOUT THE APP:** Never name it. Describe what it actually does
functionally, in language that fits this script's specific hook/problem — e.g. if the
video is about doomscrolling, tie the description to that; if it's about information
overload, tie it to that. The through-line to preserve: it delivers bite-sized ideas
from real books, in a scrolling-feed format, so it captures the same habit/urge the
video is about but redirects it toward something worth absorbing.

**CONNECTION:** Always frame it as a direct REPLACEMENT of the specific bad
behavior/urge this script is about, not a generic addition — pick the replacement
framing that actually matches what this video already established as the problem.

**CTA:** End with a line that leads into: "Comment the word BOOK and I'll send you the
method." The CTA text itself must stay exactly that (it's a working comment-automation
trigger) — but the sentence(s) leading into it can be written to fit the script.

**TONE:** Never slow down or change energy for the app moment. Same energy as the rest
of this specific script — match its actual register, don't default to "raw and urgent"
if the script itself is calm, technical, or something else.
"""


def generate_brief(transcript: str, video_url: str, tag: str = "") -> str:
    brief_prompt = BRIEF_PROMPT_PATH.read_text()
    client_context = CLIENT_PATH.read_text()

    if tag == "TV":
        brief_prompt = brief_prompt + "\n\n" + DEEPSTASH_INTEGRATION_RULES

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    for attempt in range(3):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=brief_prompt,
                messages=[{
                    "role": "user",
                    "content": f"""## CLIENT CONTEXT
{client_context}

## REFERENCE VIDEO URL
{video_url}

## LANGUAGE
English (EN) — MANDATORY: Write the ENTIRE brief in English.

## TRANSCRIPT
{transcript}

---
Generate the content brief following the instructions exactly. Output only the brief in markdown, nothing else."""
                }]
            )
            return message.content[0].text
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < 2:
                time.sleep(30 * (attempt + 1))
                continue
            raise


def rich_text(content: str, url: str = None):
    obj = {"type": "text", "text": {"content": content}}
    if url:
        obj["text"]["link"] = {"url": url}
    return obj


def parse_rich_text(line: str):
    parts = []
    remaining = re.sub(r'\*\*(.*?)\*\*', lambda m: m.group(0), line)
    pattern = r'\*\*(.*?)\*\*'
    last = 0
    for m in re.finditer(pattern, line):
        if m.start() > last:
            parts.append(rich_text(line[last:m.start()]))
        parts.append({"type": "text", "text": {"content": m.group(1)}, "annotations": {"bold": True}})
        last = m.end()
    if last < len(line):
        parts.append(rich_text(line[last:]))
    return parts if parts else [rich_text(line)]


def paragraph_block(text: str):
    return {"type": "paragraph", "paragraph": {"rich_text": parse_rich_text(text)}}


def markdown_to_notion_blocks(brief: str, video_url: str) -> list:
    blocks = []
    lines = brief.split('\n')
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Divider
        if stripped == '---':
            blocks.append({"type": "divider", "divider": {}})
            i += 1
            continue

        # Heading 2 or 3 — check if it's a toggle section
        TOGGLE_LABELS = ('original script', 'adapted script')

        if stripped.startswith('## ') or stripped.startswith('### '):
            prefix_len = 3 if stripped.startswith('## ') else 4
            label_raw = stripped[prefix_len:].strip().rstrip(':')
            if label_raw.lower() in TOGGLE_LABELS and not label_raw.isupper():
                label = label_raw if label_raw[0].isupper() else label_raw.title()
                children = []
                i += 1
                while i < len(lines):
                    child_stripped = lines[i].strip()
                    if child_stripped.startswith('## ') or child_stripped.startswith('### ') or \
                       child_stripped == '---' or \
                       child_stripped.lower().lstrip('- ').rstrip(':') in TOGGLE_LABELS:
                        break
                    if child_stripped:
                        children.append(paragraph_block(child_stripped))
                    i += 1
                if not children:
                    children = [paragraph_block("")]
                blocks.append({
                    "type": "toggle",
                    "toggle": {
                        "rich_text": [rich_text(label)],
                        "children": children
                    }
                })
                continue
            if prefix_len == 3:
                blocks.append({
                    "type": "heading_2",
                    "heading_2": {"rich_text": [rich_text(label_raw)]}
                })
            else:
                blocks.append({
                    "type": "heading_3",
                    "heading_3": {"rich_text": [rich_text(label_raw)]}
                })
            i += 1
            continue

        # Quote
        if stripped.startswith('> '):
            content = stripped[2:]
            content = re.sub(r'\*\*(.*?)\*\*', r'\1', content)
            if 'Reference video' in content and video_url:
                rt = [
                    rich_text("❗ Reference video: "),
                    rich_text("click here", url=video_url)
                ]
            else:
                rt = [rich_text(content)]
            blocks.append({"type": "quote", "quote": {"rich_text": rt}})
            i += 1
            continue

        # Toggle: "- Original Script" or "- Adapted Script" list style
        if stripped.startswith('- ') and stripped[2:].strip().lower().rstrip(':') in TOGGLE_LABELS:
            label = stripped[2:].strip().rstrip(':')
            children = []
            i += 1
            while i < len(lines):
                child_stripped = lines[i].strip()
                if child_stripped.startswith('## ') or child_stripped.startswith('### ') or \
                   child_stripped == '---' or \
                   (child_stripped.startswith('- ') and child_stripped[2:].strip().lower().rstrip(':') in TOGGLE_LABELS):
                    break
                if child_stripped:
                    children.append(paragraph_block(child_stripped))
                i += 1
            if not children:
                children = [paragraph_block("")]
            blocks.append({
                "type": "toggle",
                "toggle": {
                    "rich_text": [rich_text(label)],
                    "children": children
                }
            })
            continue

        # Bullet list
        if stripped.startswith('- '):
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": parse_rich_text(stripped[2:])}
            })
            i += 1
            continue

        # Numbered list
        if re.match(r'^\d+\. ', stripped):
            content = re.sub(r'^\d+\. ', '', stripped)
            blocks.append({
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": parse_rich_text(content)}
            })
            i += 1
            continue

        # Bold toggle: **Original Script** or **Adapted Script**
        bold_match = re.match(r'^\*\*(.+?)\*\*$', stripped)
        if bold_match and bold_match.group(1).lower().rstrip(':') in TOGGLE_LABELS:
            label = bold_match.group(1).strip().rstrip(':')
            children = []
            i += 1
            while i < len(lines):
                child_stripped = lines[i].strip()
                if child_stripped.startswith('## ') or child_stripped.startswith('### ') or \
                   child_stripped == '---' or \
                   (child_stripped.startswith('- ') and child_stripped[2:].strip().lower().rstrip(':') in TOGGLE_LABELS) or \
                   (re.match(r'^\*\*(.+?)\*\*$', child_stripped) and re.match(r'^\*\*(.+?)\*\*$', child_stripped).group(1).lower().rstrip(':') in TOGGLE_LABELS):
                    break
                if child_stripped:
                    children.append(paragraph_block(child_stripped))
                i += 1
            if not children:
                children = [paragraph_block("")]
            blocks.append({
                "type": "toggle",
                "toggle": {
                    "rich_text": [rich_text(label)],
                    "children": children
                }
            })
            continue

        # Paragraph
        blocks.append(paragraph_block(stripped))
        i += 1

    return blocks


def extract_title(brief: str) -> str:
    # First pass: look for explicit TITLE: line (with or without bold)
    for line in brief.split('\n'):
        s = line.strip()
        # **TITLE:** value or TITLE: value
        m = re.match(r'^\*{0,2}TITLE:\*{0,2}\s*(.*)', s)
        if m and m.group(1).strip():
            return m.group(1).strip()
    # Second pass: first H1 that isn't a generic header
    for line in brief.split('\n'):
        s = line.strip()
        if re.match(r'^# (?!#)', s):
            candidate = s[2:].strip()
            if candidate.lower() not in ('content brief', 'brief', 'video brief'):
                return candidate
    return "Untitled"


def strip_title_line(brief: str) -> str:
    lines = brief.split('\n')
    filtered = [
        l for l in lines
        if not re.match(r'^\*{0,2}TITLE:\*{0,2}', l.strip())
        and not re.match(r'^# (?!#)', l.strip())
    ]
    return '\n'.join(filtered)


SCANNER_KEYWORDS = ("scan", "point my phone", "point it at", "point this", "point your phone")


def post_app_moment_comment(headers: dict, script_blocks: list) -> None:
    """Attach a B-roll direction comment to the app-integration line — mirrors
    how these briefs get annotated by hand in Notion (e.g. "Show the screen
    recording of the main page and the books"). Best-effort: a missing
    "insert comment" capability on the integration shouldn't break publishing."""
    paragraphs = [
        b for b in script_blocks
        if b.get("type") == "paragraph" and b["paragraph"].get("rich_text")
    ]
    if len(paragraphs) < 2:
        return

    # CTA is always the last line ("Comment the word BOOK..."); the app pivot is the line right before it
    pivot_block = paragraphs[-2]
    pivot_text = "".join(
        rt.get("plain_text", "") for rt in pivot_block["paragraph"]["rich_text"]
    ).lower()

    if any(k in pivot_text for k in SCANNER_KEYWORDS):
        comment_text = "Show the screen recording of the book scanner and show the slides"
    else:
        comment_text = "Show the screen recording of the main page and the books"

    try:
        requests.post(
            "https://api.notion.com/v1/comments",
            headers=headers,
            json={
                "parent": {"block_id": pivot_block["id"]},
                "rich_text": [{"text": {"content": comment_text}}]
            }
        )
    except requests.RequestException:
        pass


def upload_image_to_notion(image_bytes: bytes, filename: str, token: str) -> str | None:
    """Uploads one image via Notion's File Upload API, returns the file_upload id."""
    try:
        resp = requests.post(
            "https://api.notion.com/v1/file_uploads",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2026-03-11",
                "Content-Type": "application/json",
            },
            json={"mode": "single_part", "filename": filename, "content_type": "image/jpeg"}
        )
        resp.raise_for_status()
        upload_id = resp.json()["id"]

        send_resp = requests.post(
            f"https://api.notion.com/v1/file_uploads/{upload_id}/send",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2026-03-11"},
            files={"file": (filename, image_bytes, "image/jpeg")}
        )
        send_resp.raise_for_status()
        return upload_id
    except Exception as e:
        print(f"  [notion upload error] {filename}: {e}")
        return None


def create_notion_text_comment(block_id: str, text: str, token: str) -> bool:
    try:
        resp = requests.post(
            "https://api.notion.com/v1/comments",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
            json={
                "parent": {"type": "block_id", "block_id": block_id},
                "rich_text": [{"type": "text", "text": {"content": text}}]
            }
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"  [notion comment error] {e}")
        return False


def create_notion_link_comment(block_id: str, label: str, url: str, token: str) -> bool:
    """Same as create_notion_text_comment but the URL portion is a real
    hyperlink (clickable directly from the comment), not just plain text
    Notion may or may not auto-linkify."""
    try:
        resp = requests.post(
            "https://api.notion.com/v1/comments",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
            json={
                "parent": {"type": "block_id", "block_id": block_id},
                "rich_text": [
                    {"type": "text", "text": {"content": f"{label}: "}},
                    {"type": "text", "text": {"content": url, "link": {"url": url}}}
                ]
            }
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"  [notion comment error] {e}")
        return False


def match_images_to_adapted_script(reference_images: list[dict], adapted_lines: list[str]) -> dict[int, dict]:
    """For each reference image (with its original-transcript context), asks Claude
    which line of the ADAPTED script covers that same moment, and the exact substring
    to underline there. Returns {line_index: {"image": img_dict, "quote": str}}."""
    if not reference_images or not adapted_lines:
        return {}

    numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(adapted_lines))
    images_desc = "\n".join(
        f'{j}. Original line: "{img.get("original_context", "")}" — image shown: {img.get("caption", "")}'
        for j, img in enumerate(reference_images)
    )
    prompt = (
        f"Adapted script (numbered lines):\n{numbered}\n\n"
        f"Images found in the original video, with what was being said in the ORIGINAL "
        f"transcript when each one appeared:\n{images_desc}\n\n"
        "Every image MUST be assigned to a line — pick whichever line is the CLOSEST match "
        "even if it's not a perfect one; there is no option to skip an image. For each image, "
        "give the exact substring (word-for-word, must appear verbatim in that line) to "
        "underline. Reply ONLY as a JSON array with exactly one entry per image:\n"
        '[{"image": 0, "line": 3, "quote": "exact substring from that line"}]'
    )
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5", max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        text = next((b.text for b in msg.content if b.type == "text"), "")
        m = re.search(r"\[.*\]", text, re.DOTALL)
        raw = json.loads(m.group(0)) if m else []
    except Exception as e:
        print(f"  [image-match error] {e}")
        return {}

    result = {}
    for r in raw:
        img_idx, line_idx, quote = r.get("image"), r.get("line"), r.get("quote", "")
        if img_idx is None or line_idx is None or not quote:
            continue
        if not (0 <= img_idx < len(reference_images)) or not (0 <= line_idx < len(adapted_lines)):
            continue
        if quote not in adapted_lines[line_idx]:
            quote = adapted_lines[line_idx]  # non-verbatim quote — underline the whole line instead of dropping the image
        result[line_idx] = {"image": reference_images[img_idx], "quote": quote}
    return result


def match_headers_to_adapted_script(text_headers: list[dict], adapted_lines: list[str]) -> dict[int, str]:
    """Matches each detected on-screen text header to the ADAPTED script line
    covering that same moment, numbers them in order of appearance in the
    video, and returns {line_index: "Header: N. Title"}."""
    if not text_headers or not adapted_lines:
        return {}

    ordered = sorted(text_headers, key=lambda h: h["timestamp"])
    numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(adapted_lines))
    headers_desc = "\n".join(
        f'{j}. Original line when shown: "{h.get("original_context", "")}" — on-screen text: "{h["text"]}"'
        for j, h in enumerate(ordered)
    )
    prompt = (
        f"Adapted script (numbered lines):\n{numbered}\n\n"
        f"On-screen text headers found in the original video, in order of appearance, with "
        f"what was being said in the ORIGINAL transcript when each appeared:\n{headers_desc}\n\n"
        "For each header, find which line of the ADAPTED script covers that same moment/topic. "
        "Reply ONLY as a JSON array:\n"
        '[{"header": 0, "line": 3}]\n'
        "Skip a header only if it truly has no corresponding line in the adapted script."
    )
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5", max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        text = next((b.text for b in msg.content if b.type == "text"), "")
        m = re.search(r"\[.*\]", text, re.DOTALL)
        raw = json.loads(m.group(0)) if m else []
    except Exception as e:
        print(f"  [header-match error] {e}")
        return {}

    result = {}
    for r in raw:
        h_idx, line_idx = r.get("header"), r.get("line")
        if h_idx is None or line_idx is None:
            continue
        if not (0 <= h_idx < len(ordered)) or not (0 <= line_idx < len(adapted_lines)):
            continue
        result[line_idx] = f"Header: {h_idx + 1}. {ordered[h_idx]['text']}"
    return result


def underline_paragraph_block(text: str, quote: str):
    """Paragraph block with one substring underlined, formatting (e.g. **bold**)
    preserved outside the underlined span."""
    start = text.find(quote)
    if start == -1:
        return paragraph_block(text)
    before, after = text[:start], text[start + len(quote):]
    parts = []
    if before:
        parts.extend(parse_rich_text(before))
    parts.append({"type": "text", "text": {"content": quote}, "annotations": {"underline": True}})
    if after:
        parts.extend(parse_rich_text(after))
    return {"type": "paragraph", "paragraph": {"rich_text": parts}}


def create_notion_comment_with_image(block_id: str, image_bytes: bytes, token: str) -> bool:
    """Returns True on success. On failure (e.g. the integration doesn't have
    "Insert comments" enabled) the caller falls back to the Reference Images
    gallery so the image is never silently lost."""
    file_id = upload_image_to_notion(image_bytes, "reference.jpg", token)
    if not file_id:
        return False
    try:
        resp = requests.post(
            "https://api.notion.com/v1/comments",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2026-03-11", "Content-Type": "application/json"},
            json={
                "parent": {"type": "block_id", "block_id": block_id},
                "rich_text": [],
                "attachments": [{"type": "file_upload", "file_upload_id": file_id}]
            }
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"  [notion comment error] {e}")
        return False


def publish_to_notion(brief: str, tag: str, video_url: str, transcript: str = "", reference_images: list[dict] = None, text_headers: list[dict] = None, has_collage_background: bool = False) -> str:
    token = TF_NOTION_TOKEN if tag == 'TF' else TV_NOTION_TOKEN
    parent_id = TF_PAGE_ID if tag == 'TF' else TV_PAGE_ID

    title = extract_title(brief)
    page_title = f"Idea ({tag}) - {title}"

    clean_brief = strip_title_line(brief)
    blocks = markdown_to_notion_blocks(clean_brief, video_url)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    # Always fill Original Script toggle with the actual transcript — never trust Claude to reproduce it
    if transcript:
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', transcript) if s.strip()]
        transcript_blocks = [paragraph_block(s) for s in sentences]
        for block in blocks:
            if block.get("type") == "toggle":
                label = block["toggle"].get("rich_text", [{}])[0].get("text", {}).get("content", "").lower()
                if "original script" in label:
                    block["toggle"]["children"] = transcript_blocks

    # Match reference images to the ADAPTED script (not the original transcript) and
    # underline the line that references each one, and detect enumerated-list
    # headers (e.g. "10 habits", "top 3 ways") to comment separately — both
    # comments go on their line once its block ID exists in Notion (further down).
    image_matches = {}
    list_headers = {}
    for block in blocks:
        if block.get("type") == "toggle":
            label = block["toggle"].get("rich_text", [{}])[0].get("text", {}).get("content", "").lower()
            if "adapted script" not in label:
                continue
            children = block["toggle"].get("children", [])
            adapted_lines = [
                "".join(r.get("text", {}).get("content", "") for r in child["paragraph"]["rich_text"])
                for child in children
            ]
            if text_headers:
                list_headers = match_headers_to_adapted_script(text_headers, adapted_lines)
            if reference_images:
                image_matches = match_images_to_adapted_script(reference_images, adapted_lines)
                for line_idx, match in image_matches.items():
                    children[line_idx] = underline_paragraph_block(adapted_lines[line_idx], match["quote"])
            break

    # Extract toggle children — Notion API doesn't reliably persist nested
    # children in the page-creation call, so we append them separately.
    toggle_children = {}  # position -> (label, children list)
    flat_blocks = []
    for block in blocks:
        if block.get("type") == "toggle":
            label = block["toggle"].get("rich_text", [{}])[0].get("text", {}).get("content", "")
            children = block["toggle"].pop("children", [])
            if children:
                toggle_children[len(flat_blocks)] = (label, children)
        flat_blocks.append(block)

    page_data = {
        "parent": {"page_id": parent_id},
        "properties": {
            "title": {"title": [{"text": {"content": page_title}}]}
        },
        "children": flat_blocks[:100]
    }

    resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=page_data)
    resp.raise_for_status()
    page = resp.json()
    page_id = page["id"]

    # Upload remaining top-level blocks if > 100
    if len(flat_blocks) > 100:
        for start in range(100, len(flat_blocks), 100):
            chunk = flat_blocks[start:start + 100]
            requests.patch(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=headers,
                json={"children": chunk}
            )

    # Append toggle children separately by fetching block IDs from Notion
    if toggle_children:
        all_page_blocks = []
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = requests.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=headers, params=params
            )
            r.raise_for_status()
            data = r.json()
            all_page_blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        adapted_script_blocks = []
        for pos, (label, children) in toggle_children.items():
            if pos < len(all_page_blocks):
                toggle_id = all_page_blocks[pos]["id"]
                created = []
                for start in range(0, len(children), 100):
                    chunk = children[start:start + 100]
                    r = requests.patch(
                        f"https://api.notion.com/v1/blocks/{toggle_id}/children",
                        headers=headers,
                        json={"children": chunk}
                    )
                    if r.ok:
                        created.extend(r.json().get("results", []))
                if "adapted script" in label.lower():
                    adapted_script_blocks = created

        if tag == "TV" and adapted_script_blocks:
            post_app_moment_comment(headers, adapted_script_blocks)

        # Now that the adapted-script blocks exist in Notion, attach every
        # reference image as a comment on its matched, underlined line.
        # Every image is assigned a line by match_images_to_adapted_script (no
        # skipping), so there's no separate gallery — a failed post (e.g. API
        # error) is just logged, never surfaced as a visible fallback block.
        for line_idx, match in image_matches.items():
            if line_idx >= len(adapted_script_blocks):
                continue
            create_notion_comment_with_image(adapted_script_blocks[line_idx]["id"], match["image"]["bytes"], token)

        # Background-collage overlays aren't matched to a specific line — the
        # creator's own body always covers part of them, so no crop is usable.
        # Point to the shared drive once, on the script's first line.
        if has_collage_background and adapted_script_blocks:
            create_notion_link_comment(
                adapted_script_blocks[0]["id"],
                "Use images from the General Elements Drive",
                GENERAL_ELEMENTS_DRIVE_URL,
                token
            )

        # Comment "Header: N. Title" on each detected list-item announcement line.
        for line_idx, header_text in list_headers.items():
            if line_idx < len(adapted_script_blocks):
                create_notion_text_comment(adapted_script_blocks[line_idx]["id"], header_text, token)

    return f"https://notion.so/{page_id.replace('-', '')}"


def generate_tb_script(transcript: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are a personal brand content strategist. Analyze this video transcript and write an adapted talking head script.

Transcript:
{transcript[:3000]}

Output JSON only (no markdown):
{{
  "hook": "The hook sentence (first 1-2 sentences of the script)",
  "paragraphs": [
    "Hook — strong opener 1-2 sentences",
    "Setup paragraph",
    "Core insight",
    "Example or proof",
    "Takeaway or CTA"
  ]
}}

Rules: English, 4-6 paragraphs, rewrite fully (no lifted phrases), talking head style — direct, no fluff."""
    for attempt in range(3):
        try:
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = msg.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < 2:
                time.sleep(30 * (attempt + 1))
                continue
            raise


def publish_tb_to_notion(handle: str, hook: str, paragraphs: list, video_url: str = "") -> str:
    headers = {
        "Authorization": f"Bearer {TB_NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    hook_short = hook[:80] + ("…" if len(hook) > 80 else "")
    page_title = f"{handle} — {hook_short}"

    def _p(text):
        return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

    script_children = [_p(p) for p in paragraphs if p.strip()]

    GENERAL_ELEMENTS_URL = "https://drive.google.com/drive/folders/1T_ssCuSnjQvnSiTzYM1zXnijuo1w5rc6?usp=sharing"

    def _link(text, url):
        return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text, "link": {"url": url}}}]}}

    top_blocks = []
    if video_url:
        top_blocks.append({
            "type": "quote",
            "quote": {"rich_text": [
                {"type": "text", "text": {"content": "Reference video: "}},
                {"type": "text", "text": {"content": video_url, "link": {"url": video_url}}}
            ]}
        })
    top_blocks.append(_p("General Elements:"))
    top_blocks.append(_link(GENERAL_ELEMENTS_URL, GENERAL_ELEMENTS_URL))
    toggle_block = {
        "type": "toggle",
        "toggle": {"rich_text": [{"type": "text", "text": {"content": "Script"}, "annotations": {"bold": True}}]}
    }
    top_blocks.append(toggle_block)

    page_data = {
        "parent": {"page_id": TB_PAGE_ID},
        "properties": {"title": {"title": [{"text": {"content": page_title}}]}},
        "children": top_blocks
    }

    resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=page_data)
    resp.raise_for_status()
    page = resp.json()
    page_id = page["id"]

    # Append script paragraphs inside the toggle
    r = requests.get(f"https://api.notion.com/v1/blocks/{page_id}/children", headers=headers)
    r.raise_for_status()
    blocks = r.json().get("results", [])
    toggle_id = next((b["id"] for b in blocks if b.get("type") == "toggle"), None)
    if toggle_id:
        requests.patch(
            f"https://api.notion.com/v1/blocks/{toggle_id}/children",
            headers=headers,
            json={"children": script_children}
        )

    return f"https://notion.so/{page_id.replace('-', '')}"


def process_video(url: str, tag: str) -> dict:
    transcript = ""
    reference_images = []
    text_headers = []
    has_collage_background = False

    # Fathom: try to get transcript directly from the page (faster, no download)
    if "fathom.video" in url:
        transcript = get_fathom_transcript(url)

    if not transcript:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "audio.wav")
            actual_path = download_audio(url, audio_path)
            transcript_data = transcribe_audio(actual_path or audio_path)
            transcript = transcript_data["content"]

            # Must run inside the tmpdir block — the downloaded video file
            # gets deleted as soon as this "with" exits.
            video_path = find_downloaded_video(tmpdir)
            reference_images, text_headers, has_collage_background = extract_overlays(video_path, transcript_data.get("segments"))

    if tag == "TB":
        handle = extract_handle_from_url(url)
        hook = next((s.strip() for s in transcript.split('.') if s.strip()), transcript[:80])
        paragraphs = [s.strip() for s in re.split(r'(?<=[.!?])\s+', transcript) if s.strip()]
        page_url = publish_tb_to_notion(handle, hook, paragraphs, video_url=url)
        return {"url": page_url, "hook": hook}

    brief = generate_brief(transcript, url, tag=tag)
    page_url = publish_to_notion(brief, tag, url, transcript=transcript, reference_images=reference_images, text_headers=text_headers, has_collage_background=has_collage_background)

    hook = ""
    for line in brief.split('\n'):
        s = line.strip()
        if s and not s.startswith('#') and not s.startswith('>') and not s.startswith('-') and not s.startswith('TITLE:'):
            hook = s[:120]
            break

    return {"url": page_url, "hook": hook}


def process_video_file(file_path: str, tag: str, original_filename: str) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, "audio.wav")
        ffmpeg_result = subprocess.run([
            "ffmpeg", "-i", file_path, "-vn", "-ar", "16000", "-ac", "1", audio_path, "-y"
        ], capture_output=True, text=True)
        if not os.path.exists(audio_path):
            raise RuntimeError(f"ffmpeg failed: {ffmpeg_result.stderr[-400:]}")

        transcript_data = transcribe_audio(audio_path)
        transcript = transcript_data["content"]

    reference_images, text_headers, has_collage_background = extract_overlays(file_path, transcript_data.get("segments"))

    brief = generate_brief(transcript, original_filename)
    page_url = publish_to_notion(brief, tag, original_filename, transcript=transcript, reference_images=reference_images, text_headers=text_headers, has_collage_background=has_collage_background)

    hook = ""
    for line in brief.split('\n'):
        s = line.strip()
        if s and not s.startswith('#') and not s.startswith('>') and not s.startswith('-') and not s.startswith('TITLE:'):
            hook = s[:120]
            break

    return {"url": page_url, "hook": hook}


async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    urls, tag = extract_urls_and_tag(texto)

    if not urls or not tag:
        await update.message.reply_text("Mandame links con el tag TF, TV o TB.")
        return

    total = len(urls)
    batch_size = 3

    for batch_start in range(0, total, batch_size):
        batch = urls[batch_start:batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size

        await update.message.reply_text(
            f"⏳ Processing batch {batch_num}/{total_batches}: links {batch_start + 1}-{batch_start + len(batch)}..."
        )

        for idx, url in enumerate(batch):
            if idx > 0:
                await asyncio.sleep(15)
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, process_video, url, tag)
                await update.message.reply_text(
                    f"✅ Brief published: {result['url']}\n"
                    f"Preview: {result['hook']}"
                )
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                await update.message.reply_text(f"❌ Error con {url}:\n{str(e)[:300]}\n\n{tb[-600:]}")


async def video_responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    tag = None
    if re.search(r'\bTF\b', caption):
        tag = 'TF'
    elif re.search(r'\bTV\b', caption):
        tag = 'TV'
    elif re.search(r'\bTB\b', caption):
        tag = 'TB'

    if not tag:
        await update.message.reply_text("Mandame el video con caption TF, TV o TB.")
        return

    video = update.message.video or update.message.document
    await update.message.reply_text("⏳ Descargando y procesando video...")

    with tempfile.TemporaryDirectory() as tmpdir:
        file = await context.bot.get_file(video.file_id)
        file_path = os.path.join(tmpdir, "video.mp4")
        await file.download_to_drive(file_path)

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, process_video_file, file_path, tag, "video_upload")
            await update.message.reply_text(
                f"✅ Brief published: {result['url']}\n"
                f"Preview: {result['hook']}"
            )
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            await update.message.reply_text(f"❌ Error:\n{str(e)[:300]}\n\n{tb[-600:]}")


app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))
app.add_handler(MessageHandler((filters.VIDEO | filters.Document.VIDEO) & ~filters.COMMAND, video_responder))
app.run_polling()
