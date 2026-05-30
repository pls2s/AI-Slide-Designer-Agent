# AI Slide Designer Agent

MVP Telegram bot that analyzes PowerPoint slide screenshots/images with selectable Gemini or GPT vision APIs, and can turn every page of a slide-draft PDF into a decorated presentation-ready PNG deck.

## Features

- `/start` and `/help` commands
- Accepts slide images from Telegram photos or image documents
- Accepts PDF slide drafts and renders every page
- Downloads and validates the uploaded image
- Normalizes images with Pillow before analysis
- Lets each chat choose AI provider with `/provider`
- Sends the slide image to Google Gemini or OpenAI GPT vision
- Generates a polished decorated slide image from a PDF draft
- Returns Thai feedback covering layout, hierarchy, readability, typography, color, professionalism, and presentation effectiveness
- Includes a design score breakdown and an English image-generation prompt

## Project Structure

```text
ai-slide-designer-bot/
├── bot.py
├── config.py
├── prompts.py
├── slide_analyzer.py
├── slide_generator.py
├── slide_renderer.py
├── requirements.txt
├── .env.example
├── README.md
└── assets/
```

## Requirements

- Python 3.11+
- Telegram bot token from BotFather
- Gemini API key from Google AI Studio, OpenAI API key, or both

## Installation

```bash
cd /Users/peerawootposh/ai-slide-designer-bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and add:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here
OPENAI_API_KEY=your_openai_api_key_here
```

You can set only one AI key if you want to run one provider; leave the unused key blank. If both are set, the bot starts with Gemini by default and users can switch per chat with `/provider`.

Optional environment variables:

```env
AI_PROVIDER=
GEMINI_MODEL=gemini-2.5-flash
GEMINI_IMAGE_MODEL=gemini-2.5-flash-image
OPENAI_MODEL=gpt-4.1-mini
OPENAI_IMAGE_MODEL=gpt-image-2
LOG_LEVEL=INFO
MAX_IMAGE_SIZE_MB=15
MAX_PDF_SIZE_MB=25
REQUEST_TIMEOUT_SECONDS=120
```

Set `AI_PROVIDER=gemini` or `AI_PROVIDER=gpt` only when you want to force the startup default. If it is blank, the bot uses Gemini when `GEMINI_API_KEY` exists, otherwise GPT when `OPENAI_API_KEY` exists.

## Run

```bash
source .venv/bin/activate
python bot.py
```

The bot runs with Telegram long polling. Send it a slide screenshot/exported slide image for analysis, or a PDF slide draft to generate a ZIP of decorated PNG slides.

## AI Provider Selection

Use `/provider` in Telegram to choose the API for the current chat:

- Gemini (ฟรี/โควตาฟรี): uses `GEMINI_API_KEY`, `GEMINI_MODEL`, and `GEMINI_IMAGE_MODEL`
- GPT (เสียเงิน): uses `OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_IMAGE_MODEL`

The selection is stored in memory, so restarting the bot resets chats back to `AI_PROVIDER`.

## Deck Style Lock

Use deck mode when you want all slides in the same deck to keep one visual theme:

1. Send `/deckstart`
2. Send the first slide image or a PDF draft. The bot saves its style guide from the first slide/page.
3. Send the rest of the deck one slide at a time, or send a multi-page PDF. Theme recommendations and generated slide images will reuse the saved style.
4. Send `/deckclear` when the deck is finished.

Use `/deckstatus` to view the active style guide. Deck style is stored in memory, so restarting the bot clears it.

## PDF Decoration

Send a PDF document to the bot. The bot will:

1. Download the PDF from Telegram
2. Render every page to `slide-draft-page-001.png`, `slide-draft-page-002.png`, etc. with PyMuPDF
3. Use the selected AI provider to generate a cleaner, decorated 16:9 slide image for each page
4. Return `decorated-slide-deck.zip` as a Telegram document to avoid image compression

For multi-page PDFs, every page is processed sequentially. Large decks can take several minutes because each page requires a separate image-generation request.

## Gemini Integration

The analyzer uses the Google GenAI SDK:

```python
from google import genai

client = genai.Client(api_key=GEMINI_API_KEY)
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=[SYSTEM_PROMPT, image],
)
```

Local images are converted to Gemini parts with:

```python
from google.genai import types

image = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
```

PDF decoration with Gemini uses the Gemini image model with the rendered PDF page as an image reference:

```python
response = client.models.generate_content(
    model="gemini-2.5-flash-image",
    contents=[SLIDE_DECORATION_PROMPT, reference_image],
)
```

## GPT Integration

The analyzer uses the OpenAI Responses API with a Base64 data URL image input:

```python
from openai import OpenAI

client = OpenAI(api_key=OPENAI_API_KEY)
response = client.responses.create(
    model="gpt-4.1-mini",
    input=[{
        "role": "user",
        "content": [
            {"type": "input_text", "text": SYSTEM_PROMPT},
            {"type": "input_image", "image_url": "data:image/jpeg;base64,..."},
        ],
    }],
)
```

PDF decoration with GPT uses the OpenAI Images edit API with the rendered PDF page as a reference image:

```python
result = client.images.edit(
    model="gpt-image-2",
    image=[open("slide-draft-page-1.png", "rb")],
    prompt=SLIDE_DECORATION_PROMPT,
    size="2048x1152",
    quality="high",
)
```

## Output Format

The bot asks the selected AI provider to return:

```text
คะแนนสไลด์: X/10

Design Score Breakdown:
Layout: X/10
Typography: X/10
Visual Hierarchy: X/10
Color Usage: X/10
Readability: X/10
Professionalism: X/10

จุดแข็ง:
* ...

ปัญหาหลัก:
1. ...

ข้อเสนอแนะในการปรับ:
1. ...

Layout ใหม่ที่แนะนำ:
* Header:
* Content:
* Visual:
* Footer:

Visual Improvement:
* Suggested infographic:
* Suggested illustration:
* Suggested icon style:

Theme Recommendation:
* Colors:
* Typography:
* Design Style:

Prompt สำหรับสร้างภาพใหม่:
...
```

## Future-Ready Extension Points

The current code keeps the bot, configuration, prompt, and analysis logic separated so future features can be added without rewriting the MVP:

- Theme memory: persist preferred colors, fonts, and design styles per user
- User profiles: attach Telegram user IDs to profile settings
- Supabase integration: store users, analysis history, image metadata, and premium status
- PowerPoint generation: add a service that creates `.pptx` files from AI recommendations
- Multi-slide deck analysis: aggregate multiple uploaded slide images into a deck-level critique
- Design history: let users retrieve previous analyses
- Premium mode: rate limits, higher model tier, longer reports, and deck generation

## Production Notes

- Store secrets only in environment variables or a secret manager.
- Run behind a process manager such as systemd, supervisor, or a container runtime.
- Add persistent storage before enabling multi-slide analysis or design history.
- Add rate limiting and abuse controls before public launch.
- For webhooks, replace `run_polling()` with a Telegram webhook deployment.
# AI-Slide-Designer-Agent
