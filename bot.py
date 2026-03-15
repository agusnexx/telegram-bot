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

TELEGRAM_TOKEN = "8107809274:AAHAaxUvp6sTVbPzJ8JQV5vx333ChOZou9E"
ANTHROPIC_API_KEY = "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"

TF_NOTION_TOKEN = "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"
TF_PAGE_ID = "307ebaa28b978015a033dadaff979987"
TV_NOTION_TOKEN = "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"
TV_PAGE_ID = "307ebaa28b978023bc8ceb933a58e6b7"

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


def download_audio(url: str, output_path: str):
    result = subprocess.run([
        "yt-dlp", "-x", "--audio-format", "mp3",
        "-o", output_path, "--no-playlist",
        "--user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ], capture_output=True, text=True, timeout=180)
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

        # Heading 2
        if stripped.startswith('## '):
            blocks.append({
                "type": "heading_2",
                "heading_2": {"rich_text": [rich_text(stripped[3:])]}
            })
            i += 1
            continue

        # Heading 3
        if stripped.startswith('### '):
            blocks.append({
                "type": "heading_3",
                "heading_3": {"rich_text": [rich_text(stripped[4:])]}
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

        # Toggle: Original Script / Adapted Script
        if stripped == '- Original Script' or stripped == '- Adapted Script':
            label = stripped[2:]
            children = []
            i += 1
            while i < len(lines):
                child_stripped = lines[i].strip()
                # Stop if we hit another toggle or section
                if child_stripped in ('- Original Script', '- Adapted Script') or \
                   child_stripped.startswith('## ') or child_stripped == '---':
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

        # Paragraph
        blocks.append(paragraph_block(stripped))
        i += 1

    return blocks


def extract_title(brief: str) -> str:
    for line in brief.split('\n'):
        if line.startswith('# '):
            return line[2:].strip()
    for line in brief.split('\n'):
        if line.strip() and not line.startswith('#') and not line.startswith('>') and not line.startswith('-'):
            return line.strip()[:80]
    return "Untitled"


def publish_to_notion(brief: str, tag: str, video_url: str) -> str:
    token = TF_NOTION_TOKEN if tag == 'TF' else TV_NOTION_TOKEN
    parent_id = TF_PAGE_ID if tag == 'TF' else TV_PAGE_ID

    title = extract_title(brief)
    page_title = f"Idea ({tag}) - {title}"

    blocks = markdown_to_notion_blocks(brief, video_url)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    page_data = {
        "parent": {"page_id": parent_id},
        "properties": {
            "title": {"title": [{"text": {"content": page_title}}]}
        },
        "children": blocks[:100]
    }

    resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=page_data)
    resp.raise_for_status()
    page = resp.json()
    page_id = page["id"]

    # Upload remaining blocks if > 100
    if len(blocks) > 100:
        for start in range(100, len(blocks), 100):
            chunk = blocks[start:start + 100]
            requests.patch(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=headers,
                json={"children": chunk}
            )

    return f"https://notion.so/{page_id.replace('-', '')}"


def process_video(url: str, tag: str) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, "audio.mp3")
        download_audio(url, audio_path)

        if not os.path.exists(audio_path):
            raise RuntimeError("Audio download failed — file not found after yt-dlp")

        transcript_data = transcribe_audio(audio_path)
        transcript = transcript_data["content"]

    brief = generate_brief(transcript, url)
    page_url = publish_to_notion(brief, tag, url)

    hook = ""
    for line in brief.split('\n'):
        if line.strip() and not line.startswith('#') and not line.startswith('>') and not line.startswith('-'):
            hook = line.strip()[:120]
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
                await update.message.reply_text(f"❌ Error con {url}:\n{str(e)[:500]}")


app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))
app.run_polling()
