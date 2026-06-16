from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from io import BytesIO
from pathlib import Path
from urllib import error as url_error
from urllib import request as url_request

from google import genai
from google.genai import types
from openai import OpenAI
from PIL import Image, ImageOps, UnidentifiedImageError

from prompts import DECK_STYLE_EXTRACTION_PROMPT, build_analysis_prompt


logger = logging.getLogger(__name__)

MAX_IMAGE_DIMENSION = 2400
JPEG_QUALITY = 92
REFERENCE_PROMPT_PREFIX = "Use the provided slide image as the primary visual reference."
OLLAMA_PROMPT_SUFFIX = """

Ollama note: write Thai critique only, no Chinese characters. Fill every field.
In the final prompt section, write only the actual English image-edit prompt.
It must begin with: Use the provided slide image as the primary visual reference.
Do not write a generic preservation prompt. Name concrete visible details from
the slide: readable text, object counts, positions, colors, shapes, connectors,
callouts, charts, tables, and any icons you can see.
""".rstrip()
OLLAMA_REFERENCE_PROMPT_REPAIR = f"""
You are writing ONLY one professional English image-edit prompt for a slide
redesign image model.

Look carefully at the uploaded slide screenshot. The prompt will be used
together with this same screenshot as the image reference.

Requirements:
- Start exactly with: "{REFERENCE_PROMPT_PREFIX}"
- Describe the actual visible slide, not a generic template.
- Write as an instruction to an image-edit model, not as critique or analysis.
- Include concrete details you can see: readable text, number of main elements,
  positions, colors, shapes, connectors, callouts/buttons, charts/tables/icons,
  background, title/subtitle placement, and spacing.
- It is okay to quote visible Thai slide text inside the English prompt.
- Preserve the original topic, all readable text, layout structure, color
  palette, visual hierarchy, and template identity.
- Improve only professional polish: alignment, spacing, typography, contrast,
  hierarchy, and subtle decoration.
- Do not add unrelated imagery, fake data, new logos, new claims, or a different
  theme.
- Return one paragraph only. Do not add labels, markdown, explanations, or
  placeholders.
""".strip()
PROMPT_SECTION_TITLE = "Prompt สำหรับสร้างภาพใหม่:"
CJK_PATTERN = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
THAI_PATTERN = re.compile(r"[\u0e00-\u0e7f]")
CONCRETE_REFERENCE_PATTERN = re.compile(
    r"\b("
    r"circle|rounded|button|callout|timeline|workflow|flow|arrow|connector|"
    r"red|blue|purple|green|teal|orange|yellow|pink|white|gray|grey|black|"
    r"top|bottom|left|right|center|middle|header|headline|subtitle|footer|"
    r"label|badge|card|box|diagram|icon|table|chart|photo|image|step|pill"
    r")\b",
    re.IGNORECASE,
)
PROMPT_DIAGNOSTIC_PHRASES = (
    "score",
    "difficult to see",
    "hard to read",
    "poor alignment",
    "visually appealing",
    "look cluttered",
    "looks cluttered",
    "could benefit",
    "might be improved",
    "may be difficult",
    "lacks",
)
INSTRUCTION_LEAK_MARKERS = (
    "Continue with concrete details",
    "Style Lock:",
    "Use the following style guide",
    "Private style guidance",
    "Ollama note:",
    "Additional instruction",
    "requested output structure",
    "followed by one professional English prompt",
)
REFERENCE_PROMPT_FALLBACK = (
    "Use the provided slide image as the primary visual reference. Recreate the "
    "same slide template instead of inventing a new concept: keep the exact 16:9 "
    "canvas, light background, title/subtitle/header areas, same number of main "
    "diagram/chart/table elements, same shape types, approximate element sizes, "
    "connectors, labels, callout/button areas, and original color palette. "
    "Preserve every readable text string exactly as shown; if exact text "
    "rendering is uncertain, keep the text blocks, spacing, and hierarchy "
    "faithful to the reference instead of inventing replacements. Improve only "
    "professional polish: cleaner alignment, more balanced spacing, sharper "
    "typography, stronger contrast, clearer hierarchy, and subtle decoration "
    "that matches the existing visual style. Do not add unrelated imagery, fake "
    "data, new logos, new claims, or a different theme."
)
REFERENCE_EDIT_CONSTRAINTS = (
    " Preserve every readable text string exactly as shown, keep the same number "
    "of visible elements and their approximate positions, preserve the original "
    "color palette and template identity, and improve only professional polish: "
    "alignment, spacing, typography, contrast, hierarchy, and subtle decoration. "
    "Do not add unrelated imagery, fake data, new logos, new claims, or a "
    "different theme."
)
BLANK_FIELD_DEFAULTS = {
    "Header": "ใช้หัวสไลด์สำหรับชื่อเรื่องหลักที่อ่านชัดและวางแนวให้สอดคล้องกับธีม",
    "Content": "จัดกลุ่มเนื้อหาหลักให้เป็นบล็อกที่สแกนง่ายและมีลำดับความสำคัญชัดเจน",
    "Visual": "คงองค์ประกอบภาพเดิมและเพิ่มเฉพาะ visual accent ที่ช่วยเน้นข้อมูล",
    "Footer": "ใช้พื้นที่ท้ายสไลด์อย่างเรียบง่ายสำหรับหมายเหตุ แหล่งที่มา หรือปล่อยว่างถ้าไม่จำเป็น",
    "Suggested infographic": "ใช้ infographic แบบเรียบง่ายที่รักษาโครงข้อมูลเดิมและช่วยเน้นประเด็นหลัก",
    "Suggested illustration": "ใช้ภาพประกอบหรือ accent ที่สอดคล้องกับเนื้อหาโดยไม่เปลี่ยนธีมของสไลด์",
    "Suggested icon style": "ใช้ไอคอนเส้นบางหรือ duotone ที่สะอาดและสอดคล้องกับ Clean Corporate",
    "Colors": "คงโทนสีหลักจากสไลด์เดิม เพิ่ม contrast เฉพาะจุดที่ต้องการเน้น",
    "Typography": "ใช้ sans-serif ที่อ่านง่าย เพิ่มน้ำหนักหัวข้อและจัดขนาดตัวอักษรให้เป็นลำดับ",
    "Design Style": "Clean Corporate แบบเรียบ โปร่ง มี grid ชัด และใช้ accent อย่างประหยัด",
}


