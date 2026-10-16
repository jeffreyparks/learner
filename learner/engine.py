"""Study engine: pure functions over cards and attempts. No I/O, no LLM, no stored state.

Progress is always calculated from the attempt log (DESIGN.md §5). The caller passes the
collection's active cards and all of their attempts; everything else is derived here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from . import sequencer
from .config import Config
from .models import Attempt, Card
from .sequencer import CardSequence

NEW, LEARNING, KNOWN = "new", "learning", "known"
CORRECT, ASSISTED, INCORRECT = "correct", "assisted", "incorrect"
RECENT_WINDOW = 5  # attempts used to judge how weak a learning card is


def outcome_for(right: bool, hints_used: int) -> str:
    """Whoever graded it (you or the LLM): any hint turns a right answer into `assisted` (DESIGN.md §5.2)."""
    if not right:
        return INCORRECT
    return ASSISTED if hints_used > 0 else CORRECT


# --- per-card progress ---


@dataclass(frozen=True)
class Progress:
    status: str
    streak: int = 0
    attempts: int = 0
    recent_accuracy: float = 0.0
    last_seen: str | None = None


def card_progress(card: Card, attempts: Sequence[Attempt], known_streak: int) -> Progress:
    """`attempts` are this card's attempts in order. Only a card's first attempt in a session can raise its streak."""
    counted = [a for a in attempts if card.progress_reset_at is None or a.ts > card.progress_reset_at]
    if not counted:
        return Progress(NEW)
    streak = 0
    sessions_seen: set[str] = set()
    for a in counted:
        first_in_session = a.session_id not in sessions_seen
        sessions_seen.add(a.session_id)
        if a.outcome == INCORRECT:
            streak = 0
        elif a.outcome == CORRECT and first_in_session:
            streak += 1
    recent = counted[-RECENT_WINDOW:]
    accuracy = sum(a.outcome != INCORRECT for a in recent) / len(recent)
    status = KNOWN if streak >= known_streak else LEARNING
    return Progress(status, streak, len(counted), accuracy, counted[-1].ts)


def progress_map(
    cards: Sequence[Card], attempts: Sequence[Attempt], known_streak: int, exclude_session: str | None = None
) -> dict[int, Progress]:
    """Progress for every card. `exclude_session` gives the state as it was when that session started."""
    by_card: dict[int, list[Attempt]] = {c.id: [] for c in cards}
    for a in sorted(attempts, key=lambda a: a.id):
        if a.card_id in by_card and a.session_id != exclude_session:
            by_card[a.card_id].append(a)
    return {c.id: card_progress(c, by_card[c.id], known_streak) for c in cards}


# --- tiers ---


@dataclass(frozen=True)
class TierSummary:
    tier: int
    total: int
    new: int
    learning: int
    known: int
    unlocked: bool

    @property
    def pct_known(self) -> float:
        return self.known / self.total if self.total else 0.0

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.known == self.total


def _pct_known(cards: Sequence[Card], progress: dict[int, Progress], tier: int) -> float:
    in_tier = [c for c in cards if c.tier == tier]
    return sum(progress[c.id].status == KNOWN for c in in_tier) / len(in_tier) if in_tier else 0.0


def unlocked_through(cards: Sequence[Card], progress: dict[int, Progress], stored_max: int, unlock_pct: float) -> int:
    """Highest unlocked tier. Never lower than `stored_max` (unlocks are sticky); the lowest tier is always open."""
    tiers = sorted({c.tier for c in cards})
    if not tiers:
        return stored_max
    highest = max(stored_max, tiers[0])
    for lower, upper in zip(tiers, tiers[1:]):
        if lower <= highest < upper and _pct_known(cards, progress, lower) >= unlock_pct:
            highest = upper
    return highest


def tier_summaries(cards: Sequence[Card], progress: dict[int, Progress], unlocked: int) -> list[TierSummary]:
    summaries = []
    for tier in sorted({c.tier for c in cards}):
        counts = Counter(progress[c.id].status for c in cards if c.tier == tier)
        summaries.append(
            TierSummary(
                tier=tier,
                total=sum(counts.values()),
                new=counts[NEW],
                learning=counts[LEARNING],
                known=counts[KNOWN],
                unlocked=tier <= unlocked,
            )
        )
    return summaries


def default_tier(summaries: Sequence[TierSummary]) -> int | None:
    """The tier a session starts on by default: the highest unlocked one."""
    unlocked = [s.tier for s in summaries if s.unlocked]
    return max(unlocked) if unlocked else None


# --- sessions ---


def session_plan(
    cards: Sequence[Card], start: dict[int, Progress], tier: int, cfg: Config, order: CardSequence | None = None
) -> list[int]:
    """Card ids planned for a session, fixed by the progress at the session's start (DESIGN.md §5.5).

    New cards follow `order` (the concept graph, ready cards first) when given, else creation order.
    """
    in_tier = [c for c in cards if c.tier == tier]
    learning = sorted(
        (c for c in in_tier if start[c.id].status == LEARNING),
        key=lambda c: (start[c.id].recent_accuracy, start[c.id].last_seen or "", c.id),
    )
    new = sorted((c for c in in_tier if start[c.id].status == NEW), key=lambda c: (c.created_at, c.id))
    if order is not None:
        known_ids = {cid for cid, p in start.items() if p.status == KNOWN}
        by_id = {c.id: c for c in new}
        new = [by_id[cid] for cid in sequencer.new_card_order(order, tier, set(by_id), known_ids)]
    known = sorted(
        (c for c in cards if c.tier <= tier and start[c.id].status == KNOWN),
        key=lambda c: (start[c.id].last_seen or "", c.id),
    )
    if not learning and not new:  # tier complete: a short refresher-only pass
        return [c.id for c in known[: max(1, cfg.session_size // 4)]]
    n_new = min(len(new), cfg.new_per_session)
    n_refresh = min(len(known), round(cfg.session_size * cfg.refresher_pct))
    n_learning = max(0, cfg.session_size - n_new - n_refresh)
    return [c.id for c in learning[:n_learning] + new[:n_new] + known[:n_refresh]]


@dataclass(frozen=True)
class SessionState:
    next_card_id: int | None
    answered: int
    remaining: int


def session_state(
    cards: Sequence[Card], attempts: Sequence[Attempt], session_id: str, tier: int, cfg: Config,
    order: CardSequence | None = None,
) -> SessionState:
    """What to show next. Stateless: the session is reconstructed from its attempts every time."""
    start = progress_map(cards, attempts, cfg.known_streak, exclude_session=session_id)
    plan = session_plan(cards, start, tier, cfg, order)
    active = {c.id for c in cards}
    session = [a for a in sorted(attempts, key=lambda a: a.id) if a.session_id == session_id and a.card_id in active]

    last_index: dict[int, int] = {}
    last_outcome: dict[int, str] = {}
    counts: Counter[int] = Counter()
    for i, a in enumerate(session):
        last_index[a.card_id] = i
        last_outcome[a.card_id] = a.outcome
        counts[a.card_id] += 1

    # Misses come back after `requeue_gap` other cards, up to `max_attempts_per_card` tries per session.
    pending = sorted(
        (i, card_id)
        for card_id, i in last_index.items()
        if last_outcome[card_id] == INCORRECT and counts[card_id] < cfg.max_attempts_per_card
    )
    due = [card_id for i, card_id in pending if len(session) - 1 - i >= cfg.requeue_gap]
    unseen = [card_id for card_id in plan if card_id not in last_index]

    if due:
        next_id = due[0]
    elif unseen:
        next_id = unseen[0]
    elif pending:
        next_id = pending[0][1]
    else:
        next_id = None
    return SessionState(next_id, len(session), len(unseen) + len(pending))


@dataclass(frozen=True)
class SessionSummary:
    answered: int
    right: int
    wrong: int
    newly_known: int


def session_summary(
    cards: Sequence[Card], attempts: Sequence[Attempt], session_id: str, known_streak: int
) -> SessionSummary:
    start = progress_map(cards, attempts, known_streak, exclude_session=session_id)
    end = progress_map(cards, attempts, known_streak)
    session = [a for a in attempts if a.session_id == session_id]
    wrong = sum(a.outcome == INCORRECT for a in session)
    newly_known = sum(end[c.id].status == KNOWN and start[c.id].status != KNOWN for c in cards)
    return SessionSummary(len(session), len(session) - wrong, wrong, newly_known)
