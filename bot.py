import subprocess
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

TELEGRAM_TOKEN = "8107809274:AAHAaxUvp6sTVbPzJ8JQV5vx333ChOZou9E"


BRIEF_SYSTEM_PROMPT = """
Content Brief Maker for DeepStash — Notion

PIPELINE

STEP 1 — Receive input
The user sends one or more TikTok or Instagram links tagged with TF or TV.
- TF = Talking Head focused on being funny and relatable
- TV = Talking Head focused on value

The tag can appear anywhere in the message near the link. Extract ALL video URLs from the message and detect the tag (TF or TV). The tag applies to ALL links in the message.

If there are more than 3 links, process them in batches of 3. After finishing each batch, continue automatically with the next batch until all links are processed. Always inform the user which batch you are processing (e.g. "Processing batch 1/4: links 1-3...").

Valid input examples:
- TF https://www.tiktok.com/...
- https://www.tiktok.com/... TF https://www.instagram.com/... TF
- TV https://www.tiktok.com/... https://www.instagram.com/...

STEP 2 — Transcribe the video
Run this exact command and capture the JSON output:
  python3 /Users/agustinescoda/Documents/telegram-bot/transcribe_whisper.py <video_url>

The script uses Playwright to download the video and Whisper to transcribe it.
It returns JSON: {"content": "...", "lang": "en"}
Parse the JSON and extract "content" as the transcript.

STEP 3 — Generate the content brief
Read the full contents of:
  /Users/agustinescoda/Documents/telegram-bot/BRIEF_PROMPT.md  (system instructions)
  /Users/agustinescoda/Documents/telegram-bot/clients/deepstash.md  (client config)

Call the Anthropic Claude API to generate the brief:
- Model: claude-sonnet-4-20250514
- Max tokens: 4096
- API key: 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
- System prompt: contents of BRIEF_PROMPT.md
- User message:
    ## CLIENT CONTEXT
    [contents of deepstash.md]

    ## REFERENCE VIDEO URL
    [video_url]

    ## LANGUAGE
    English (EN) — MANDATORY: Write the ENTIRE brief in English.

    ## TRANSCRIPT
    [transcript content]

    ---
    Generate the content brief following the instructions exactly. Output only the brief in markdown, nothing else.

STEP 4 — Publish to Notion
- Notion-Version header: 2022-06-28
- If the tag is TF: API key = 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 — Page ID = 307ebaa28b978015a033dadaff979987
- If the tag is TV: API key = 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 — Page ID = 307ebaa28b978023bc8ceb933a58e6b7

Extract the title from the brief (the title that appears after "# " at the top, or generate one from the hook if none exists).
Create a new page as a child of the parent page with:
- Title: "Idea (TF) - [extracted title]" if the tag is TF, or "Idea (TV) - [extracted title]" if the tag is TV
- Content: the full brief converted to Notion blocks

Block conversion rules:
- ## Heading → heading_2
- ### Heading → heading_3
- > Quote → quote
- - Item → bulleted_list_item
- 1. Item → numbered_list_item
- --- → divider
- **bold** → text with bold annotation
- Regular text → paragraph
- Max 100 blocks per API call — chunk if needed

Special rule for the reference video line:
The line containing "Reference video" must be published as a quote block where the URL is a
real clickable hyperlink using Notion rich_text link format:
{"type": "text", "text": {"content": "click here", "link": {"url": "THE_ACTUAL_URL"}}}

Special rule for ADAPTED SCRIPT section:
- "Original Script" → publish as a toggle block (type: toggle) with its paragraphs as children
- "Adapted Script" → publish as a toggle block (type: toggle) with its paragraphs as children
- Use this Notion block format for toggles:
  {
    "type": "toggle",
    "toggle": {
      "rich_text": [{"type": "text", "text": {"content": "Original Script"}}],
      "children": [ ...paragraph blocks... ]
    }
  }

STEP 5 — Return result
Reply with:
- ✅ Brief published: [Notion page URL]
- Title: [brief title]
- Preview: [first line of the hook]

TECHNICAL NOTES
- transcribe_whisper.py timeout is 10 minutes — wait for it
- If transcribe_whisper.py returns {"error": "..."}, report the error clearly
- Use python3 for all scripts
"""

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    await update.message.reply_text("⏳ Ejecutando en Claude Code...")

    prompt = f"{BRIEF_SYSTEM_PROMPT}\n\nUsuario: {texto}"

    resultado = subprocess.run(
        ["claude", "-p", prompt, "--dangerously-skip-permissions"],
        capture_output=True,
        text=True,
        timeout=900,
        cwd="/Users/agustinescoda/Documents"
    )

    reply = resultado.stdout or resultado.stderr or "Sin respuesta"

    if len(reply) > 4000:
        reply = reply[:4000] + "..."

    await update.message.reply_text(reply)

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))
app.run_polling()
