"""Pick and build the LLM backend (Claude or Gemini) from the environment."""

from __future__ import annotations

import os

from .config import LLMConfig, gemini_api_key, llm_provider
from .contracts import LLM

ANTHROPIC_HINT = (
    "No Anthropic API key is set. Add ANTHROPIC_API_KEY=... to the project's .env, or add "
    "GEMINI_API_KEY=... to use Gemini's free tier (https://aistudio.google.com/apikey)."
)
GEMINI_HINT = (
    "No Gemini API key is set. Add GEMINI_API_KEY=... to the project's .env "
    "(free key: https://aistudio.google.com/apikey)."
)


def backend_config() -> tuple[str, LLMConfig]:
    """(provider, LLMConfig) for the configured backend. Raises ValueError on a bad
    ARCH_PROVIDER."""
    provider = llm_provider()
    if provider == "gemini":
        from .gemini import gemini_config

        return provider, gemini_config()
    return provider, LLMConfig()


def llm_ready() -> tuple[bool, str | None]:
    """Whether the configured backend has an API key, and how to fix it if not."""
    provider = llm_provider()
    if provider == "gemini":
        return (True, None) if gemini_api_key() else (False, GEMINI_HINT)
    return (True, None) if os.getenv("ANTHROPIC_API_KEY") else (False, ANTHROPIC_HINT)


def default_llm() -> LLM:
    """ClaudeLLM or GeminiLLM, per `llm_provider()`."""
    provider, config = backend_config()
    if provider == "gemini":
        from .gemini import GeminiLLM

        return GeminiLLM(config)
    from .llm import ClaudeLLM

    return ClaudeLLM(config)
