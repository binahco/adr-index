from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Citation(BaseModel):
    file: str
    section: str
    line_start: int


class AnswerCard(BaseModel):
    """Respuesta con fuentes verificables (`answer-card-v1`)."""

    question: str
    answer: str
    citations: list[Citation] = []
    confidence: Literal["alta", "media", "baja"] = "media"
