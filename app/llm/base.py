"""The one small interface every LLM provider implements. The LLM only ever returns TEXT that
the caller parses as JSON and validates; it never writes SQL and never decides a money outcome."""
from typing import Protocol


class LLMError(Exception):
    """The provider was unreachable or returned something unusable."""


class LLM(Protocol):
    def complete_json(self, system: str, user: str) -> str:
        """Return the model's reply, which the caller expects to be a JSON document."""
        ...
