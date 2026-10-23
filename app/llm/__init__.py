from functools import lru_cache

from app import config
from app.llm.base import LLM, LLMError
from app.llm.mock import MockLLM
from app.llm.providers import GeminiLLM, OpenAICompatLLM

__all__ = ["LLM", "LLMError", "get_llm"]


@lru_cache(maxsize=1)
def get_llm() -> LLM:
    """Chosen by LLM_PROVIDER. Also used as a FastAPI dependency, so tests can override it."""
    p = config.LLM_PROVIDER.lower()
    if p == "mock":
        return MockLLM()
    if p == "gemini":
        return GeminiLLM(config.LLM_API_KEY, config.LLM_MODEL or "gemini-2.5-flash")
    if p == "groq":
        return OpenAICompatLLM(config.LLM_BASE_URL or "https://api.groq.com/openai/v1",
                               config.LLM_MODEL or "llama-3.3-70b-versatile", config.LLM_API_KEY)
    if p == "ollama":
        return OpenAICompatLLM(config.LLM_BASE_URL or "http://localhost:11434/v1",
                               config.LLM_MODEL or "llama3.1", config.LLM_API_KEY)
    raise RuntimeError(f"unknown LLM_PROVIDER {config.LLM_PROVIDER!r} (use mock, gemini, groq or ollama)")
