#!/usr/bin/env python3
"""
Baja el audio de un video de TikTok/Instagram con Playwright y lo transcribe con Whisper.
Uso: python3 transcribe_whisper.py <url>
Salida: JSON con { "content": "...", "lang": "..." }
"""

import sys
import os
import json
import shutil
import tempfile
import subprocess
import asyncio


def get_ffmpeg() -> str:
    """Devuelve el path de ffmpeg, usando imageio-ffmpeg si no está en PATH."""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return "ffmpeg"
    except (FileNotFoundError, subprocess.CalledProcessError):
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            raise RuntimeError("ffmpeg no encontrado. Instalá imageio-ffmpeg: pip3 install imageio-ffmpeg")


def setup_ffmpeg_in_path(ffmpeg_path: str) -> None:
    """Copia ffmpeg a /tmp y lo prepone al PATH para que Whisper lo encuentre."""
    tmp_ffmpeg = "/tmp/ffmpeg"
    if not os.path.exists(tmp_ffmpeg):
        shutil.copy2(ffmpeg_path, tmp_ffmpeg)
        os.chmod(tmp_ffmpeg, 0o755)
    env_path = os.environ.get("PATH", "")
    if "/tmp" not in env_path.split(os.pathsep):
        os.environ["PATH"] = "/tmp" + os.pathsep + env_path


def download_video_ytdlp(url: str, output_path: str) -> None:
    """Descarga el video con yt-dlp (funciona con Instagram y TikTok)."""
    result = subprocess.run(
        ["python3", "-m", "yt_dlp", "-o", output_path, "--merge-output-format", "mp4", url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp falló: {result.stderr}")


async def download_video_playwright(url: str, output_path: str) -> None:
    from playwright.async_api import async_playwright

    video_body = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        async def handle_video_route(route):
            nonlocal video_body
            if not video_body:
                response = await route.fetch()
                ct = response.headers.get("content-type", "")
                if "video" in ct:
                    body = await response.body()
                    if len(body) > 100_000:
                        video_body = body
                await route.fulfill(response=response)
            else:
                await route.continue_()

        # Solo interceptar requests a CDNs de video de TikTok/Instagram
        await page.route("**/v16-webapp*/**", handle_video_route)
        await page.route("**/v19-webapp*/**", handle_video_route)
        await page.route("**/v26-webapp*/**", handle_video_route)
        await page.route("**/*.tiktokcdn.com/**", handle_video_route)
        await page.route("**/*.cdninstagram.com/**", handle_video_route)

        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)
        await browser.close()

    if not video_body:
        raise RuntimeError("No se pudo capturar el video de la página")

    with open(output_path, "wb") as f:
        f.write(video_body)


def extract_audio(video_path: str, audio_path: str, ffmpeg_path: str) -> None:
    cmd = [ffmpeg_path, "-i", video_path, "-vn", "-acodec", "mp3", "-q:a", "0", audio_path, "-y", "-loglevel", "quiet"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg falló: {result.stderr}")


def transcribe(audio_path: str) -> dict:
    import whisper
    model = whisper.load_model("medium")
    result = model.transcribe(audio_path, fp16=False)
    return {
        "content": result["text"].strip(),
        "lang": result.get("language", "en"),
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Uso: python3 transcribe_whisper.py <url>"}))
        sys.exit(1)

    url = sys.argv[1]

    try:
        ffmpeg_path = get_ffmpeg()
        setup_ffmpeg_in_path(ffmpeg_path)

        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = os.path.join(tmpdir, "video.mp4")
            audio_path = os.path.join(tmpdir, "audio.mp3")

            if "instagram.com" in url:
                download_video_ytdlp(url, video_path)
            else:
                try:
                    asyncio.run(download_video_playwright(url, video_path))
                except Exception:
                    download_video_ytdlp(url, video_path)

            if not os.path.exists(video_path) or os.path.getsize(video_path) < 1000:
                raise RuntimeError("El video descargado está vacío o es inválido")

            extract_audio(video_path, audio_path, ffmpeg_path)

            if not os.path.exists(audio_path):
                raise RuntimeError("No se pudo extraer el audio del video")

            result = transcribe(audio_path)

        print(json.dumps(result, ensure_ascii=False))

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
