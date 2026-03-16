import asyncio
import os
import re
import json
import tempfile
import subprocess
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
    return urls, tag


def get_cookies_file() -> str:
    """Write Instagram cookies to a temp file if env var is set."""
    cookies = os.environ.get("INSTAGRAM_COOKIES", "")
    if not cookies:
        return None
    import tempfile
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
    f.write("# Netscape HTTP Cookie File\n")
    cookie_pairs = {}
    for item in cookies.split(';'):
        item = item.strip()
        if '=' in item:
            name, value = item.split('=', 1)
            cookie_pairs[name.strip()] = value.strip()
    for name, value in cookie_pairs.items():
        f.write(f".instagram.com\tTRUE\t/\tTRUE\t2999999999\t{name}\t{value}\n")
    f.close()
    return f.name


def download_audio(url: str, output_path: str):
    cmd = [
        "python3", "-m", "yt_dlp",
        "-o", output_path,
        "--no-playlist",
        "--format", "bestaudio[ext=m4a]/bestaudio/best[acodec!=none]/best",
    ]
    if "instagram.com" in url:
        cookies_file = get_cookies_file()
        if cookies_file:
            cmd += ["--cookies", cookies_file]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp error: {result.stderr[-500:]}")


def transcribe_audio(audio_path: str) -> dict:
    from faster_whisper import WhisperModel
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio_path, beam_size=5)
    text = " ".join([seg.text for seg in segments]).strip()
    return {"content": text, "lang": info.language}


def generate_brief(transcript: str, video_url: str) -> str:
    brief_prompt = BRIEF_PROMPT_PATH.read_text()
    client_context = CLIENT_PATH.read_text()

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
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


def publish_to_notion(brief: str, tag: str, video_url: str, transcript: str = "") -> str:
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

    # If Original Script toggle has no children, fill with transcript as fallback
    if transcript:
        for block in blocks:
            if block.get("type") == "toggle":
                label = block["toggle"].get("rich_text", [{}])[0].get("text", {}).get("content", "").lower()
                children = block["toggle"].get("children", [])
                is_empty = not children or (len(children) == 1 and not children[0].get("paragraph", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "").strip())
                if "original script" in label and is_empty:
                    block["toggle"]["children"] = [
                        paragraph_block(line.strip())
                        for line in transcript.split('. ')
                        if line.strip()
                    ]

    # Extract toggle children — Notion API doesn't reliably persist nested
    # children in the page-creation call, so we append them separately.
    toggle_children = {}  # position -> children list
    flat_blocks = []
    for block in blocks:
        if block.get("type") == "toggle":
            children = block["toggle"].pop("children", [])
            if children:
                toggle_children[len(flat_blocks)] = children
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

        for pos, children in toggle_children.items():
            if pos < len(all_page_blocks):
                toggle_id = all_page_blocks[pos]["id"]
                for start in range(0, len(children), 100):
                    chunk = children[start:start + 100]
                    requests.patch(
                        f"https://api.notion.com/v1/blocks/{toggle_id}/children",
                        headers=headers,
                        json={"children": chunk}
                    )

    return f"https://notion.so/{page_id.replace('-', '')}"


def process_video(url: str, tag: str) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, "video.%(ext)s")
        download_audio(url, video_path)

        # Find the downloaded file
        import glob
        files = glob.glob(os.path.join(tmpdir, "video.*"))
        if not files:
            raise RuntimeError("Download failed — file not found after yt-dlp")
        video_path = files[0]

        # Extract audio with ffmpeg
        audio_path = os.path.join(tmpdir, "audio.wav")
        ffmpeg_result = subprocess.run([
            "ffmpeg", "-i", video_path, "-ar", "16000", "-ac", "1", audio_path, "-y"
        ], capture_output=True, text=True)
        if not os.path.exists(audio_path):
            raise RuntimeError(f"ffmpeg failed (rc={ffmpeg_result.returncode}): {ffmpeg_result.stderr[-400:]}")

        transcript_data = transcribe_audio(audio_path)
        transcript = transcript_data["content"]

    brief = generate_brief(transcript, url)
    page_url = publish_to_notion(brief, tag, url, transcript=transcript)

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
        await update.message.reply_text("Mandame links con el tag TF o TV.")
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

        for url in batch:
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


app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))
app.run_polling()
