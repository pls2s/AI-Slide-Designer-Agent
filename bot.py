from __future__ import annotations

import asyncio
import logging
import tempfile
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import SUPPORTED_AI_PROVIDERS, Settings, configure_logging, get_settings
from slide_generator import (
    SlideImageGenerationError,
    SlideImageGenerator,
    SlideImageQuotaError,
)
from slide_analyzer import SlideAnalysisError, SlideAnalyzer, UnsupportedImageError
from slide_renderer import SlideRenderError, UnsupportedPdfError, render_pdf_pages


logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 3900

START_MESSAGE = """
สวัสดีครับ ผมคือ AI Slide Designer Agent

ส่งภาพสไลด์ PowerPoint, Keynote, Google Slides หรือ screenshot ของสไลด์มาได้เลย
ผมจะวิเคราะห์คุณภาพการออกแบบ Layout, Typography, Visual Hierarchy, สี, ความอ่านง่าย
และให้คำแนะนำในการปรับสไลด์เป็นภาษาไทย

หรือส่งไฟล์ PDF draft ของสไลด์มา เพื่อให้ผมสร้างภาพสไลด์ตกแต่งใหม่ให้สวยขึ้น

ใช้ /provider เพื่อเลือก AI API ระหว่าง Gemini กับ GPT
ใช้ /theme เพื่อเลือกธีมสไลด์ หรือ /themetemplate เพื่อส่งภาพธีมต้นแบบ
ใช้ /deckstart ก่อนส่งสไลด์ชุดเดียวกัน เพื่อ lock style ให้ทั้ง deck
""".strip()

HELP_MESSAGE = """
วิธีใช้งาน:
1. ส่งภาพสไลด์ 1 หน้าในรูปแบบ PNG, JPG, JPEG หรือ WebP
2. ใช้ /provider เพื่อเลือก Gemini (ฟรี/โควตาฟรี) หรือ GPT (เสียเงิน)
3. ใช้ /theme ถ้าต้องการเลือกธีม หรือ /themetemplate เพื่อส่งภาพต้นแบบ
4. รอระบบวิเคราะห์สไลด์ด้วย AI provider ที่เลือก
5. รับคะแนนสไลด์ จุดแข็ง ปัญหา ข้อเสนอแนะ Theme และ Prompt สำหรับสร้างภาพใหม่

สร้างภาพตกแต่งจาก PDF:
1. ส่งไฟล์ PDF draft ของสไลด์
2. ระบบจะแปลงทุกหน้าของ PDF เป็น reference
3. รับไฟล์ ZIP รวมภาพ PNG ของทุกสไลด์ที่ตกแต่งให้ดูสวยขึ้น

ทำ style ให้ทั้ง deck เหมือนกัน:
* /theme เลือก preset theme ให้สไลด์ถัดไป
* /themetemplate แล้วส่งภาพ/PDF ต้นแบบ เพื่อใช้เป็น theme
* /themestatus ดู theme ที่ใช้อยู่
* /themeclear ล้าง theme ที่เลือก
* /deckstart เริ่ม deck mode แล้วส่งสไลด์หน้าแรกเพื่อบันทึก style
* /deckstatus ดู style ที่ใช้อยู่
* /deckclear ล้าง style เมื่อจบ deck

คำแนะนำ:
* ใช้ภาพที่คมชัดและเห็นทั้งสไลด์
* ถ้าเป็น deck หลายหน้า ให้ส่งทีละภาพ
* ถ้าเป็น PDF หลายหน้า ระบบจะสร้างภาพกลับมาทุกหน้า
* หลีกเลี่ยงภาพที่ถูก crop หรือเบลอมาก
""".strip()

NON_IMAGE_MESSAGE = """
กรุณาส่งภาพสไลด์เป็นรูปภาพ หรือส่ง PDF draft ของสไลด์ครับ
ใช้ /help เพื่อดูวิธีใช้งาน
""".strip()

PROVIDER_CALLBACK_PREFIX = "provider:"
THEME_CALLBACK_PREFIX = "theme:"


@dataclass(frozen=True, slots=True)
class FileAttachment:
    file_id: str
    file_name: str
    file_size: int | None


@dataclass(slots=True)
class DeckStyleState:
    active: bool = False
    style_guide: str | None = None
    slide_count: int = 0


@dataclass(frozen=True, slots=True)
class ThemePreset:
    key: str
    label: str
    style_guide: str


@dataclass(slots=True)
class ThemeState:
    active: bool = False
    label: str | None = None
    style_guide: str | None = None
    awaiting_template: bool = False


