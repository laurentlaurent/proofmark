"""Configuration: model provider presets and environment variables.

Every provider except the offline demo speaks the OpenAI-compatible Chat
Completions API, so one small HTTP client covers Gemini, Groq, Ollama and any
other compatible endpoint. No vendor SDK is needed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parent
STATIC_DIR = PACKAGE_DIR / "static"

load_dotenv(ROOT_DIR / ".env")

SAMPLES_DIR = Path(os.getenv("PROOFMARK_SAMPLES_DIR", str(ROOT_DIR / "samples")))
KB_DIR = Path(os.getenv("PROOFMARK_KB_DIR", str(ROOT_DIR / "knowledge_base")))
HOST = os.getenv("PROOFMARK_HOST", "127.0.0.1")
PORT = int(os.getenv("PROOFMARK_PORT", "8000"))
TIMEOUT = float(os.getenv("PROOFMARK_TIMEOUT", "120"))
AUDIT_DIR = Path(os.getenv("PROOFMARK_AUDIT_DIR", str(ROOT_DIR / "audit")))
AUDIT_ENABLED = os.getenv("PROOFMARK_AUDIT", "on").strip().lower() not in {"off", "0", "false", "no"}
EVALS_DIR = Path(os.getenv("PROOFMARK_EVALS_DIR", str(ROOT_DIR / "evals")))


class ConfigError(ValueError):
    """The chosen provider cannot be used as configured."""


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    base_url: str
    model: str
    key_env: str
    needs_key: bool
    vision: bool
    help: str
    key_url: str = ""


PRESETS: dict[str, Preset] = {
    "demo": Preset(
        "demo", "Demo mode (offline, no AI)", "", "rules", "", False, False,
        "Rule-based extraction and templates. No key needed: use it to try the workflow.",
    ),
    "gemini": Preset(
        "gemini", "Google Gemini (free tier)",
        "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-flash-latest",
        "GEMINI_API_KEY", True, True,
        "Free API key from Google AI Studio. Reads screenshots.",
        "https://aistudio.google.com/apikey",
    ),
    "groq": Preset(
        "groq", "Groq (free tier)", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b",
        "GROQ_API_KEY", True, False,
        "Free API key from the Groq console. Very fast, text only.",
        "https://console.groq.com/keys",
    ),
    "ollama": Preset(
        "ollama", "Ollama (runs on your machine)", "http://localhost:11434/v1", "llama3.1",
        "", False, False,
        "Free and private. Install Ollama, then run `ollama pull llama3.1`. "
        "For screenshots, use a vision model such as gemma3 and tick 'Model reads images'.",
        "https://ollama.com/download",
    ),
    "custom": Preset(
        "custom", "Other OpenAI-compatible API", "", "", "LLM_API_KEY", False, False,
        "Any Chat Completions endpoint, such as OpenRouter, LM Studio, Mistral or OpenAI. "
        "Set the base URL and model.",
    ),
}


def _env_flag(name: str) -> bool | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_base_url(preset: Preset) -> str:
    if preset.id == "ollama":
        return os.getenv("OLLAMA_BASE_URL", preset.base_url)
    if preset.id == "custom":
        return os.getenv("LLM_BASE_URL", "")
    return preset.base_url


def env_model(preset: Preset) -> str:
    if preset.id == "custom":
        return os.getenv("LLM_MODEL", "")
    return os.getenv(f"{preset.id.upper()}_MODEL", preset.model)


def env_key(preset: Preset) -> str:
    return os.getenv(preset.key_env, "") if preset.key_env else ""


def default_provider() -> str:
    explicit = os.getenv("PROOFMARK_PROVIDER", "").strip().lower()
    if explicit in PRESETS:
        return explicit
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    if os.getenv("GROQ_API_KEY"):
        return "groq"
    if os.getenv("LLM_BASE_URL"):
        return "custom"
    return "demo"


@dataclass
class LLMSettings:
    provider: str
    base_url: str
    model: str
    api_key: str
    vision: bool
    timeout: float = TIMEOUT

    @property
    def is_demo(self) -> bool:
        return self.provider == "demo"

    @property
    def label(self) -> str:
        return PRESETS[self.provider].label

    def ensure_ready(self) -> None:
        """Fail early, with a fix, when the provider is not usable."""
        preset = PRESETS[self.provider]
        if self.is_demo:
            return
        if not self.base_url:
            raise ConfigError("Set the API base URL for this provider (LLM_BASE_URL in .env, or in Model settings).")
        if not self.model:
            raise ConfigError("Set a model name (LLM_MODEL in .env, or in Model settings).")
        if preset.needs_key and not self.api_key:
            raise ConfigError(
                f"{preset.label} needs an API key. Add {preset.key_env} to .env or paste a key in Model settings"
                + (f" (free key: {preset.key_url})." if preset.key_url else ".")
            )


def resolve(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    vision: bool | None = None,
) -> LLMSettings:
    """Merge UI/CLI choices over environment defaults."""
    pid = (provider or default_provider()).strip().lower()
    if pid not in PRESETS:
        raise ConfigError(f"Unknown provider '{pid}'. Choose one of: {', '.join(PRESETS)}.")
    preset = PRESETS[pid]
    env_vision = _env_flag("PROOFMARK_VISION")
    if vision is None:
        vision = preset.vision if env_vision is None else env_vision
    return LLMSettings(
        provider=pid,
        base_url=(base_url or env_base_url(preset) or "").rstrip("/"),
        model=(model or env_model(preset) or "").strip(),
        api_key=(api_key or env_key(preset) or "").strip(),
        vision=bool(vision),
    )


def public_config() -> dict:
    """What the UI may know about providers. Never includes secrets."""
    providers = []
    for preset in PRESETS.values():
        providers.append({
            "id": preset.id,
            "label": preset.label,
            "model": env_model(preset),
            "base_url": env_base_url(preset),
            "vision": preset.vision,
            "needs_key": preset.needs_key,
            "key_env": preset.key_env,
            "has_env_key": bool(env_key(preset)),
            "help": preset.help,
            "key_url": preset.key_url,
        })
    return {"default_provider": default_provider(), "providers": providers}
