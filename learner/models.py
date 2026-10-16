"""Shared data types: rows read from the database, and the JSON import/export schema."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .checkers import FORMATS

ANSWER_FORMATS = tuple(FORMATS)  # the registry in checkers.py is the single source
OUTCOMES = ("correct", "assisted", "incorrect")
# sqlglot dialect names (used for syntax checking in Phase 1).
SQL_DIALECTS = ("mysql", "postgres", "sqlite", "snowflake", "bigquery", "tsql", "duckdb")


@dataclass(frozen=True)
class Reference:
    title: str
    url: str


@dataclass(frozen=True)
class Collection:
    id: int
    name: str
    tier_labels: dict[int, str]
    sql_dialect: str
    max_unlocked_tier: int
    archived: bool

    def tier_label(self, tier: int) -> str:
        label = self.tier_labels.get(tier)
        return f"Tier {tier} · {label}" if label else f"Tier {tier}"


@dataclass(frozen=True)
class Card:
    id: int
    collection_id: int
    tier: int
    answer_format: str
    prompt: str
    reference_answer: str
    rubric: list[str]
    references: list[Reference]
    tags: list[str]
    archived: bool
    progress_reset_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Analysis:
    """What a card teaches and needs, for ordering (DESIGN.md §6). `manual` = set by you, kept as is."""

    card_id: int
    introduced: list[str]
    required: list[str]
    difficulty: int
    manual: bool
    analyzed_at: str


@dataclass(frozen=True)
class Attempt:
    id: int
    card_id: int
    session_id: str
    ts: str
    outcome: str


@dataclass(frozen=True)
class AttemptRecord:
    """An attempt with the details stats and the grader test set need."""

    id: int
    card_id: int
    ts: str
    outcome: str
    answer: str | None
    max_hint_level: int
    llm_verdict: str | None
    llm_feedback: str | None
    overridden: bool
    duration_ms: int | None


# --- JSON import/export schema (also used to validate the card and collection forms) ---


class ReferenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1)
    url: str

    @field_validator("url")
    @classmethod
    def _http_url(cls, url: str) -> str:
        if not url.startswith(("http://", "https://")) or " " in url:
            raise ValueError("must be a web address starting with http:// or https://")
        return url


class CardIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: int | None = None
    tier: int = Field(ge=1)
    answer_format: Literal[ANSWER_FORMATS] = "text"
    prompt: str = Field(min_length=1)
    reference_answer: str = Field(min_length=1)
    rubric: list[str] = []
    references: list[ReferenceIn] = []
    tags: list[str] = []
    archived: bool = False

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_fields(cls, data):
        # Exports from before DECISIONS.md #51 carry a per-card study mode; it no longer exists.
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k != "default_mode"}
        return data


class CollectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    collection: str = Field(min_length=1)
    sql_dialect: Literal[SQL_DIALECTS] = "mysql"
    tier_labels: dict[int, str] = {}
    cards: list[CardIn] = []


def describe_errors(exc: ValidationError) -> list[str]:
    """Turn pydantic errors into readable lines, e.g. 'Card 3, references 1, url: must be a web address…'."""
    lines = []
    for error in exc.errors():
        loc = list(error["loc"])
        parts = []
        if loc[:1] == ["cards"] and len(loc) > 1 and isinstance(loc[1], int):
            parts.append(f"Card {loc[1] + 1}")
            loc = loc[2:]
        if loc:
            parts.append(" ".join(str(x + 1) if isinstance(x, int) else str(x) for x in loc))
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"{', '.join(parts)}: {message}" if parts else message)
    return lines
