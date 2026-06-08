from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


SUPPORTED_AI_PROVIDERS = ("gemini", "gpt", "ollama")
AI_PROVIDER_LABELS = {
    "gemini": "Gemini (ฟรี/โควตาฟรี)",
    "gpt": "GPT (เสียเงิน)",
    "ollama": "Ollama Local (ฟรี/รันในเครื่อง)",
}


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    ai_provider: str
    gemini_api_key: str | None
    gemini_model: str
    gemini_image_model: str
    openai_api_key: str | None
    openai_model: str
    openai_image_model: str
    ollama_base_url: str
    ollama_model: str
    log_level: str
    max_image_size_mb: int
    max_pdf_size_mb: int
    request_timeout_seconds: int

    @property
    def max_image_size_bytes(self) -> int:
        return self.max_image_size_mb * 1024 * 1024

    @property
    def max_pdf_size_bytes(self) -> int:
        return self.max_pdf_size_mb * 1024 * 1024

    def is_provider_available(self, provider: str) -> bool:
        if provider == "gemini":
            return bool(self.gemini_api_key)
        if provider == "gpt":
            return bool(self.openai_api_key)
        if provider == "ollama":
            return True
        return False

    def provider_label(self, provider: str) -> str:
        return AI_PROVIDER_LABELS.get(provider, provider)

    def provider_model(self, provider: str) -> str:
        if provider == "gemini":
            return self.gemini_model
        if provider == "gpt":
            return self.openai_model
        if provider == "ollama":
            return self.ollama_model
        return "unknown"

    def provider_image_model(self, provider: str) -> str:
        if provider == "gemini":
            return self.gemini_image_model
        if provider == "gpt":
            return self.openai_image_model
        if provider == "ollama":
            return "ไม่รองรับ image generation"
        return "unknown"


def get_settings() -> Settings:
    gemini_api_key = _optional_env("GEMINI_API_KEY")
    openai_api_key = _optional_env("OPENAI_API_KEY")
    ai_provider = _default_ai_provider(gemini_api_key, openai_api_key)

    return Settings(
        telegram_bot_token=_required_env("TELEGRAM_BOT_TOKEN"),
        ai_provider=ai_provider,
        gemini_api_key=gemini_api_key,
        gemini_model=_optional_env("GEMINI_MODEL") or "gemini-2.5-flash",
        gemini_image_model=(
            _optional_env("GEMINI_IMAGE_MODEL") or "gemini-2.5-flash-image"
        ),
        openai_api_key=openai_api_key,
        openai_model=_optional_env("OPENAI_MODEL") or "gpt-4.1-mini",
        openai_image_model=_optional_env("OPENAI_IMAGE_MODEL") or "gpt-image-2",
        ollama_base_url=_optional_env("OLLAMA_BASE_URL") or "http://127.0.0.1:11434",
        ollama_model=_optional_env("OLLAMA_MODEL") or "qwen2.5vl:3b",
        log_level=(_optional_env("LOG_LEVEL") or "INFO").upper(),
        max_image_size_mb=_positive_int_env("MAX_IMAGE_SIZE_MB", 15),
        max_pdf_size_mb=_positive_int_env("MAX_PDF_SIZE_MB", 25),
        request_timeout_seconds=_positive_int_env("REQUEST_TIMEOUT_SECONDS", 120),
    )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if not value or not value.strip():
        return None
    return value.strip()


def _default_ai_provider(gemini_api_key: str | None, openai_api_key: str | None) -> str:
    raw_provider = _optional_env("AI_PROVIDER")
    if raw_provider:
        provider = _normalize_ai_provider(raw_provider)
    elif gemini_api_key:
        provider = "gemini"
    elif openai_api_key:
        provider = "gpt"
    else:
        provider = "ollama"

    if provider == "gemini" and not gemini_api_key:
        raise RuntimeError("AI_PROVIDER=gemini requires GEMINI_API_KEY")
    if provider == "gpt" and not openai_api_key:
        raise RuntimeError("AI_PROVIDER=gpt requires OPENAI_API_KEY")

    return provider


def _normalize_ai_provider(value: str) -> str:
    aliases = {
        "gemini": "gemini",
        "google": "gemini",
        "gpt": "gpt",
        "openai": "gpt",
        "chatgpt": "gpt",
        "ollama": "ollama",
        "local": "ollama",
        "qwen": "ollama",
    }
    provider = aliases.get(value.strip().lower())
    if not provider:
        supported = ", ".join(SUPPORTED_AI_PROVIDERS)
        raise RuntimeError(f"AI_PROVIDER must be one of: {supported}")
    return provider


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc

    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value