class SlideAnalysisError(Exception):
    """Base exception for slide analysis failures."""


class UnsupportedImageError(SlideAnalysisError):
    """Raised when the uploaded file is not a readable image."""


class SlideAnalyzer:
    def __init__(
        self,
        *,
        gemini_api_key: str | None = None,
        gemini_model: str = "gemini-2.5-flash",
        openai_api_key: str | None = None,
        openai_model: str = "gpt-4.1-mini",
        ollama_base_url: str = "http://127.0.0.1:11434",
        ollama_model: str = "qwen2.5vl:3b",
        request_timeout_seconds: int = 120,
    ) -> None:
        self._gemini_client = (
            genai.Client(api_key=gemini_api_key) if gemini_api_key else None
        )
        self._gemini_model = gemini_model
        self._openai_client = OpenAI(api_key=openai_api_key) if openai_api_key else None
        self._openai_model = openai_model
        self._ollama_base_url = ollama_base_url.rstrip("/")
        self._ollama_model = ollama_model
        self._request_timeout_seconds = request_timeout_seconds

    async def analyze_image(
        self,
        image_path: Path,
        provider: str,
        deck_style_guide: str | None = None,
    ) -> str:
        image_bytes, mime_type = await asyncio.to_thread(self._prepare_image, image_path)
        return await asyncio.to_thread(
            self._generate_analysis,
            image_bytes,
            mime_type,
            provider,
            build_analysis_prompt(deck_style_guide),
        )

    async def extract_deck_style(self, image_path: Path, provider: str) -> str:
        image_bytes, mime_type = await asyncio.to_thread(self._prepare_image, image_path)
        return await asyncio.to_thread(
            self._generate_analysis,
            image_bytes,
            mime_type,
            provider,
            DECK_STYLE_EXTRACTION_PROMPT,
        )

    def _prepare_image(self, image_path: Path) -> tuple[bytes, str]:
        try:
            with Image.open(image_path) as image:
                image = ImageOps.exif_transpose(image)
                image = self._convert_to_rgb(image)
                image.thumbnail(
                    (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION),
                    Image.Resampling.LANCZOS,
                )

                buffer = BytesIO()
                image.save(
                    buffer,
                    format="JPEG",
                    quality=JPEG_QUALITY,
                    optimize=True,
                )
                return buffer.getvalue(), "image/jpeg"
        except UnidentifiedImageError as exc:
            raise UnsupportedImageError("The uploaded file is not a valid image.") from exc
        except OSError as exc:
            raise SlideAnalysisError("Could not process the uploaded image.") from exc

    def _generate_analysis(
        self,
        image_bytes: bytes,
        mime_type: str,
        provider: str,
        prompt: str,
    ) -> str:
        if provider == "gemini":
            return self._generate_gemini_analysis(image_bytes, mime_type, prompt)
        if provider == "gpt":
            return self._generate_openai_analysis(image_bytes, mime_type, prompt)
        if provider == "ollama":
            return self._generate_ollama_analysis(image_bytes, prompt)

        raise SlideAnalysisError(f"Unsupported AI provider: {provider}")

    def _generate_gemini_analysis(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
    ) -> str:
        if not self._gemini_client:
            raise SlideAnalysisError("Gemini provider is not configured.")

        image = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

        logger.info("Sending slide image to Gemini model=%s", self._gemini_model)
        try:
            response = self._gemini_client.models.generate_content(
                model=self._gemini_model,
                contents=[prompt, image],
            )
        except Exception as exc:
            raise SlideAnalysisError(_provider_request_error_text("Gemini", exc)) from exc

        text = getattr(response, "text", None)
        if not text:
            raise SlideAnalysisError("Gemini returned an empty response.")

        return text.strip()

    def _generate_ollama_analysis(
        self,
        image_bytes: bytes,
        prompt: str,
    ) -> str:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        text = self._request_ollama_generate(
            prompt=f"{prompt}{OLLAMA_PROMPT_SUFFIX}",
            image_base64=image_base64,
            temperature=0.2,
            purpose="analysis",
        )

        reference_prompt = None
        if _needs_reference_prompt_repair(text):
            logger.info("Repairing generic Ollama reference prompt model=%s", self._ollama_model)
            try:
                reference_prompt = self._generate_ollama_reference_prompt(image_base64)
            except SlideAnalysisError:
                logger.warning("Ollama reference prompt repair failed", exc_info=True)

        return _clean_ollama_response(text, reference_prompt=reference_prompt)

    def _generate_ollama_reference_prompt(self, image_base64: str) -> str:
        prompt = self._request_ollama_generate(
            prompt=OLLAMA_REFERENCE_PROMPT_REPAIR,
            image_base64=image_base64,
            temperature=0.1,
            purpose="reference_prompt",
        )
        prompt = _normalize_reference_prompt(prompt)
        if _is_bad_reference_prompt(prompt):
            return REFERENCE_PROMPT_FALLBACK

        return prompt

    def _request_ollama_generate(
        self,
        *,
        prompt: str,
        image_base64: str,
        temperature: float,
        purpose: str,
    ) -> str:
        payload = {
            "model": self._ollama_model,
            "prompt": prompt,
            "images": [image_base64],
            "stream": False,
            "options": {
                "temperature": temperature,
            },
        }
        request = url_request.Request(
            f"{self._ollama_base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        logger.info(
            "Sending slide image to Ollama model=%s purpose=%s",
            self._ollama_model,
            purpose,
        )
        try:
            with url_request.urlopen(
                request,
                timeout=self._request_timeout_seconds,
            ) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise SlideAnalysisError(_ollama_request_error_text(exc)) from exc

        text = response_data.get("response")
        if not text:
            raise SlideAnalysisError("Ollama returned an empty response.")

        return text.strip()

    def _generate_openai_analysis(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
    ) -> str:
        if not self._openai_client:
            raise SlideAnalysisError("GPT provider is not configured.")

        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        image_data_url = f"data:{mime_type};base64,{image_base64}"

        logger.info("Sending slide image to OpenAI model=%s", self._openai_model)
        try:
            response = self._openai_client.responses.create(
                model=self._openai_model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {
                                "type": "input_image",
                                "image_url": image_data_url,
                                "detail": "high",
                            },
                        ],
                    }
                ],
            )
        except Exception as exc:
            raise SlideAnalysisError(_provider_request_error_text("GPT", exc)) from exc

        text = getattr(response, "output_text", None)
        if not text:
            raise SlideAnalysisError("GPT returned an empty response.")

        return text.strip()

    @staticmethod
    def _convert_to_rgb(image: Image.Image) -> Image.Image:
        if image.mode == "RGBA":
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel("A"))
            return background

        if image.mode in {"LA", "P"}:
            rgba_image = image.convert("RGBA")
            background = Image.new("RGB", rgba_image.size, (255, 255, 255))
            background.paste(rgba_image, mask=rgba_image.getchannel("A"))
            return background

        return image.convert("RGB")


