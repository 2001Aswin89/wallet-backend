"""Real providers behind the same interface. Plain HTTP via httpx, no vendor SDK.

    LLM_PROVIDER=gemini   Google Gemini (free tier)         LLM_API_KEY, optional LLM_MODEL
    LLM_PROVIDER=groq     Groq (OpenAI-compatible)          LLM_API_KEY, optional LLM_MODEL
    LLM_PROVIDER=ollama   local Ollama (OpenAI-compatible)  optional LLM_MODEL, LLM_BASE_URL

NOTE: these were written against the providers' public docs but are NOT exercised by the test
suite (tests use the mock; no API key was available while building). Try one with a real key
before relying on it.
"""
import httpx

from app.llm.base import LLMError

TIMEOUT = httpx.Timeout(30.0)


class GeminiLLM:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self._key, self._model = api_key, model

    def complete_json(self, system: str, user: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        }
        try:
            r = httpx.post(url, json=payload, headers={"x-goog-api-key": self._key}, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            raise LLMError(f"gemini request failed: {type(e).__name__}") from e


class OpenAICompatLLM:
    """Groq and Ollama both speak the OpenAI chat-completions dialect."""

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self._url, self._model, self._key = base_url.rstrip("/") + "/chat/completions", model, api_key

    def complete_json(self, system: str, user: str) -> str:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        payload = {"model": self._model, "temperature": 0, "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            r = httpx.post(self._url, json=payload, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            raise LLMError(f"llm request failed: {type(e).__name__}") from e