THEME_PRESETS = (
    ThemePreset(
        key="corporate",
        label="Clean Corporate",
        style_guide="""
Deck Style Guide:
- Overall design direction: clean corporate presentation with high clarity,
  restrained visual accents, and strong business readability.
- Color palette: white and very light gray backgrounds, deep navy text,
  teal or blue accent lines, sparing use of warm highlight colors.
- Typography: modern sans-serif, clear title hierarchy, medium-weight headings,
  compact body text with generous line spacing.
- Layout system: structured grids, left-aligned content, clear section headers,
  balanced whitespace, and simple data callouts.
- Visual elements: thin dividers, subtle geometric accents, clean icons,
  charts with minimal decoration.
- Icon/illustration style: outline or duotone business icons.
- Chart/table style: simple axes, direct labels, restrained gridlines.
- Background treatment: mostly flat light backgrounds with subtle panels.
- Spacing and alignment: precise alignment and consistent margins.
- What to avoid: busy gradients, decorative clutter, playful fonts, and low contrast.
""".strip(),
    ),
    ThemePreset(
        key="startup",
        label="Bold Startup",
        style_guide="""
Deck Style Guide:
- Overall design direction: energetic startup pitch deck with confident contrast,
  bold sectioning, and modern product storytelling.
- Color palette: charcoal or off-white base, electric blue, cyan, lime,
  and coral accents used selectively.
- Typography: bold sans-serif headlines, short punchy labels, clean body text.
- Layout system: asymmetric but controlled layouts, large numbers,
  strong callout blocks, and clear visual anchors.
- Visual elements: accent bars, metric badges, product-style cards,
  simplified diagrams, and strong CTA-style emphasis.
- Icon/illustration style: modern filled or duotone icons with rounded geometry.
- Chart/table style: big headline metrics, simplified comparison charts,
  direct labels instead of dense legends.
- Background treatment: subtle depth, soft shadows, and crisp accent shapes.
- Spacing and alignment: roomy, presentation-ready spacing with clear focal points.
- What to avoid: academic density, weak hierarchy, muted same-color-only palettes.
""".strip(),
    ),
    ThemePreset(
        key="academic",
        label="Academic Minimal",
        style_guide="""
Deck Style Guide:
- Overall design direction: academic and thesis-friendly slides with calm,
  credible structure and strong readability.
- Color palette: white background, slate or black text, one scholarly accent
  color such as blue, green, or burgundy.
- Typography: readable sans-serif or serif-like academic pairing, clear headings,
  conservative body sizing.
- Layout system: title at top, content grouped into logical blocks,
  diagrams and tables with enough whitespace.
- Visual elements: labeled diagrams, simple callout boxes, footnote areas,
  restrained icons.
- Icon/illustration style: minimal line icons only when they clarify meaning.
- Chart/table style: precise labels, visible units, accessible contrast,
  minimal decorative effects.
- Background treatment: plain and clean with optional light section bands.
- Spacing and alignment: consistent margins and disciplined alignment.
- What to avoid: excessive decoration, illegible dense text, informal styling.
""".strip(),
    ),
    ThemePreset(
        key="premium",
        label="Premium Pitch",
        style_guide="""
Deck Style Guide:
- Overall design direction: premium investor or executive deck with polished
  contrast, refined whitespace, and confident visual hierarchy.
- Color palette: deep charcoal, black, ivory, metallic gold or champagne accents,
  plus one cool supporting accent when needed.
- Typography: elegant modern sans-serif, sharp title scale, concise text blocks.
- Layout system: cinematic section layouts, large visual areas, strong margins,
  selective premium callouts.
- Visual elements: fine rules, elegant number treatments, subtle image overlays,
  refined iconography.
- Icon/illustration style: thin line icons or minimal premium pictograms.
- Chart/table style: high-contrast labels, simplified executive summaries,
  minimal gridlines.
- Background treatment: dark or ivory flat backgrounds with restrained depth.
- Spacing and alignment: generous whitespace and exact alignment.
- What to avoid: cheap-looking gradients, overcrowded slides, inconsistent accents.
""".strip(),
    ),
    ThemePreset(
        key="workshop",
        label="Workshop Bright",
        style_guide="""
Deck Style Guide:
- Overall design direction: friendly workshop or classroom slides that feel
  clear, approachable, and easy to follow.
- Color palette: white or soft light base with balanced blue, green, coral,
  and yellow accents.
- Typography: friendly sans-serif, clear headings, readable body text,
  short instructional labels.
- Layout system: modular content blocks, step-by-step sections,
  visible activity areas, and simple examples.
- Visual elements: checklists, process arrows, soft highlight boxes,
  simple illustrations.
- Icon/illustration style: rounded line or filled icons with friendly proportions.
- Chart/table style: simplified visuals with clear labels and color coding.
- Background treatment: light and clean with soft section bands.
- Spacing and alignment: comfortable spacing that supports scanning.
- What to avoid: corporate stiffness, tiny text, overly decorative backgrounds.
""".strip(),
    ),
)
THEME_PRESET_BY_KEY = {preset.key: preset for preset in THEME_PRESETS}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(START_MESSAGE)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_MESSAGE)


async def provider_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    settings: Settings = context.application.bot_data["settings"]
    provider = _get_selected_provider(context, message.chat_id, settings)
    await message.reply_text(
        _provider_selection_text(settings, provider),
        reply_markup=_provider_keyboard(settings, provider),
    )


async def theme_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    theme_state = _get_theme_state(context, message.chat_id)
    await message.reply_text(
        _theme_selection_text(theme_state),
        reply_markup=_theme_keyboard(theme_state),
    )


async def theme_template_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    theme_state = _get_theme_state(context, message.chat_id)
    theme_state.awaiting_template = True
    await message.reply_text(_theme_template_waiting_text())


async def theme_status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    theme_state = _get_theme_state(context, message.chat_id)
    await message.reply_text(_theme_status_text(theme_state))


async def theme_clear_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    theme_state = _get_theme_state(context, message.chat_id)
    _clear_theme_state(theme_state)
    await message.reply_text("ล้าง theme แล้วครับ สไลด์ถัดไปจะใช้คำแนะนำแบบอิสระ")


async def deck_start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    state = _get_deck_style_state(context, message.chat_id)
    state.active = True
    state.style_guide = None
    state.slide_count = 0
    await message.reply_text(
        "เริ่ม deck mode แล้วครับ\n\n"
        "ส่งสไลด์หน้าแรกของ deck นี้มา ระบบจะใช้หน้านั้นสร้าง style guide "
        "แล้วบังคับใช้ theme เดียวกันกับสไลด์ถัดไป"
    )


async def deck_status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    state = _get_deck_style_state(context, message.chat_id)
    await message.reply_text(_deck_status_text(state))


async def deck_clear_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    state = _get_deck_style_state(context, message.chat_id)
    state.active = False
    state.style_guide = None
    state.slide_count = 0
    theme_state = _get_theme_state(context, message.chat_id)
    if _has_active_theme(theme_state):
        await message.reply_text(
            f"ล้าง deck style แล้วครับ แต่ Theme: {_theme_label(theme_state)} ยังใช้อยู่"
        )
        return

    await message.reply_text("ล้าง deck style แล้วครับ สไลด์ถัดไปจะใช้คำแนะนำแบบอิสระ")