def _clean_ollama_response(text: str, reference_prompt: str | None = None) -> str:
    text = _remove_cjk_lines(text)
    text = _fill_blank_fields(text)
    return _clean_ollama_prompt_section(text, reference_prompt)


def _remove_cjk_lines(text: str) -> str:
    cleaned_lines = []
    for line in text.splitlines():
        if not CJK_PATTERN.search(line):
            cleaned_lines.append(line)
            continue

        stripped = line.strip()
        if stripped.startswith(("1.", "2.", "3.")):
            prefix = stripped[:2]
            cleaned_lines.append(
                f"{prefix} ปรับข้อความสำคัญให้โดดเด่นขึ้นด้วยน้ำหนักตัวอักษร สี หรือพื้นที่ว่าง"
            )
        elif stripped.startswith("*"):
            cleaned_lines.append("* ปรับรายละเอียดนี้ให้ชัดเจนขึ้นโดยยึดจากภาพต้นแบบ")
        else:
            cleaned_lines.append("ปรับรายละเอียดนี้ให้ชัดเจนขึ้นโดยยึดจากภาพต้นแบบ")

    return "\n".join(cleaned_lines)


def _fill_blank_fields(text: str) -> str:
    for field, default in BLANK_FIELD_DEFAULTS.items():
        pattern = re.compile(rf"(\* {re.escape(field)}:)[ \t]*$", re.MULTILINE)
        text = pattern.sub(rf"\1 {default}", text)
    return text


