"""Sequencer: orders cards within each tier from their concept analysis (DESIGN.md §6). Pure logic.

A card that introduces a concept comes before the cards in its tier that require it. Ties are
broken by difficulty, then creation order. Cards without a current analysis ("unsequenced") go
last, in creation order, until the next rebuild.
"""

from __future__ import annotations

import heapq
import re
from collections import defaultdict
from collections.abc import Collection as Container
from collections.abc import Sequence
from dataclasses import dataclass, field

from .models import Analysis, Card


def normalize_concept(name: str) -> str:
    """'List Comprehension ' → 'list-comprehension', so names compare reliably."""
    return re.sub(r"[^a-z0-9+#.]+", "-", name.strip().lower()).strip("-")


def concept_list(names: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for name in names:
        if normalized := normalize_concept(name):
            seen[normalized] = None
    return list(seen)


def is_current(card: Card, analysis: Analysis | None) -> bool:
    """An analysis is current unless the card was edited after it. Your own (manual) analyses always are."""
    return analysis is not None and (analysis.manual or analysis.analyzed_at >= card.updated_at)


@dataclass(frozen=True)
class Warning:
    kind: str  # "cycle" | "missing" | "tier_mismatch"
    message: str
    card_ids: tuple[int, ...]
    concept: str = ""  # for "missing" and "tier_mismatch"


@dataclass
class CardSequence:
    order: dict[int, list[int]] = field(default_factory=dict)  # tier → card ids, in study order
    unsequenced: set[int] = field(default_factory=set)
    warnings: list[Warning] = field(default_factory=list)
    analyses: dict[int, Analysis] = field(default_factory=dict)  # current analyses only
    introduced_in_tier: dict[str, set[int]] = field(default_factory=dict)  # concept → tiers
    tier_of: dict[int, int] = field(default_factory=dict)  # card id → tier

    def lowest_tier(self, card_ids) -> int:
        return min((self.tier_of[cid] for cid in card_ids if cid in self.tier_of), default=1)

    def warnings_for(self, card_id: int) -> list[Warning]:
        return [w for w in self.warnings if card_id in w.card_ids]


def build(cards: Sequence[Card], analyses: dict[int, Analysis]) -> CardSequence:
    seq = CardSequence()
    current = {c.id: analyses[c.id] for c in cards if is_current(c, analyses.get(c.id))}
    seq.analyses = current
    seq.unsequenced = {c.id for c in cards if c.id not in current}
    seq.tier_of = {c.id: c.tier for c in cards}

    introduced_in_tier: dict[str, set[int]] = defaultdict(set)
    for c in cards:
        if c.id in current:
            for concept in current[c.id].introduced:
                introduced_in_tier[concept].add(c.tier)
    seq.introduced_in_tier = dict(introduced_in_tier)

    for tier in sorted({c.tier for c in cards}):
        in_tier = [c for c in cards if c.tier == tier]
        ordered, cycle_ids = _order_tier([c for c in in_tier if c.id in current], current, introduced_in_tier, tier)
        leftovers = sorted((c for c in in_tier if c.id not in current), key=lambda c: (c.created_at, c.id))
        seq.order[tier] = ordered + [c.id for c in leftovers]
        if cycle_ids:
            seq.warnings.append(Warning(
                "cycle",
                f"Tier {tier}: these cards depend on each other in a circle, so the order between them was guessed.",
                tuple(cycle_ids),
            ))

    _add_concept_warnings(seq, cards, current, introduced_in_tier)
    return seq


def _order_tier(
    cards: list[Card], analyses: dict[int, Analysis], introduced_in_tier: dict[str, set[int]], tier: int
) -> tuple[list[int], list[int]]:
    """Topological sort with a priority queue (difficulty, created_at, id). Cycles are broken, not fatal."""
    by_id = {c.id: c for c in cards}
    introducers: dict[str, set[int]] = defaultdict(set)
    for c in cards:
        for concept in analyses[c.id].introduced:
            introducers[concept].add(c.id)

    prereqs: dict[int, set[int]] = {c.id: set() for c in cards}
    for c in cards:
        for concept in analyses[c.id].required:
            if any(t < tier for t in introduced_in_tier.get(concept, ())):
                continue  # taught in a lower tier: tier gating already covers it
            prereqs[c.id] |= introducers.get(concept, set()) - {c.id}

    dependents: dict[int, set[int]] = defaultdict(set)
    for card_id, before in prereqs.items():
        for p in before:
            dependents[p].add(card_id)

    def priority(card_id: int) -> tuple:
        c = by_id[card_id]
        return (analyses[card_id].difficulty, c.created_at, c.id)

    cycle_ids = sorted(cid for cid in prereqs if _reaches_itself(cid, prereqs))
    remaining = {cid: len(p) for cid, p in prereqs.items()}
    heap = [(priority(cid), cid) for cid, n in remaining.items() if n == 0]
    heapq.heapify(heap)
    order = []
    while remaining:
        if not heap:  # every remaining card waits on another: a cycle. Release its easiest card.
            stuck = min((cid for cid in remaining if cid in cycle_ids), key=priority)
            remaining[stuck] = 0
            heapq.heappush(heap, (priority(stuck), stuck))
        _, cid = heapq.heappop(heap)
        if cid not in remaining:
            continue
        del remaining[cid]
        order.append(cid)
        for d in dependents[cid]:
            if d in remaining and remaining[d] > 0:
                remaining[d] -= 1
                if remaining[d] == 0:
                    heapq.heappush(heap, (priority(d), d))
    return order, cycle_ids


def _reaches_itself(start: int, prereqs: dict[int, set[int]]) -> bool:
    stack, seen = list(prereqs[start]), set()
    while stack:
        cid = stack.pop()
        if cid == start:
            return True
        if cid not in seen:
            seen.add(cid)
            stack.extend(prereqs[cid])
    return False


def _add_concept_warnings(
    seq: CardSequence, cards: Sequence[Card], analyses: dict[int, Analysis], introduced_in_tier: dict[str, set[int]]
) -> None:
    missing: dict[str, list[int]] = defaultdict(list)
    for c in cards:
        if c.id not in analyses:
            continue
        for concept in analyses[c.id].required:
            tiers = introduced_in_tier.get(concept)
            if not tiers:
                missing[concept].append(c.id)
            elif min(tiers) > c.tier:
                seq.warnings.append(Warning(
                    "tier_mismatch",
                    f"Needs “{concept}”, which is only taught in tier {min(tiers)}. This card may belong in a later tier.",
                    (c.id,),
                    concept,
                ))
    for concept, ids in sorted(missing.items()):
        count = f"{len(ids)} cards need" if len(ids) > 1 else "1 card needs"
        seq.warnings.append(Warning(
            "missing", f"{count} “{concept}”, but no card teaches it. Consider adding one.", tuple(ids), concept
        ))


def new_card_order(seq: CardSequence, tier: int, new_ids: Container[int], known_ids: Container[int]) -> list[int]:
    """New cards in study order, ready ones first (DESIGN.md §6.5). Readiness is a preference, not a lock."""
    ordered = [cid for cid in seq.order.get(tier, []) if cid in new_ids]
    ready = [cid for cid in ordered if _is_ready(seq, cid, tier, known_ids)]
    return ready + [cid for cid in ordered if cid not in ready]


def _is_ready(seq: CardSequence, card_id: int, tier: int, known_ids: Container[int]) -> bool:
    analysis = seq.analyses.get(card_id)
    if analysis is None:
        return True
    for concept in analysis.required:
        tiers = seq.introduced_in_tier.get(concept)
        if not tiers or any(t < tier for t in tiers):
            continue  # nobody teaches it (nothing to wait for), or a lower tier does
        teachers = [cid for cid, a in seq.analyses.items()
                    if concept in a.introduced and cid != card_id and seq.tier_of.get(cid) == tier]
        if teachers and not any(cid in known_ids for cid in teachers):
            return False
    return True
