from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, StrictStr

from app.auth import current_user_id
from app.errors import AppError
from app.llm import LLM, LLMError, get_llm
from app.services import statement_qa
from app.services.idempotency import idempotency_key

router = APIRouter()


class AskIn(BaseModel):
    question: StrictStr = Field(min_length=1, max_length=500)


@router.post("/users/{user_id}/statement/ask")
def ask(user_id: int, body: AskIn, caller: Annotated[int, Depends(current_user_id)],
        _key: Annotated[str, Depends(idempotency_key)], llm: Annotated[LLM, Depends(get_llm)]):
    """The Idempotency-Key header is required like on every POST, but nothing is stored: the
    endpoint only reads, so a retry is already harmless, and storing a response would hold a
    database transaction open across a slow external LLM call."""
    if caller != user_id:
        raise AppError(403, "FORBIDDEN", "you can only ask about your own statement")
    try:
        return statement_qa.ask(llm, user_id, body.question)
    except LLMError:
        raise AppError(502, "LLM_UNAVAILABLE", "the language model is unavailable; try again shortly")