def _needs_reference_prompt_repair(text: str) -> bool:
    if PROMPT_SECTION_TITLE not in text:
        return True

    _, prompt = text.rsplit(PROMPT_SECTION_TITLE, 1)
    prompt = _normalize_reference_prompt(prompt)
    return _is_bad_reference_prompt(prompt)


def _clean_ollama_prompt_section(
    text: str,
    reference_prompt: str | None = None,
) -> str:
    replacement_prompt = (
        _ensure_reference_edit_constraints(_normalize_reference_prompt(reference_prompt))
        if reference_prompt
        else REFERENCE_PROMPT_FALLBACK
    )

    if PROMPT_SECTION_TITLE not in text:
        return f"{text.rstrip()}\n\n{PROMPT_SECTION_TITLE}\n{replacement_prompt}"

    body, prompt = text.rsplit(PROMPT_SECTION_TITLE, 1)
    prompt = _normalize_reference_prompt(prompt)

    if _is_bad_reference_prompt(prompt):
        prompt = replacement_prompt
    else:
        prompt = _ensure_reference_edit_constraints(prompt)

    return f"{body.rstrip()}\n\n{PROMPT_SECTION_TITLE}\n{prompt.strip()}"


def _normalize_reference_prompt(prompt: str | None) -> str:
    if not prompt:
        return REFERENCE_PROMPT_FALLBACK

    prompt = _remove_instruction_leak(prompt).strip()
    prompt = _remove_prompt_diagnostic_sentences(prompt)
    prompt = re.sub(r"\s+", " ", prompt).strip().strip('"').strip()

    if not prompt:
        return REFERENCE_PROMPT_FALLBACK

    if not prompt.startswith(REFERENCE_PROMPT_PREFIX):
        prompt = f"{REFERENCE_PROMPT_PREFIX} {prompt}"

    return prompt


def _ensure_reference_edit_constraints(prompt: str) -> str:
    lower_prompt = prompt.lower()
    if (
        "preserve every readable text" in lower_prompt
        and "improve only professional polish" in lower_prompt
    ):
        return prompt

    return f"{prompt.rstrip().rstrip('.')}.{REFERENCE_EDIT_CONSTRAINTS}"


