from __future__ import annotations

import asyncio
import logging
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import SUPPORTED_AI_PROVIDERS, Settings, configure_logging, get_settings
from slide_generator import SlideImageGenerationError, SlideImageGenerator
from slide_analyzer import SlideAnalysisError, SlideAnalyzer, UnsupportedImageError
from slide_renderer import SlideRenderError, UnsupportedPdfError, render_pdf_first_page


logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 3900

START_MESSAGE = """
สวัสดีครับ ผมคือ AI Slide Designer Agent

ส่งภาพสไลด์ PowerPoint, Keynote, Google Slides หรือ screenshot ของสไลด์มาได้เลย
ผมจะวิเคราะห์คุณภาพการออกแบบ Layout, Typography, Visual Hierarchy, สี, ความอ่านง่าย
และให้คำแนะนำในการปรับสไลด์เป็นภาษาไทย

หรือส่งไฟล์ PDF draft ของสไลด์มา เพื่อให้ผมสร้างภาพสไลด์ตกแต่งใหม่ให้สวยขึ้น

ใช้ /provider เพื่อเลือก AI API ระหว่าง Gemini กับ GPT
""".strip()

HELP_MESSAGE = """
วิธีใช้งาน:
1. ส่งภาพสไลด์ 1 หน้าในรูปแบบ PNG, JPG, JPEG หรือ WebP
2. ใช้ /provider เพื่อเลือก Gemini (ฟรี/โควตาฟรี) หรือ GPT (เสียเงิน)
3. รอระบบวิเคราะห์สไลด์ด้วย AI provider ที่เลือก
4. รับคะแนนสไลด์ จุดแข็ง ปัญหา ข้อเสนอแนะ Theme และ Prompt สำหรับสร้างภาพใหม่

สร้างภาพตกแต่งจาก PDF:
1. ส่งไฟล์ PDF draft ของสไลด์
2. ระบบจะใช้หน้าแรกของ PDF เป็น reference
3. รับไฟล์ภาพ PNG ของสไลด์ที่ตกแต่งให้ดูสวยขึ้น

คำแนะนำ:
* ใช้ภาพที่คมชัดและเห็นทั้งสไลด์
* ถ้าเป็น deck หลายหน้า ให้ส่งทีละภาพ
* ถ้าเป็น PDF หลายหน้า ระบบจะใช้หน้าแรกก่อน
* หลีกเลี่ยงภาพที่ถูก crop หรือเบลอมาก
""".strip()

NON_IMAGE_MESSAGE = """
กรุณาส่งภาพสไลด์เป็นรูปภาพ หรือส่ง PDF draft ของสไลด์ครับ
ใช้ /help เพื่อดูวิธีใช้งาน
""".strip()

PROVIDER_CALLBACK_PREFIX = "provider:"


@dataclass(frozen=True, slots=True)
class FileAttachment:
    file_id: str
    file_name: str
    file_size: int | None


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


async def handle_slide_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    settings: Settings = context.application.bot_data["settings"]
    analyzer: SlideAnalyzer = context.application.bot_data["slide_analyzer"]
    provider = _get_selected_provider(context, message.chat_id, settings)

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

    provider_label = settings.provider_label(provider)
    status_message = await message.reply_text(
        f"กำลังดาวน์โหลดและวิเคราะห์สไลด์ด้วย {provider_label}..."
    )
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

            analysis = await analyzer.analyze_image(image_path, provider)

        await status_message.delete()
        await _reply_long_text(message, analysis)
    except UnsupportedImageError:
        await status_message.edit_text(
            "อ่านไฟล์ภาพไม่ได้ครับ กรุณาส่งไฟล์ PNG, JPG, JPEG หรือ WebP ที่เปิดได้ตามปกติ"
        )
    except SlideAnalysisError:
        logger.exception("Slide analysis failed")
        await status_message.edit_text(
            "วิเคราะห์สไลด์ไม่สำเร็จครับ กรุณาลองส่งภาพที่คมชัดกว่าเดิมอีกครั้ง"
        )
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
        await handle_slide_pdf(update, context)
        return

    await handle_slide_image(update, context)


