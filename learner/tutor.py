"""The LLM tutor: grading and hints. Prompts live in prompts/*.md (DESIGN.md §7.3).

Each prompt file holds the system prompt, then a `=== USER MESSAGE ===` line, then the user message
template with $placeholders. Files are read on every call, so editing a prompt takes effect
without restarting.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape, unescape
from pathlib import Path
from string import Template

from . import checkers
from .llm import ImageInput, LLMError, LLMProvider
from .models import ANSWER_FORMATS, Analysis, Card, CardIn, Collection
from .sequencer import concept_list

PROMPTS = Path(__file__).parent / "prompts"
SEPARATOR = "\n=== USER MESSAGE ===\n"
VERDICTS = ("correct", "partially_correct", "incorrect")
MAX_HINT_LEVEL = 3

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "feedback": {"type": "string"},
        "rubric_hits": {"type": "array", "items": {"type": "string"}},
        "rubric_misses": {"type": "array", "items": {"type": "string"}},
        "equivalent_approach": {"type": "boolean"},
    },
    "required": ["verdict", "feedback", "rubric_hits", "rubric_misses", "equivalent_approach"],
    "additionalProperties": False,
}

HINT_SCHEMA = {
    "type": "object",
    "properties": {"hint": {"type": "string"}},
    "required": ["hint"],
    "additionalProperties": False,
}


SEQUENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "integer"},
                    "concepts_introduced": {"type": "array", "items": {"type": "string"}},
                    "concepts_required": {"type": "array", "items": {"type": "string"}},
                    "difficulty": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                },
                "required": ["card_id", "concepts_introduced", "concepts_required", "difficulty"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Grade:
    verdict: str
    feedback: str
    rubric_hits: list[str]
    rubric_misses: list[str]
    equivalent_approach: bool

    @property
    def right(self) -> bool:
        """Only a full `correct` counts as right; partially correct counts as wrong (DESIGN.md §5.2)."""
        return self.verdict == "correct"


def render(name: str, **values: object) -> tuple[str, str]:
    """(system prompt, user message) for prompts/<name>.md with the placeholders filled in."""
    text = (PROMPTS / f"{name}.md").read_text()
    system, _, user = text.partition(SEPARATOR)
    return system.strip(), Template(user.strip()).safe_substitute({k: str(v) for k, v in values.items()})


def _format_label(card: Card, dialect: str) -> str:
    return {"text": "free text", "sql": f"SQL ({dialect} dialect)"}.get(card.answer_format, card.answer_format)


def grade(provider: LLMProvider, card: Card, answer: str, dialect: str, images: Sequence[ImageInput] = ()) -> Grade:
    diagnostics = checkers.check(card.answer_format, answer, dialect)
    if card.answer_format == "text":
        syntax = "Not applicable (free-text answer)."
    elif diagnostics:
        syntax = "\n".join(f"line {d.line}, column {d.col} ({d.severity}): {d.message}" for d in diagnostics)
    else:
        syntax = "No syntax problems found."
    system, user = render(
        "grade",
        format=_format_label(card, dialect),
        prompt=card.prompt,
        reference_answer=card.reference_answer,
        key_points="\n".join(f"- {p}" for p in card.rubric) or "(none given)",
        answer=answer,
        diagnostics=syntax,
    )
    data = provider.complete("grade", system, user, GRADE_SCHEMA, images)
    if data.get("verdict") not in VERDICTS or not isinstance(data.get("feedback"), str):
        raise LLMError("The grader's reply was incomplete.")
    return Grade(
        verdict=data["verdict"],
        feedback=data["feedback"].strip(),
        rubric_hits=list(data.get("rubric_hits") or []),
        rubric_misses=list(data.get("rubric_misses") or []),
        equivalent_approach=bool(data.get("equivalent_approach")),
    )


def hint(
    provider: LLMProvider, card: Card, answer: str, level: int, earlier: list[str], dialect: str,
    images: Sequence[ImageInput] = (),
) -> str:
    level = max(1, min(level, MAX_HINT_LEVEL))
    system, user = render(
        "hint",
        format=_format_label(card, dialect),
        prompt=card.prompt,
        reference_answer=card.reference_answer,
        answer=answer.strip() or "(nothing written yet)",
        earlier_hints="\n".join(f"{i}. {h}" for i, h in enumerate(earlier, start=1)) or "(none)",
        level=level,
    )
    data = provider.complete("hint", system, user, HINT_SCHEMA, images)
    text = data.get("hint")
    if not isinstance(text, str) or not text.strip():
        raise LLMError("The tutor's hint came back empty.")
    return text.strip()


@dataclass(frozen=True)
class CardConcepts:
    introduced: list[str]
    required: list[str]
    difficulty: int


def analyze_tier(
    provider: LLMProvider,
    collection_name: str,
    tier: int,
    cards: Sequence[Card],
    fixed: dict[int, Analysis],
    lower_concepts: Sequence[str],
    existing_concepts: Sequence[str],
) -> dict[int, CardConcepts]:
    """One call for a whole tier, so concept names stay consistent (DESIGN.md §6.1).

    `fixed` holds your manual analyses: they're shown to the model as context and never replaced.
    Returns concepts for the other cards; any card the model skipped is simply left out.
    """
    blocks = []
    for card in cards:
        fixed_line = ""
        if card.id in fixed:
            a = fixed[card.id]
            fixed_line = (f'\n<fixed_concepts introduced="{escape(", ".join(a.introduced))}" '
                          f'required="{escape(", ".join(a.required))}"/>')
        blocks.append(
            f'<card id="{card.id}" format="{card.answer_format}" tags="{escape(", ".join(card.tags))}">\n'
            f"<prompt>\n{card.prompt}\n</prompt>\n"
            f"<reference_answer>\n{card.reference_answer}\n</reference_answer>{fixed_line}\n</card>"
        )
    system, user = render(
        "sequence",
        collection=collection_name,
        tier=tier,
        lower_concepts=", ".join(lower_concepts) or "(none)",
        existing_concepts=", ".join(existing_concepts) or "(none yet)",
        cards="\n\n".join(blocks),
    )
    data = provider.complete("sequence", system, user, SEQUENCE_SCHEMA)
    wanted = {c.id for c in cards} - set(fixed)
    result: dict[int, CardConcepts] = {}
    for item in data.get("cards") or []:
        try:
            card_id, difficulty = int(item["card_id"]), int(item["difficulty"])
        except (KeyError, TypeError, ValueError):
            continue
        if card_id in wanted:
            result[card_id] = CardConcepts(
                introduced=concept_list(item.get("concepts_introduced") or []),
                required=concept_list(item.get("concepts_required") or []),
                difficulty=max(1, min(difficulty, 5)),
            )
    if wanted and not result:
        raise LLMError("The model didn't return concepts for any card.")
    return result


MAX_TIERS = 5

RETIER_SCHEMA = {
    "type": "object",
    "properties": {
        "tiers": {"type": "array", "items": {"type": "string"}},
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"card_id": {"type": "integer"}, "tier": {"type": "integer"}},
                "required": ["card_id", "tier"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tiers", "cards"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Retier:
    labels: list[str]  # tier names in study order; tier n is labels[n - 1]
    tiers: dict[int, int]  # card id → tier


def suggest_tiers(provider: LLMProvider, collection_name: str, cards: Sequence[Card]) -> Retier:
    """Sort every card of a collection into 1–5 tiers in one call (DESIGN.md §12.6).

    The cards' current tiers are deliberately not shown. Cards the model leaves out keep their old tier
    number (clamped to the new tier count), so nothing is lost.
    """
    blocks = "\n\n".join(
        f'<card id="{c.id}">\n{escape(c.prompt[:300])}\n<answer>{escape(c.reference_answer[:200])}</answer>\n</card>'
        for c in cards
    )
    system, user = render("retier", collection=escape(collection_name), cards=blocks)
    data = provider.complete("sequence", system, user, RETIER_SCHEMA)
    names = [unescape(str(n)).strip() for n in data.get("tiers") or []]
    labels = list(dict.fromkeys(n for n in names if n))[:MAX_TIERS]
    if not labels:
        raise LLMError("The AI didn't suggest any tiers.")
    wanted = {c.id: c for c in cards}
    assigned: dict[int, int] = {}
    for item in data.get("cards") or []:
        try:
            card_id, tier = int(item["card_id"]), int(item["tier"])
        except (KeyError, TypeError, ValueError):
            continue
        if card_id in wanted:
            assigned[card_id] = max(1, min(tier, len(labels)))
    if wanted and not assigned:
        raise LLMError("The AI didn't place any card in a tier.")
    return Retier(labels, {cid: assigned.get(cid, min(c.tier, len(labels))) for cid, c in wanted.items()})


DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tier": {"type": "integer"},
                    "answer_format": {"type": "string", "enum": list(ANSWER_FORMATS)},
                    "prompt": {"type": "string"},
                    "reference_answer": {"type": "string"},
                    "rubric": {"type": "array", "items": {"type": "string"}},
                    "references": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                            "required": ["title", "url"],
                            "additionalProperties": False,
                        },
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "concepts_introduced": {"type": "array", "items": {"type": "string"}},
                    "concepts_required": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["tier", "answer_format", "prompt", "reference_answer", "rubric", "references",
                             "tags", "concepts_introduced", "concepts_required"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}

MAX_DRAFTS = 5


@dataclass(frozen=True)
class Draft:
    card: CardIn  # references are kept separately: they're only saved if you tick them
    suggested_references: list[dict]
    introduced: list[str]
    required: list[str]

    def payload(self) -> dict:
        """Everything needed to save this draft later (carried in the page while you decide)."""
        return {"card": self.card.model_dump(exclude={"references"}), "references": self.suggested_references,
                "introduced": self.introduced, "required": self.required}


def draft_cards(
    provider: LLMProvider,
    collection: Collection,
    cards: Sequence[Card],
    analyses: dict[int, Analysis],
    notes: str,
    count: int,
    tier: int | None = None,
    concept: str = "",
) -> list[Draft]:
    """Draft up to `count` cards from your notes, in the collection's style. Nothing is saved here."""
    count = max(1, min(count, MAX_DRAFTS))
    tiers = sorted({c.tier for c in cards} | set(collection.tier_labels) | {1})
    examples = [c for c in cards if tier is None or c.tier == tier][:3] or list(cards[:3])
    concepts_by_tier: dict[int, list[str]] = {}
    for c in cards:
        if c.id in analyses:
            concepts_by_tier.setdefault(c.tier, []).extend(analyses[c.id].introduced)
    system, user = render(
        "draft",
        collection=escape(collection.name),
        dialect=collection.sql_dialect,
        tiers="\n".join(f"- {collection.tier_label(t)}" for t in tiers),
        existing_concepts="\n".join(
            f"- tier {t}: {', '.join(concept_list(names))}" for t, names in sorted(concepts_by_tier.items())
        ) or "(none yet)",
        existing_prompts="\n".join(f"- {c.prompt.splitlines()[0][:120]}" for c in cards[:200]) or "(none yet)",
        examples="\n\n".join(
            f'<card tier="{c.tier}" format="{c.answer_format}">\n<prompt>\n{c.prompt}\n</prompt>\n'
            f"<reference_answer>\n{c.reference_answer}\n</reference_answer>\n"
            f"<key_points>{'; '.join(c.rubric)}</key_points>\n</card>"
            for c in examples
        ) or "(no cards yet)",
        count=count,
        tier=tier or "",
        concept=escape(concept),
        notes=notes.strip(),
    )
    data = provider.complete("draft", system, user, DRAFT_SCHEMA)
    drafts = []
    for item in (data.get("cards") or [])[:count]:
        try:
            card = CardIn(
                tier=tier or max(1, int(item.get("tier") or 1)),
                answer_format=item.get("answer_format", "text"),
                prompt=item.get("prompt", ""),
                reference_answer=item.get("reference_answer", ""),
                rubric=[r for r in item.get("rubric") or [] if isinstance(r, str) and r.strip()],
                tags=[t for t in item.get("tags") or [] if isinstance(t, str) and t.strip()],
            )
        except (ValueError, TypeError):  # incl. pydantic's ValidationError: skip unusable drafts
            continue
        references = [r for r in item.get("references") or []
                      if isinstance(r, dict) and str(r.get("url", "")).startswith(("http://", "https://"))]
        drafts.append(Draft(card, references, concept_list(item.get("concepts_introduced") or []),
                            concept_list(item.get("concepts_required") or [])))
    if not drafts:
        raise LLMError("The AI didn't produce any usable cards. Try adding more detail to your notes.")
    return drafts


DECK_ORDER_SCHEMA = {
    "type": "object",
    "properties": {"order": {"type": "array", "items": {"type": "string"}}},
    "required": ["order"],
    "additionalProperties": False,
}


def order_subdecks(provider: LLMProvider, deck_name: str, subdecks: Sequence[tuple[str, Sequence[str]]]) -> list[str]:
    """Suggest a foundations-first study order for a deck's subdecks: (name, example prompts) pairs.
    Names the model leaves out keep their place at the end; unknown names are ignored."""
    blocks = "\n\n".join(
        f'<subdeck name="{escape(name)}">\n' + "\n".join(f"- {p}" for p in prompts) + "\n</subdeck>"
        for name, prompts in subdecks
    )
    system, user = render("deck_order", deck=escape(deck_name), subdecks=blocks)
    data = provider.complete("sequence", system, user, DECK_ORDER_SCHEMA)
    names = [name for name, _ in subdecks]
    order = [n for n in dict.fromkeys(unescape(str(n)) for n in data.get("order") or []) if n in names]
    if not order:
        raise LLMError("The AI didn't suggest an order.")
    return order + [n for n in names if n not in order]