def _remove_prompt_diagnostic_sentences(prompt: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", prompt.strip())
    cleaned_sentences = [
        sentence
        for sentence in sentences
        if not any(phrase in sentence.lower() for phrase in PROMPT_DIAGNOSTIC_PHRASES)
    ]

    return " ".join(cleaned_sentences).strip() or prompt


def _remove_instruction_leak(prompt: str) -> str:
    for marker in INSTRUCTION_LEAK_MARKERS:
        marker_index = prompt.find(marker)
        if marker_index != -1:
            prompt = prompt[:marker_index]

    return prompt.strip().strip('"').strip()


def _is_bad_reference_prompt(prompt: str) -> bool:
    if len(prompt) < 180:
        return True

    if any(marker in prompt for marker in INSTRUCTION_LEAK_MARKERS):
        return True

    if "placeholder" in prompt.lower() or "do not copy instructions" in prompt.lower():
        return True

    if _looks_generic_reference_prompt(prompt):
        return True

    return False


def _looks_generic_reference_prompt(prompt: str) -> bool:
    lower_prompt = prompt.lower()
    generic_markers = sum(
        phrase in lower_prompt
        for phrase in (
            "preserve all readable text",
            "same 16:9 composition",
            "layout structure",
            "content hierarchy",
            "color palette",
            "background treatment",
            "visual style from the reference",
            "improve only professional polish",
        )
    )
    concrete_markers = len(CONCRETE_REFERENCE_PATTERN.findall(prompt))

    if THAI_PATTERN.search(prompt):
        concrete_markers += 2

    return generic_markers >= 4 and concrete_markers < 4


def _provider_request_error_text(provider_label: str, exc: Exception) -> str:
    status_code = getattr(exc, "status_code", None)
    message = str(exc).lower()

    if status_code == 503 or "high demand" in message or "unavailable" in message:
        return (
            f"{provider_label} กำลังมีผู้ใช้งานสูงชั่วคราวครับ "
            "กรุณารอสักครู่แล้วลองใหม่ หรือใช้ /provider เปลี่ยนไปใช้อีก provider"
        )

    if status_code == 429 or any(
        text in message
        for text in (
            "too many requests",
            "resource_exhausted",
            "quota",
            "rate limit",
        )
    ):
        return (
            f"{provider_label} quota/rate limit เต็มครับ "
            "กรุณารอสักครู่ ตรวจ billing/quota หรือใช้ /provider เปลี่ยนไปใช้อีก provider"
        )

    if status_code in {401, 403} or any(
        text in message
        for text in (
            "api key",
            "permission",
            "unauthorized",
            "forbidden",
        )
    ):
        return (
            f"{provider_label} API key ใช้งานไม่ได้หรือยังไม่มีสิทธิ์เรียก model นี้ครับ "
            "กรุณาตรวจค่า API key ใน .env แล้วรัน bot ใหม่"
        )

    return f"{provider_label} request failed."


def _ollama_request_error_text(exc: Exception) -> str:
    status_code = getattr(exc, "code", None)

    if isinstance(exc, url_error.HTTPError):
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
    elif isinstance(exc, url_error.URLError):
        detail = str(getattr(exc, "reason", exc))
    else:
        detail = str(exc)

    message = detail.lower()
    if "connection refused" in message or "nodename" in message:
        return (
            "Ollama ยังไม่ได้เปิดหรือเชื่อมต่อไม่ได้ครับ "
            "เปิด Ollama app แล้วลองใหม่ หรือรัน `open -a Ollama --args hidden`"
        )

    if "model" in message and ("not found" in message or "pull" in message):
        return (
            "ยังไม่มี Ollama model ที่เลือกครับ "
            "รัน `ollama pull qwen2.5vl:3b` แล้วลองใหม่"
        )

    if "llama-server binary not found" in message:
        return (
            "Ollama ติดตั้งไม่ครบครับ ให้ใช้ `brew install ollama-app` "
            "หรือดาวน์โหลด Ollama.app จากเว็บทางการ แล้วเปิด app ใหม่"
        )

    if status_code:
        return f"Ollama request failed with HTTP {status_code}: {detail}"

    return f"Ollama request failed: {detail}"