async def handle_slide_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    settings: Settings = context.application.bot_data["settings"]
    generator: SlideImageGenerator = context.application.bot_data[
        "slide_image_generator"
    ]
    provider = _get_selected_provider(context, message.chat_id, settings)

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

    provider_label = settings.provider_label(provider)
    status_message = await message.reply_text(
        f"กำลังแปลง PDF และสร้างภาพสไลด์ตกแต่งด้วย {provider_label}..."
    )
    typing_task = asyncio.create_task(_send_typing_until_done(context, message.chat_id))

    try:
        with tempfile.TemporaryDirectory(prefix="ai-slide-designer-pdf-") as temp_dir:
            temp_path = Path(temp_dir)
            pdf_path = temp_path / attachment.file_name
            reference_image_path = temp_path / "slide-draft-page-1.png"
            output_image_path = temp_path / "decorated-slide.png"

            telegram_file = await context.bot.get_file(
                attachment.file_id,
                read_timeout=settings.request_timeout_seconds,
                write_timeout=settings.request_timeout_seconds,
            )
            await telegram_file.download_to_drive(custom_path=pdf_path)

            await asyncio.to_thread(
                render_pdf_first_page,
                pdf_path,
                reference_image_path,
            )
            await generator.generate_decorated_slide(
                reference_image_path,
                provider,
                output_image_path,
            )

            await status_message.delete()
            with output_image_path.open("rb") as image_file:
                await message.reply_document(
                    document=image_file,
                    filename="decorated-slide.png",
                    caption=(
                        "สร้างภาพสไลด์ตกแต่งจาก PDF หน้าแรกเรียบร้อยครับ "
                        f"({provider_label})"
                    ),
                )
    except UnsupportedPdfError:
        await status_message.edit_text(
            "อ่านไฟล์ PDF ไม่ได้ครับ กรุณาส่ง PDF ที่เปิดได้ตามปกติ"
        )
    except SlideRenderError:
        logger.exception("PDF rendering failed")
        await status_message.edit_text(
            "แปลง PDF เป็นภาพไม่สำเร็จครับ กรุณาลอง export หน้า slide เป็น PDF ใหม่"
        )
    except SlideImageGenerationError:
        logger.exception("Decorated slide image generation failed")
        await status_message.edit_text(
            "สร้างภาพสไลด์ตกแต่งไม่สำเร็จครับ กรุณาลองส่ง PDF ที่ชัดขึ้นอีกครั้ง"
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
    if update.effective_message:
        await update.effective_message.reply_text(NON_IMAGE_MESSAGE)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram update error", exc_info=context.error)


def build_application(settings: Settings) -> Application:
    analyzer = SlideAnalyzer(
        gemini_api_key=settings.gemini_api_key,
        gemini_model=settings.gemini_model,
        openai_api_key=settings.openai_api_key,
        openai_model=settings.openai_model,
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

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("provider", provider_command))
    application.add_handler(
        CallbackQueryHandler(
            handle_provider_selection,
            pattern=f"^{PROVIDER_CALLBACK_PREFIX}",
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


def _get_selected_provider(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    settings: Settings,
) -> str:
    provider = _chat_provider_map(context).get(chat_id)
    if provider and settings.is_provider_available(provider):
        return provider
    return settings.ai_provider


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
        ]
    )


def _provider_selected_text(settings: Settings, provider: str) -> str:
    return "\n".join(
        [
            f"ตั้งค่าแชตนี้ให้ใช้ {settings.provider_label(provider)} แล้ว",
            f"Analysis model: {settings.provider_model(provider)}",
            f"Image model: {settings.provider_image_model(provider)}",
            "",
            "ส่งภาพสไลด์หรือ PDF draft มาได้เลยครับ",
        ]
    )


def _missing_provider_key_text(settings: Settings, provider: str) -> str:
    key_name = "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"
    return "\n".join(
        [
            f"ยังใช้ {settings.provider_label(provider)} ไม่ได้ครับ",
            f"กรุณาตั้งค่า {key_name} ในไฟล์ .env แล้ว restart bot",
        ]
    )


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
        await message.reply_text(chunk)


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
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(4)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    application = build_application(settings)

    logger.info(
        "Starting AI Slide Designer Agent provider=%s "
        "gemini_model=%s gemini_image_model=%s "
        "openai_model=%s openai_image_model=%s",
        settings.ai_provider,
        settings.gemini_model,
        settings.gemini_image_model,
        settings.openai_model,
        settings.openai_image_model,
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
