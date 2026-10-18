"""Stats: pure calculations over attempts, cards and their progress (DESIGN.md §10, Phase 4)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .engine import INCORRECT, LEARNING, REVEALED, Progress
from .models import Analysis, AttemptRecord, Card


def local_day(ts: str) -> date:
    return datetime.fromisoformat(ts).astimezone().date()


@dataclass(frozen=True)
class Totals:
    attempts: int
    right: int
    with_hints: int
    median_seconds: float | None

    @property
    def right_pct(self) -> float:
        return self.right / self.attempts if self.attempts else 0.0


def totals(records: Sequence[AttemptRecord]) -> Totals:
    records = [r for r in records if r.outcome != REVEALED]  # peeks aren't answers
    durations = sorted(r.duration_ms for r in records if r.duration_ms)
    median = durations[len(durations) // 2] / 1000 if durations else None
    return Totals(
        attempts=len(records),
        right=sum(r.outcome != INCORRECT for r in records),
        with_hints=sum(r.max_hint_level > 0 for r in records),
        median_seconds=median,
    )


@dataclass(frozen=True)
class Day:
    day: date
    attempts: int
    right: int

    @property
    def right_pct(self) -> float:
        return self.right / self.attempts if self.attempts else 0.0


def daily(records: Sequence[AttemptRecord], days: int = 30, today: date | None = None) -> list[Day]:
    """One entry per calendar day for the last `days` days (empty days included, so gaps show)."""
    today = today or datetime.now().astimezone().date()
    counts: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    for r in records:
        if r.outcome == REVEALED:
            continue
        d = local_day(r.ts)
        counts[d][0] += 1
        counts[d][1] += r.outcome != INCORRECT
    start = today - timedelta(days=days - 1)
    return [Day(start + timedelta(i), *counts.get(start + timedelta(i), [0, 0])) for i in range(days)]


@dataclass(frozen=True)
class GraderRow:
    answer_format: str
    graded: int
    overridden: int
    too_harsh: int  # the AI said wrong, you said right
    too_lenient: int  # the AI said right, you said wrong

    @property
    def override_pct(self) -> float:
        return self.overridden / self.graded if self.graded else 0.0


def grader_agreement(records: Sequence[AttemptRecord], cards: dict[int, Card]) -> list[GraderRow]:
    """How often you overrode the AI grader, per answer format. The evidence for model choices (DECISIONS #53)."""
    rows: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for r in records:
        if r.llm_verdict is None or r.card_id not in cards:
            continue
        row = rows[cards[r.card_id].answer_format]
        row[0] += 1
        if r.overridden:
            row[1] += 1
            row[2 if r.outcome != INCORRECT else 3] += 1
    return [GraderRow(fmt, *row) for fmt, row in sorted(rows.items())]


@dataclass(frozen=True)
class Weak:
    name: str
    accuracy: float  # over each card's last few attempts
    attempts: int
    card_ids: tuple[int, ...]


def weakest_concepts(
    cards: Sequence[Card], analyses: dict[int, Analysis], progress: dict[int, Progress], limit: int = 8
) -> list[Weak]:
    """Concepts whose cards you get wrong most, judged by the cards that teach them. Perfect ones aren't weak."""
    by_concept: dict[str, list[int]] = defaultdict(list)
    for c in cards:
        if c.id in analyses:
            for concept in analyses[c.id].introduced:
                by_concept[concept].append(c.id)
    weak = []
    for concept, ids in by_concept.items():
        tried = [cid for cid in ids if progress.get(cid) and progress[cid].attempts]
        attempts = sum(progress[cid].attempts for cid in tried)
        if attempts >= 2:
            accuracy = sum(progress[cid].recent_accuracy for cid in tried) / len(tried)
            if accuracy < 1:
                weak.append(Weak(concept, accuracy, attempts, tuple(ids)))
    return sorted(weak, key=lambda w: (w.accuracy, -w.attempts, w.name))[:limit]


def weakest_cards(cards: Sequence[Card], progress: dict[int, Progress], limit: int = 8) -> list[Card]:
    """Cards still being learned, lowest recent accuracy first (at least 2 attempts)."""
    candidates = [c for c in cards if progress[c.id].status == LEARNING and progress[c.id].attempts >= 2]
    return sorted(candidates, key=lambda c: (progress[c.id].recent_accuracy, -progress[c.id].attempts))[:limit]