async def handle_provider_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None:
        return

    await query.answer()

    provider = (query.data or "").removeprefix(PROVIDER_CALLBACK_PREFIX)
    settings: Settings = context.application.bot_data["settings"]
    if provider not in SUPPORTED_AI_PROVIDERS:
        await query.edit_message_text("ตัวเลือก AI API ไม่ถูกต้องครับ ใช้ /provider อีกครั้ง")
        return

    if not settings.is_provider_available(provider):
        await query.edit_message_text(
            _missing_provider_key_text(settings, provider),
            reply_markup=_provider_keyboard(settings, settings.ai_provider),
        )
        return

    chat = update.effective_chat
    if chat is None:
        await query.edit_message_text("เลือก provider ไม่สำเร็จครับ ใช้ /provider อีกครั้ง")
        return

    _chat_provider_map(context)[chat.id] = provider
    await query.edit_message_text(
        _provider_selected_text(settings, provider),
        reply_markup=_provider_keyboard(settings, provider),
    )


async def handle_theme_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None:
        return

    await query.answer()

    chat = update.effective_chat
    if chat is None:
        await query.edit_message_text("เลือก theme ไม่สำเร็จครับ ใช้ /theme อีกครั้ง")
        return

    action = (query.data or "").removeprefix(THEME_CALLBACK_PREFIX)
    theme_state = _get_theme_state(context, chat.id)

    if action == "template":
        theme_state.awaiting_template = True
        await query.edit_message_text(_theme_template_waiting_text())
        return

    if action == "clear":
        _clear_theme_state(theme_state)
        await query.edit_message_text(
            "ล้าง theme แล้วครับ สไลด์ถัดไปจะใช้คำแนะนำแบบอิสระ",
            reply_markup=_theme_keyboard(theme_state),
        )
        return

    preset = THEME_PRESET_BY_KEY.get(action)
    if preset is None:
        await query.edit_message_text("ตัวเลือก theme ไม่ถูกต้องครับ ใช้ /theme อีกครั้ง")
        return

    _set_theme_from_preset(theme_state, preset)
    await query.edit_message_text(
        _theme_selected_text(theme_state),
        reply_markup=_theme_keyboard(theme_state),
    )


async def handle_slide_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    settings: Settings = context.application.bot_data["settings"]
    analyzer: SlideAnalyzer = context.application.bot_data["slide_analyzer"]
    provider = _get_selected_provider(context, message.chat_id, settings)
    deck_state = _get_deck_style_state(context, message.chat_id)
    theme_state = _get_theme_state(context, message.chat_id)

    try:
        attachment = _extract_image_attachment(message)
    except UnsupportedImageError:
        await message.reply_text(NON_IMAGE_MESSAGE)
        return

    if attachment.file_size and attachment.file_size > settings.max_image_size_bytes:
        await message.reply_text(
            f"ไฟล์ภาพใหญ่เกินไปครับ ขนาดสูงสุดคือ {settings.max_image_size_mb} MB"
        )
        return

    if not settings.is_provider_available(provider):
        await message.reply_text(_missing_provider_key_text(settings, provider))
        return

    if theme_state.awaiting_template:
        await _capture_theme_template_from_image(
            message,
            context,
            settings,
            analyzer,
            provider,
            attachment,
            theme_state,
        )
        return

    provider_label = settings.provider_label(provider)
    style_status = _style_status_suffix(deck_state, theme_state)
    status_message = await _reply_text_with_retry(
        message,
        f"กำลังดาวน์โหลดและวิเคราะห์สไลด์ด้วย {provider_label}{style_status}..."
    )
    if status_message is None:
        return
    typing_task = asyncio.create_task(_send_typing_until_done(context, message.chat_id))

    try:
        with tempfile.TemporaryDirectory(prefix="ai-slide-designer-") as temp_dir:
            image_path = Path(temp_dir) / attachment.file_name
            telegram_file = await context.bot.get_file(
                attachment.file_id,
                read_timeout=settings.request_timeout_seconds,
                write_timeout=settings.request_timeout_seconds,
            )
            await telegram_file.download_to_drive(custom_path=image_path)

            style_was_created = False
            if deck_state.active and not deck_state.style_guide:
                await status_message.edit_text(
                    "กำลังอ่านและบันทึก style ของ deck จากสไลด์หน้าแรก..."
                )
                deck_state.style_guide = await analyzer.extract_deck_style(
                    image_path,
                    provider,
                )
                style_was_created = True

            analysis = await analyzer.analyze_image(
                image_path,
                provider,
                _active_style_guide(deck_state, theme_state),
            )
            if deck_state.active:
                deck_state.slide_count += 1
                analysis = _with_deck_prefix(analysis, deck_state, style_was_created)
            elif _has_active_theme(theme_state):
                analysis = _with_theme_prefix(analysis, theme_state)

        await status_message.delete()
        await _reply_long_text(message, analysis)
    except UnsupportedImageError:
        await status_message.edit_text(
            "อ่านไฟล์ภาพไม่ได้ครับ กรุณาส่งไฟล์ PNG, JPG, JPEG หรือ WebP ที่เปิดได้ตามปกติ"
        )
    except SlideAnalysisError as exc:
        logger.warning("Slide analysis failed: %s", exc)
        await status_message.edit_text(_analysis_error_text(exc))
    except TelegramError:
        logger.exception("Telegram API error while handling image")
        with suppress(TelegramError):
            await status_message.edit_text(
                "เกิดปัญหาระหว่างรับส่งไฟล์กับ Telegram กรุณาลองใหม่อีกครั้ง"
            )
    except Exception:
        logger.exception("Unexpected error while handling image")
        await status_message.edit_text(
            "เกิดข้อผิดพลาดที่ไม่คาดคิด กรุณาลองใหม่อีกครั้ง"
        )
    finally:
        typing_task.cancel()
        with suppress(asyncio.CancelledError):
            await typing_task


