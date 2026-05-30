from __future__ import annotations

import asyncio
import base64
import logging
from io import BytesIO
from pathlib import Path

from google import genai
from google.genai import types
from openai import OpenAI
from PIL import Image, ImageOps, UnidentifiedImageError

from prompts import DECK_STYLE_EXTRACTION_PROMPT, build_analysis_prompt


logger = logging.getLogger(__name__)

MAX_IMAGE_DIMENSION = 2400
JPEG_QUALITY = 92


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
    ) -> None:
        self._gemini_client = (
            genai.Client(api_key=gemini_api_key) if gemini_api_key else None
        )
        self._gemini_model = gemini_model
        self._openai_client = OpenAI(api_key=openai_api_key) if openai_api_key else None
        self._openai_model = openai_model

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
            raise SlideAnalysisError("Gemini request failed.") from exc

        text = getattr(response, "text", None)
        if not text:
            raise SlideAnalysisError("Gemini returned an empty response.")

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
            raise SlideAnalysisError("OpenAI request failed.") from exc

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
