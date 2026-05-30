from __future__ import annotations

import asyncio
import base64
import logging
from io import BytesIO
from pathlib import Path
from typing import Iterable

from google import genai
from openai import OpenAI
from PIL import Image, ImageOps, UnidentifiedImageError

from prompts import build_slide_decoration_prompt


logger = logging.getLogger(__name__)

REFERENCE_MAX_IMAGE_DIMENSION = 2048
REFERENCE_PNG_FORMAT = "PNG"
OPENAI_SLIDE_SIZE = "2048x1152"
OPENAI_IMAGE_QUALITY = "high"


class SlideImageGenerationError(Exception):
    """Raised when an AI provider cannot generate a decorated slide image."""


class SlideImageGenerator:
    def __init__(
        self,
        *,
        gemini_api_key: str | None = None,
        gemini_image_model: str = "gemini-2.5-flash-image",
        openai_api_key: str | None = None,
        openai_image_model: str = "gpt-image-2",
    ) -> None:
        self._gemini_client = (
            genai.Client(api_key=gemini_api_key) if gemini_api_key else None
        )
        self._gemini_image_model = gemini_image_model
        self._openai_client = OpenAI(api_key=openai_api_key) if openai_api_key else None
        self._openai_image_model = openai_image_model

    async def generate_decorated_slide(
        self,
        reference_image_path: Path,
        provider: str,
        output_path: Path,
        deck_style_guide: str | None = None,
    ) -> Path:
        return await asyncio.to_thread(
            self._generate_decorated_slide,
            reference_image_path,
            provider,
            output_path,
            deck_style_guide,
        )

    def _generate_decorated_slide(
        self,
        reference_image_path: Path,
        provider: str,
        output_path: Path,
        deck_style_guide: str | None,
    ) -> Path:
        prompt = build_slide_decoration_prompt(deck_style_guide)
        if provider == "gemini":
            return self._generate_gemini_image(reference_image_path, output_path, prompt)
        if provider == "gpt":
            return self._generate_openai_image(reference_image_path, output_path, prompt)

        raise SlideImageGenerationError(f"Unsupported AI provider: {provider}")

    def _generate_gemini_image(
        self,
        reference_image_path: Path,
        output_path: Path,
        prompt: str,
    ) -> Path:
        if not self._gemini_client:
            raise SlideImageGenerationError("Gemini image provider is not configured.")

        reference_image = self._load_reference_image(reference_image_path)

        logger.info(
            "Generating decorated slide with Gemini image model=%s",
            self._gemini_image_model,
        )
        try:
            response = self._gemini_client.models.generate_content(
                model=self._gemini_image_model,
                contents=[prompt, reference_image],
            )
        except Exception as exc:
            raise SlideImageGenerationError("Gemini image generation failed.") from exc

        for part in _iter_gemini_parts(response):
            if not _part_has_inline_image(part):
                continue

            image = _part_to_image(part)
            image.save(output_path, format=REFERENCE_PNG_FORMAT)
            return output_path

        raise SlideImageGenerationError("Gemini did not return an image.")

    def _generate_openai_image(
        self,
        reference_image_path: Path,
        output_path: Path,
        prompt: str,
    ) -> Path:
        if not self._openai_client:
            raise SlideImageGenerationError("GPT image provider is not configured.")

        logger.info(
            "Generating decorated slide with OpenAI image model=%s",
            self._openai_image_model,
        )
        try:
            with reference_image_path.open("rb") as image_file:
                result = self._openai_client.images.edit(
                    model=self._openai_image_model,
                    image=[image_file],
                    prompt=prompt,
                    size=OPENAI_SLIDE_SIZE,
                    quality=OPENAI_IMAGE_QUALITY,
                )
        except Exception as exc:
            raise SlideImageGenerationError("OpenAI image generation failed.") from exc

        image_base64 = result.data[0].b64_json if result.data else None
        if not image_base64:
            raise SlideImageGenerationError("OpenAI did not return an image.")

        output_path.write_bytes(base64.b64decode(image_base64))
        return output_path

    def _load_reference_image(self, image_path: Path) -> Image.Image:
        try:
            with Image.open(image_path) as image:
                image = ImageOps.exif_transpose(image)
                image = _convert_to_rgb(image)
                image.thumbnail(
                    (REFERENCE_MAX_IMAGE_DIMENSION, REFERENCE_MAX_IMAGE_DIMENSION),
                    Image.Resampling.LANCZOS,
                )
                return image.copy()
        except UnidentifiedImageError as exc:
            raise SlideImageGenerationError(
                "The rendered PDF page is not a valid image."
            ) from exc
        except OSError as exc:
            raise SlideImageGenerationError(
                "Could not prepare the rendered PDF page."
            ) from exc


def _iter_gemini_parts(response: object) -> Iterable[object]:
    parts = getattr(response, "parts", None)
    if parts is not None:
        return parts

    candidates = getattr(response, "candidates", None) or []
    candidate_parts = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        candidate_parts.extend(getattr(content, "parts", None) or [])
    return candidate_parts


def _part_has_inline_image(part: object) -> bool:
    return getattr(part, "inline_data", None) is not None


def _part_to_image(part: object) -> Image.Image:
    as_image = getattr(part, "as_image", None)
    if callable(as_image):
        image = as_image()
        if image is not None:
            return image.copy()

    inline_data = getattr(part, "inline_data", None)
    data = getattr(inline_data, "data", None)
    if isinstance(data, str):
        data = base64.b64decode(data)
    if not data:
        raise SlideImageGenerationError("Gemini image part does not contain data.")

    with Image.open(BytesIO(data)) as image:
        return image.copy()


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