async def handle_slide_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    if _is_pdf_document(message):
        theme_state = _get_theme_state(context, message.chat_id)
        if theme_state.awaiting_template:
            await handle_theme_template_pdf(update, context)
            return

        await handle_slide_pdf(update, context)
        return

    await handle_slide_image(update, context)


async def handle_theme_template_pdf(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    settings: Settings = context.application.bot_data["settings"]
    analyzer: SlideAnalyzer = context.application.bot_data["slide_analyzer"]
    provider = _get_selected_provider(context, message.chat_id, settings)
    theme_state = _get_theme_state(context, message.chat_id)

    try:
        attachment = _extract_pdf_attachment(message)
    except UnsupportedPdfError:
        await message.reply_text("กรุณาส่งภาพหรือ PDF ที่ใช้เป็น theme template ครับ")
        return

    if attachment.file_size and attachment.file_size > settings.max_pdf_size_bytes:
        await message.reply_text(
            f"ไฟล์ PDF ใหญ่เกินไปครับ ขนาดสูงสุดคือ {settings.max_pdf_size_mb} MB"
        )
        return

    if not settings.is_provider_available(provider):
        await message.reply_text(_missing_provider_key_text(settings, provider))
        return

    provider_label = settings.provider_label(provider)
    status_message = await _reply_text_with_retry(
        message,
        f"กำลังอ่าน theme จาก PDF หน้าแรกด้วย {provider_label}..."
    )
    if status_message is None:
        return
    typing_task = asyncio.create_task(_send_typing_until_done(context, message.chat_id))

    try:
        with tempfile.TemporaryDirectory(prefix="ai-slide-theme-pdf-") as temp_dir:
            temp_path = Path(temp_dir)
            pdf_path = temp_path / attachment.file_name
            reference_dir = temp_path / "rendered-pages"

            telegram_file = await context.bot.get_file(
                attachment.file_id,
                read_timeout=settings.request_timeout_seconds,
                write_timeout=settings.request_timeout_seconds,
            )
            await telegram_file.download_to_drive(custom_path=pdf_path)

            reference_image_paths = await asyncio.to_thread(
                render_pdf_pages,
                pdf_path,
                reference_dir,
            )
            style_guide = await analyzer.extract_deck_style(
                reference_image_paths[0],
                provider,
            )
            _set_theme_from_template(theme_state, style_guide, "Theme จาก PDF ต้นแบบ")

        await status_message.edit_text(
            _theme_template_saved_text(theme_state),
            reply_markup=_theme_keyboard(theme_state),
        )
    except UnsupportedPdfError:
        await status_message.edit_text(
            "อ่านไฟล์ PDF ไม่ได้ครับ กรุณาส่ง PDF ที่เปิดได้ตามปกติ"
        )
    except SlideRenderError:
        logger.exception("Theme template PDF rendering failed")
        await status_message.edit_text(
            "แปลง PDF template เป็นภาพไม่สำเร็จครับ กรุณาลอง export PDF ใหม่"
        )
    except SlideAnalysisError as exc:
        logger.warning("Theme template extraction failed: %s", exc)
        await status_message.edit_text(_analysis_error_text(exc))
    except TelegramError:
        logger.exception("Telegram API error while handling theme template PDF")
        with suppress(TelegramError):
            await status_message.edit_text(
                "เกิดปัญหาระหว่างรับส่งไฟล์กับ Telegram กรุณาลองใหม่อีกครั้ง"
            )
    except Exception:
        logger.exception("Unexpected error while handling theme template PDF")
        await status_message.edit_text(
            "เกิดข้อผิดพลาดที่ไม่คาดคิด กรุณาลองใหม่อีกครั้ง"
        )
    finally:
        typing_task.cancel()
        with suppress(asyncio.CancelledError):
            await typing_task


async def handle_slide_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    settings: Settings = context.application.bot_data["settings"]
    analyzer: SlideAnalyzer = context.application.bot_data["slide_analyzer"]
    generator: SlideImageGenerator = context.application.bot_data[
        "slide_image_generator"
    ]
    provider = _get_selected_provider(context, message.chat_id, settings)
    deck_state = _get_deck_style_state(context, message.chat_id)
    theme_state = _get_theme_state(context, message.chat_id)

    try:
        attachment = _extract_pdf_attachment(message)
    except UnsupportedPdfError:
        await message.reply_text(NON_IMAGE_MESSAGE)
        return

    if attachment.file_size and attachment.file_size > settings.max_pdf_size_bytes:
        await message.reply_text(
            f"ไฟล์ PDF ใหญ่เกินไปครับ ขนาดสูงสุดคือ {settings.max_pdf_size_mb} MB"
        )
        return

    if not settings.is_provider_available(provider):
        await message.reply_text(_missing_provider_key_text(settings, provider))
        return

    if provider == "ollama":
        await message.reply_text(_pdf_generation_not_supported_text(settings, provider))
        return

    provider_label = settings.provider_label(provider)
    style_status = _style_status_suffix(deck_state, theme_state)
    status_message = await _reply_text_with_retry(
        message,
        f"กำลังแปลง PDF ทั้ง deck และสร้างภาพสไลด์ตกแต่งด้วย {provider_label}{style_status}..."
    )
    if status_message is None:
        return
    typing_task = asyncio.create_task(_send_typing_until_done(context, message.chat_id))

    try:
        with tempfile.TemporaryDirectory(prefix="ai-slide-designer-pdf-") as temp_dir:
            temp_path = Path(temp_dir)
            pdf_path = temp_path / attachment.file_name
            reference_dir = temp_path / "rendered-pages"
            output_dir = temp_path / "decorated-pages"
            output_zip_path = temp_path / "decorated-slide-deck.zip"
            output_dir.mkdir(parents=True, exist_ok=True)

            telegram_file = await context.bot.get_file(
                attachment.file_id,
                read_timeout=settings.request_timeout_seconds,
                write_timeout=settings.request_timeout_seconds,
            )
            await telegram_file.download_to_drive(custom_path=pdf_path)

            reference_image_paths = await asyncio.to_thread(
                render_pdf_pages,
                pdf_path,
                reference_dir,
            )

            style_was_created = False
            if deck_state.active and not deck_state.style_guide:
                await status_message.edit_text(
                    "กำลังอ่านและบันทึก style ของ deck จาก PDF หน้าแรก..."
                )
                deck_state.style_guide = await analyzer.extract_deck_style(
                    reference_image_paths[0],
                    provider,
                )
                style_was_created = True

            output_image_paths: list[Path] = []
            for page_index, reference_image_path in enumerate(reference_image_paths, 1):
                await status_message.edit_text(
                    "กำลังสร้างภาพสไลด์ตกแต่ง "
                    f"{page_index}/{len(reference_image_paths)} ด้วย {provider_label}..."
                )
                output_image_path = output_dir / f"decorated-slide-{page_index:03}.png"
                await generator.generate_decorated_slide(
                    reference_image_path,
                    provider,
                    output_image_path,
                    _active_style_guide(deck_state, theme_state),
                )
                output_image_paths.append(output_image_path)

            await asyncio.to_thread(_zip_files, output_image_paths, output_zip_path)
            if deck_state.active:
                deck_state.slide_count += len(reference_image_paths)

            await status_message.delete()
            with output_zip_path.open("rb") as zip_file:
                await message.reply_document(
                    document=zip_file,
                    filename="decorated-slide-deck.zip",
                    caption=_decorated_deck_caption(
                        provider_label,
                        deck_state,
                        style_was_created,
                        len(reference_image_paths),
                        theme_state,
                    ),
                )
    except UnsupportedPdfError:
        await status_message.edit_text(
            "อ่านไฟล์ PDF ไม่ได้ครับ กรุณาส่ง PDF ที่เปิดได้ตามปกติ"
        )
    except SlideRenderError:
        logger.exception("PDF rendering failed")
        await status_message.edit_text(
            "แปลง PDF เป็นภาพไม่สำเร็จครับ กรุณาลอง export deck เป็น PDF ใหม่"
        )
    except SlideImageQuotaError as exc:
        logger.warning("Decorated slide image generation quota exceeded: %s", exc)
        await status_message.edit_text(str(exc))
    except SlideImageGenerationError:
        logger.exception("Decorated slide image generation failed")
        await status_message.edit_text(
            "สร้างภาพสไลด์ตกแต่งทั้ง deck ไม่สำเร็จครับ กรุณาลองส่ง PDF ที่ชัดขึ้นอีกครั้ง"
        )
    except TelegramError:
        logger.exception("Telegram API error while handling PDF")
        with suppress(TelegramError):
            await status_message.edit_text(
                "เกิดปัญหาระหว่างรับส่งไฟล์กับ Telegram กรุณาลองใหม่อีกครั้ง"
            )
    except Exception:
        logger.exception("Unexpected error while handling PDF")
        await status_message.edit_text(
            "เกิดข้อผิดพลาดที่ไม่คาดคิด กรุณาลองใหม่อีกครั้ง"
        )
    finally:
        typing_task.cancel()
        with suppress(asyncio.CancelledError):
            await typing_task


async def handle_non_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    theme_state = _get_theme_state(context, message.chat_id)
    if theme_state.awaiting_template:
        await message.reply_text(_theme_template_waiting_text())
        return

    await message.reply_text(NON_IMAGE_MESSAGE)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning("Telegram network error while processing update: %s", context.error)
        return

    logger.exception("Unhandled Telegram update error", exc_info=context.error)


def build_application(settings: Settings) -> Application:
    analyzer = SlideAnalyzer(
        gemini_api_key=settings.gemini_api_key,
        gemini_model=settings.gemini_model,
        openai_api_key=settings.openai_api_key,
        openai_model=settings.openai_model,
        ollama_base_url=settings.ollama_base_url,
        ollama_model=settings.ollama_model,
        request_timeout_seconds=settings.request_timeout_seconds,
    )
    generator = SlideImageGenerator(
        gemini_api_key=settings.gemini_api_key,
        gemini_image_model=settings.gemini_image_model,
        openai_api_key=settings.openai_api_key,
        openai_image_model=settings.openai_image_model,
    )

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["settings"] = settings
    application.bot_data["slide_analyzer"] = analyzer
    application.bot_data["slide_image_generator"] = generator
    application.bot_data["chat_ai_providers"] = {}
    application.bot_data["deck_style_states"] = {}
    application.bot_data["theme_states"] = {}

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("provider", provider_command))
    application.add_handler(CommandHandler("theme", theme_command))
    application.add_handler(CommandHandler("themes", theme_command))
    application.add_handler(CommandHandler("themetemplate", theme_template_command))
    application.add_handler(CommandHandler("themestatus", theme_status_command))
    application.add_handler(CommandHandler("themeclear", theme_clear_command))
    application.add_handler(CommandHandler("deckstart", deck_start_command))
    application.add_handler(CommandHandler("deckstatus", deck_status_command))
    application.add_handler(CommandHandler("deckclear", deck_clear_command))
    application.add_handler(
        CallbackQueryHandler(
            handle_provider_selection,
            pattern=f"^{PROVIDER_CALLBACK_PREFIX}",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            handle_theme_selection,
            pattern=f"^{THEME_CALLBACK_PREFIX}",
        )
    )
    application.add_handler(MessageHandler(filters.PHOTO, handle_slide_image))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_slide_document))
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_non_image)
    )
    application.add_error_handler(error_handler)

    return application


