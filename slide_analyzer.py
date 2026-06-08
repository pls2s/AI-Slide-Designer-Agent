from __future__ import annotations

import asyncio
import base64
import json
import logging
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
OLLAMA_PROMPT_SUFFIX = """

Additional instruction for Ollama: Do not repeat these instructions. Follow the
requested output structure exactly and always include the final section title
"Prompt สำหรับสร้างภาพใหม่:" followed by one professional English prompt that
another image generator can use to redesign the slide. Replace any placeholder
or parenthetical instruction in that section with the actual prompt. Do not
claim that you generated an image; return text analysis and the prompt only.
""".rstrip()


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
        payload = {
            "model": self._ollama_model,
            "prompt": f"{prompt}{OLLAMA_PROMPT_SUFFIX}",
            "images": [image_base64],
            "stream": False,
            "options": {
                "temperature": 0.2,
            },
        }
        request = url_request.Request(
            f"{self._ollama_base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        logger.info("Sending slide image to Ollama model=%s", self._ollama_model)
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