def _chat_provider_map(context: ContextTypes.DEFAULT_TYPE) -> dict[int, str]:
    return context.application.bot_data.setdefault("chat_ai_providers", {})


def _deck_style_map(context: ContextTypes.DEFAULT_TYPE) -> dict[int, DeckStyleState]:
    return context.application.bot_data.setdefault("deck_style_states", {})


def _theme_state_map(context: ContextTypes.DEFAULT_TYPE) -> dict[int, ThemeState]:
    return context.application.bot_data.setdefault("theme_states", {})


def _get_deck_style_state(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> DeckStyleState:
    states = _deck_style_map(context)
    state = states.get(chat_id)
    if state is None:
        state = DeckStyleState()
        states[chat_id] = state
    return state


def _get_theme_state(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> ThemeState:
    states = _theme_state_map(context)
    state = states.get(chat_id)
    if state is None:
        state = ThemeState()
        states[chat_id] = state
    return state


def _get_selected_provider(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    settings: Settings,
) -> str:
    provider = _chat_provider_map(context).get(chat_id)
    if provider and settings.is_provider_available(provider):
        return provider
    return settings.ai_provider


async def _capture_theme_template_from_image(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    analyzer: SlideAnalyzer,
    provider: str,
    attachment: FileAttachment,
    theme_state: ThemeState,
) -> None:
    provider_label = settings.provider_label(provider)
    status_message = await _reply_text_with_retry(
        message,
        f"กำลังอ่าน theme จากภาพต้นแบบด้วย {provider_label}..."
    )
    if status_message is None:
        return
    typing_task = asyncio.create_task(_send_typing_until_done(context, message.chat_id))

    try:
        with tempfile.TemporaryDirectory(prefix="ai-slide-theme-") as temp_dir:
            image_path = Path(temp_dir) / attachment.file_name
            telegram_file = await context.bot.get_file(
                attachment.file_id,
                read_timeout=settings.request_timeout_seconds,
                write_timeout=settings.request_timeout_seconds,
            )
            await telegram_file.download_to_drive(custom_path=image_path)

            style_guide = await analyzer.extract_deck_style(image_path, provider)
            _set_theme_from_template(theme_state, style_guide, "Theme จากภาพต้นแบบ")

        await status_message.edit_text(
            _theme_template_saved_text(theme_state),
            reply_markup=_theme_keyboard(theme_state),
        )
    except UnsupportedImageError:
        await status_message.edit_text(
            "อ่านภาพต้นแบบไม่ได้ครับ กรุณาส่งไฟล์ PNG, JPG, JPEG หรือ WebP ที่เปิดได้ตามปกติ"
        )
    except SlideAnalysisError as exc:
        logger.warning("Theme template extraction failed: %s", exc)
        await status_message.edit_text(_analysis_error_text(exc))
    except TelegramError:
        logger.exception("Telegram API error while handling theme template image")
        with suppress(TelegramError):
            await status_message.edit_text(
                "เกิดปัญหาระหว่างรับส่งไฟล์กับ Telegram กรุณาลองใหม่อีกครั้ง"
            )
    except Exception:
        logger.exception("Unexpected error while handling theme template image")
        await status_message.edit_text(
            "เกิดข้อผิดพลาดที่ไม่คาดคิด กรุณาลองใหม่อีกครั้ง"
        )
    finally:
        typing_task.cancel()
        with suppress(asyncio.CancelledError):
            await typing_task


def _set_theme_from_preset(state: ThemeState, preset: ThemePreset) -> None:
    state.active = True
    state.label = preset.label
    state.style_guide = preset.style_guide
    state.awaiting_template = False


def _set_theme_from_template(
    state: ThemeState,
    style_guide: str,
    label: str,
) -> None:
    state.active = True
    state.label = label
    state.style_guide = style_guide
    state.awaiting_template = False


def _clear_theme_state(state: ThemeState) -> None:
    state.active = False
    state.label = None
    state.style_guide = None
    state.awaiting_template = False


def _has_active_theme(state: ThemeState) -> bool:
    return state.active and bool(state.style_guide)


def _theme_label(state: ThemeState) -> str:
    return state.label or "Theme ที่เลือก"


def _active_style_guide(
    deck_state: DeckStyleState,
    theme_state: ThemeState,
) -> str | None:
    if deck_state.active and deck_state.style_guide:
        return deck_state.style_guide
    if _has_active_theme(theme_state):
        return theme_state.style_guide
    return None


def _style_status_suffix(
    deck_state: DeckStyleState,
    theme_state: ThemeState,
) -> str:
    if deck_state.active:
        return " และ deck style"
    if _has_active_theme(theme_state):
        return f" และ theme: {_theme_label(theme_state)}"
    return ""


def _deck_status_text(state: DeckStyleState) -> str:
    if not state.active:
        return "Deck mode ยังไม่ได้เปิดครับ ใช้ /deckstart เพื่อ lock style ให้ deck เดียวกัน"

    if not state.style_guide:
        return (
            "Deck mode เปิดอยู่ แต่ยังไม่มี style guide\n\n"
            "ส่งสไลด์หน้าแรกมา ระบบจะใช้หน้านั้นบันทึก style ของ deck"
        )

    return "\n".join(
        [
            "Deck mode เปิดอยู่",
            f"ประมวลผลแล้ว: {state.slide_count} หน้า",
            "",
            state.style_guide,
        ]
    )


def _with_deck_prefix(
    text: str,
    state: DeckStyleState,
    style_was_created: bool,
) -> str:
    if style_was_created:
        prefix = (
            "บันทึก Deck Style จากสไลด์หน้าแรกแล้วครับ "
            "สไลด์ถัดไปในแชตนี้จะใช้ theme เดียวกัน\n\n"
        )
    else:
        prefix = (
            f"ใช้ Deck Style เดิมสำหรับหน้า {state.slide_count} แล้วครับ\n\n"
        )

    return f"{prefix}{text}"


def _with_theme_prefix(text: str, state: ThemeState) -> str:
    return f"ใช้ Theme: {_theme_label(state)}\n\n{text}"


def _decorated_deck_caption(
    provider_label: str,
    state: DeckStyleState,
    style_was_created: bool,
    page_count: int,
    theme_state: ThemeState,
) -> str:
    caption = (
        f"สร้างภาพสไลด์ตกแต่งจาก PDF ครบ {page_count} หน้าแล้วครับ "
        f"({provider_label})\nส่งกลับเป็น ZIP รวมไฟล์ PNG ทุกหน้า"
    )
    if state.active:
        if style_was_created:
            return f"{caption}\nบันทึก Deck Style แล้ว และใช้ theme เดียวกันทั้ง deck"
        return f"{caption}\nใช้ Deck Style เดิมสำหรับหน้า {state.slide_count}"
    if _has_active_theme(theme_state):
        return f"{caption}\nใช้ Theme: {_theme_label(theme_state)}"
    return caption


def _zip_files(file_paths: list[Path], zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in file_paths:
            archive.write(file_path, arcname=file_path.name)
    return zip_path


def _provider_selection_text(settings: Settings, selected_provider: str) -> str:
    return "\n".join(
        [
            "เลือก AI API สำหรับแชตนี้",
            "",
            f"ตอนนี้ใช้: {settings.provider_label(selected_provider)}",
            f"Analysis model: {settings.provider_model(selected_provider)}",
            f"Image model: {settings.provider_image_model(selected_provider)}",
            "",
            "Gemini เหมาะกับโหมดฟรี/ประหยัด",
            "GPT ใช้ OpenAI API และมีค่าใช้จ่ายตามบัญชี",
            "Ollama รันในเครื่อง เหมาะกับวิเคราะห์ภาพ แต่ยังไม่รองรับสร้างภาพ PDF",
        ]
    )


def _provider_selected_text(settings: Settings, provider: str) -> str:
    return "\n".join(
        [
            f"ตั้งค่าแชตนี้ให้ใช้ {settings.provider_label(provider)} แล้ว",
            f"Analysis model: {settings.provider_model(provider)}",
            f"Image model: {settings.provider_image_model(provider)}",
            "",
            _provider_ready_next_step(provider),
        ]
    )


def _missing_provider_key_text(settings: Settings, provider: str) -> str:
    if provider == "ollama":
        return (
            "Ollama ใช้ไม่ได้ครับ กรุณาเปิด Ollama app และตรวจว่าโหลด model แล้ว "
            "ด้วย `ollama list`"
        )

    key_name = "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"
    return "\n".join(
        [
            f"ยังใช้ {settings.provider_label(provider)} ไม่ได้ครับ",
            f"กรุณาตั้งค่า {key_name} ในไฟล์ .env แล้ว restart bot",
        ]
    )


def _provider_ready_next_step(provider: str) -> str:
    if provider == "ollama":
        return "ส่งภาพสไลด์มาได้เลยครับ ถ้าจะสร้างภาพจาก PDF ให้ใช้ /provider เลือก Gemini หรือ GPT"

    return "ส่งภาพสไลด์หรือ PDF draft มาได้เลยครับ"


def _pdf_generation_not_supported_text(settings: Settings, provider: str) -> str:
    return "\n".join(
        [
            f"{settings.provider_label(provider)} ยังไม่รองรับการสร้างภาพสไลด์จาก PDF ครับ",
            "",
            "Ollama/Qwen2.5-VL ใช้ได้กับการวิเคราะห์ภาพและดึง theme จากภาพ/PDF template",
            "ถ้าต้องการตกแต่ง PDF เป็นภาพสไลด์ ให้ใช้ /provider เลือก Gemini หรือ GPT",
        ]
    )


def _analysis_error_text(exc: SlideAnalysisError) -> str:
    text = str(exc).strip()
    if text:
        return text

    return "วิเคราะห์สไลด์ไม่สำเร็จครับ กรุณาลองใหม่อีกครั้ง"


def _theme_selection_text(state: ThemeState) -> str:
    lines = ["เลือก theme สำหรับสไลด์ถัดไปในแชตนี้", ""]
    if state.awaiting_template:
        lines.extend(
            [
                "ตอนนี้กำลังรอภาพหรือ PDF ต้นแบบ",
                "ส่งตัวอย่างธีมมาได้เลย ระบบจะดึง style guide จากไฟล์นั้น",
                "",
            ]
        )
    elif _has_active_theme(state):
        lines.extend(["ตอนนี้ใช้:", _theme_label(state), ""])
    else:
        lines.extend(["ตอนนี้ยังไม่ได้ lock theme", ""])

    lines.extend(
        [
            "เลือก preset ด้านล่าง หรือกดใช้ภาพ/PDF เป็นต้นแบบ",
            "เมื่อตั้ง theme แล้ว การวิเคราะห์ภาพและการตกแต่ง PDF จะใช้ theme นี้",
        ]
    )
    return "\n".join(lines)


def _theme_selected_text(state: ThemeState) -> str:
    return "\n".join(
        [
            f"ตั้ง Theme: {_theme_label(state)} แล้ว",
            "",
            "ส่งภาพสไลด์หรือ PDF draft มาได้เลย ระบบจะใช้ theme นี้เป็นทิศทางการออกแบบ",
        ]
    )


def _theme_template_waiting_text() -> str:
    return "\n".join(
        [
            "ส่งภาพหรือ PDF ต้นแบบ theme มาได้เลยครับ",
            "",
            "ถ้าส่งภาพ ระบบจะดึง style จากภาพนั้น",
            "ถ้าส่ง PDF ระบบจะใช้หน้าแรกเป็นต้นแบบ",
            "หลังบันทึกแล้ว สไลด์ถัดไปจะใช้ theme เดียวกัน",
        ]
    )


def _theme_template_saved_text(state: ThemeState) -> str:
    return "\n".join(
        [
            f"บันทึก {_theme_label(state)} แล้วครับ",
            "",
            "ส่งภาพสไลด์หรือ PDF draft ถัดไปมาได้เลย ระบบจะใช้ theme นี้เป็นต้นแบบ",
        ]
    )


def _theme_status_text(state: ThemeState) -> str:
    if state.awaiting_template:
        return _theme_template_waiting_text()

    if not _has_active_theme(state):
        return "ยังไม่ได้เลือก theme ครับ ใช้ /theme เพื่อเลือก preset หรือ /themetemplate เพื่อส่งต้นแบบ"

    return "\n".join(
        [
            f"Theme ที่ใช้อยู่: {_theme_label(state)}",
            "",
            state.style_guide or "",
        ]
    )


def _theme_keyboard(state: ThemeState) -> InlineKeyboardMarkup:
    rows = []
    selected_label = _theme_label(state) if _has_active_theme(state) else None
    for preset in THEME_PRESETS:
        selected_prefix = "✓ " if preset.label == selected_label else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{selected_prefix}{preset.label}",
                    callback_data=f"{THEME_CALLBACK_PREFIX}{preset.key}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "ใช้ภาพ/PDF เป็นต้นแบบ",
                callback_data=f"{THEME_CALLBACK_PREFIX}template",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "ล้าง theme",
                callback_data=f"{THEME_CALLBACK_PREFIX}clear",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _provider_keyboard(settings: Settings, selected_provider: str) -> InlineKeyboardMarkup:
    rows = []
    for provider in SUPPORTED_AI_PROVIDERS:
        selected_prefix = "✓ " if provider == selected_provider else ""
        unavailable_suffix = (
            "" if settings.is_provider_available(provider) else " (ยังไม่ตั้งค่า key)"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    (
                        f"{selected_prefix}"
                        f"{settings.provider_label(provider)}"
                        f"{unavailable_suffix}"
                    ),
                    callback_data=f"{PROVIDER_CALLBACK_PREFIX}{provider}",
                )
            ]
        )

    return InlineKeyboardMarkup(rows)


def _is_pdf_document(message: Message) -> bool:
    if not message.document:
        return False

    file_name = message.document.file_name or ""
    return (
        message.document.mime_type == "application/pdf"
        or Path(file_name).suffix.lower() == ".pdf"
    )


def _extract_pdf_attachment(message: Message) -> FileAttachment:
    if not _is_pdf_document(message):
        raise UnsupportedPdfError("Document is not a PDF.")

    document = message.document
    if document is None:
        raise UnsupportedPdfError("No PDF attachment found.")

    return FileAttachment(
        file_id=document.file_id,
        file_name="telegram-slide-draft.pdf",
        file_size=document.file_size,
    )


def _extract_image_attachment(message: Message) -> FileAttachment:
    if message.photo:
        photo = message.photo[-1]
        return FileAttachment(
            file_id=photo.file_id,
            file_name="telegram-slide-photo.jpg",
            file_size=photo.file_size,
        )

    if message.document and message.document.mime_type:
        if not message.document.mime_type.startswith("image/"):
            raise UnsupportedImageError("Document is not an image.")

        suffix = Path(message.document.file_name or "slide-image").suffix
        file_name = f"telegram-slide-document{suffix or '.jpg'}"
        return FileAttachment(
            file_id=message.document.file_id,
            file_name=file_name,
            file_size=message.document.file_size,
        )

    raise UnsupportedImageError("No image attachment found.")


async def _reply_long_text(message: Message, text: str) -> None:
    for chunk in _split_text(text):
        if await _reply_text_with_retry(message, chunk) is None:
            return


async def _reply_text_with_retry(
    message: Message,
    text: str,
    *,
    attempts: int = 3,
    **kwargs: Any,
) -> Message | None:
    for attempt in range(1, attempts + 1):
        try:
            return await message.reply_text(text, **kwargs)
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
        except (NetworkError, TimedOut) as exc:
            if attempt >= attempts:
                logger.warning(
                    "Could not send Telegram message after %s attempts: %s",
                    attempts,
                    exc,
                )
                return None
            await asyncio.sleep(attempt)

    return None


def _split_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}".strip()
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)

        if len(paragraph) <= limit:
            current = paragraph
        else:
            chunks.extend(_split_oversized_paragraph(paragraph, limit))
            current = ""

    if current:
        chunks.append(current)
    return chunks


def _split_oversized_paragraph(paragraph: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in paragraph.splitlines():
        candidate = f"{current}\n{line}".strip()
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)

        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line

    if current:
        chunks.append(current)
    return chunks


async def _send_typing_until_done(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    while True:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except (NetworkError, TimedOut) as exc:
            logger.warning("Could not send Telegram typing action: %s", exc)
        await asyncio.sleep(4)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    application = build_application(settings)

    logger.info(
        "Starting AI Slide Designer Agent provider=%s "
        "gemini_model=%s gemini_image_model=%s "
        "openai_model=%s openai_image_model=%s "
        "ollama_model=%s ollama_base_url=%s",
        settings.ai_provider,
        settings.gemini_model,
        settings.gemini_image_model,
        settings.openai_model,
        settings.openai_image_model,
        settings.ollama_model,
        settings.ollama_base_url,
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
