"""Tiers of a collection (DESIGN.md §12.5): the one place that changes them.

The rule: a collection's tiers are exactly 1…N, and `tier_labels` has one entry per tier (a blank
label displays as "Tier n"). Every operation here updates cards, labels and the sticky unlock
together in one transaction, then tidies up, so labels can't drift from the cards.

Archived cards are renumbered along with everyone else so they keep their place. Cards archived by
deleting their tier are parked beyond the last tier; unarchiving puts them in the last tier.
"""

from __future__ import annotations

import json
import sqlite3

from . import store

PARKED_TIER = 10_000  # where cards of a deleted tier wait (archived) until someone unarchives them


class TierError(ValueError):
    pass


def _state(conn: sqlite3.Connection, collection_id: int) -> tuple[dict[int, str], int]:
    row = conn.execute("SELECT tier_labels_json, max_unlocked_tier FROM collection WHERE id = ?", (collection_id,)).fetchone()
    if row is None:
        raise TierError("Collection not found.")
    return {int(k): v for k, v in json.loads(row[0]).items()}, row[1]


def _write(conn: sqlite3.Connection, collection_id: int, labels: dict[int, str], max_unlocked: int) -> None:
    conn.execute(
        "UPDATE collection SET tier_labels_json = ?, max_unlocked_tier = ? WHERE id = ?",
        (json.dumps({str(k): v for k, v in sorted(labels.items())}), max(1, max_unlocked), collection_id),
    )


def _renumber(conn: sqlite3.Connection, collection_id: int, mapping: dict[int, int]) -> None:
    """Move every card (archived too) from tier old → new, in one statement so swaps don't collide."""
    if not mapping or all(k == v for k, v in mapping.items()):
        return
    cases = " ".join(f"WHEN {int(old)} THEN {int(new)}" for old, new in mapping.items())
    olds = ",".join(str(int(o)) for o in mapping)
    conn.execute(f"UPDATE card SET tier = CASE tier {cases} END WHERE collection_id = ? AND tier IN ({olds})", (collection_id,))


def _unlocked_after(max_unlocked: int, mapping: dict[int, int]) -> int:
    """Unlocked tiers stay unlocked: the new limit covers wherever the old unlocked tiers went."""
    moved = [new for old, new in mapping.items() if old <= max_unlocked]
    return max(moved, default=1)


def tidy(conn: sqlite3.Connection, collection_id: int) -> None:
    """Enforce the rule: tiers are 1…N with no gaps, every tier with active cards has a label entry."""
    with conn:
        _tidy(conn, collection_id)


def _tidy(conn: sqlite3.Connection, collection_id: int) -> None:
    labels, max_unlocked = _state(conn, collection_id)
    in_use = {t for (t,) in conn.execute(
        "SELECT DISTINCT tier FROM card WHERE collection_id = ? AND archived = 0", (collection_id,))}
    existing = sorted((in_use | set(labels)) - {PARKED_TIER})
    mapping = {old: new for new, old in enumerate(existing, start=1)}
    _renumber(conn, collection_id, mapping)
    new_labels = {mapping[old]: labels.get(old, "") for old in existing}
    _write(conn, collection_id, new_labels, _unlocked_after(max_unlocked, mapping) if mapping else 1)


def tidy_all(conn: sqlite3.Connection) -> None:
    for collection in store.list_collections(conn, include_archived=True):
        tidy(conn, collection.id)


def count(conn: sqlite3.Connection, collection_id: int) -> int:
    return len(_state(conn, collection_id)[0])


def set_labels(conn: sqlite3.Connection, collection_id: int, new_labels: dict[int, str]) -> None:
    """Rename tiers. Only existing tiers can be named; unknown numbers are ignored."""
    with conn:
        labels, max_unlocked = _state(conn, collection_id)
        for tier, label in new_labels.items():
            if tier in labels:
                labels[tier] = label.strip()
        _write(conn, collection_id, labels, max_unlocked)


def add(conn: sqlite3.Connection, collection_id: int, label: str = "") -> int:
    """Add an empty tier at the end. Returns its number."""
    with conn:
        _tidy(conn, collection_id)
        labels, max_unlocked = _state(conn, collection_id)
        tier = len(labels) + 1
        labels[tier] = label.strip()
        _write(conn, collection_id, labels, max_unlocked)
        return tier


def swap(conn: sqlite3.Connection, collection_id: int, tier: int, other: int) -> None:
    """Swap two tiers (cards and labels). Used for ↑ / ↓."""
    with conn:
        labels, max_unlocked = _state(conn, collection_id)
        if tier not in labels or other not in labels or tier == other:
            raise TierError("There's no tier to swap with.")
        _renumber(conn, collection_id, {tier: other, other: tier})
        labels[tier], labels[other] = labels[other], labels[tier]
        mapping = {t: t for t in labels} | {tier: other, other: tier}
        _write(conn, collection_id, labels, _unlocked_after(max_unlocked, mapping))
        _tidy(conn, collection_id)


def move_tier_cards(conn: sqlite3.Connection, collection_id: int, tier: int, to: int) -> int:
    """Move all of a tier's cards (archived too) into another tier; the emptied tier stays. Returns how many."""
    with conn:
        labels, _ = _state(conn, collection_id)
        if tier not in labels or to not in labels:
            raise TierError("Choose an existing tier.")
        cur = conn.execute("UPDATE card SET tier = ? WHERE collection_id = ? AND tier = ?", (to, collection_id, tier))
        _tidy(conn, collection_id)
        return cur.rowcount


def move_cards(conn: sqlite3.Connection, card_ids: list[int], collection_id: int, to: int) -> int:
    """Move chosen cards (from any collections) into a tier of `collection_id` (`to` may be N+1: a new
    tier at the end). Source collections are tidied afterwards; their emptied tiers stay."""
    with conn:
        labels, max_unlocked = _state(conn, collection_id)
        if to == len(labels) + 1:
            labels[to] = ""
            _write(conn, collection_id, labels, max_unlocked)
        elif to not in labels:
            raise TierError("Choose an existing tier.")
        marks = ",".join("?" for _ in card_ids)
        sources = {c for (c,) in conn.execute(f"SELECT DISTINCT collection_id FROM card WHERE id IN ({marks})", card_ids)}
        cur = conn.execute(f"UPDATE card SET collection_id = ?, tier = ? WHERE id IN ({marks})", (collection_id, to, *card_ids))
        for cid in sources | {collection_id}:
            _tidy(conn, cid)
        return cur.rowcount


def delete(conn: sqlite3.Connection, collection_id: int, tier: int) -> int:
    """Delete a tier: its active cards are archived (history kept), and the tiers above move down.
    Returns how many cards were archived."""
    with conn:
        labels, max_unlocked = _state(conn, collection_id)
        if tier not in labels:
            raise TierError("That tier doesn't exist.")
        archived = conn.execute(
            "UPDATE card SET archived = 1 WHERE collection_id = ? AND tier = ? AND archived = 0", (collection_id, tier)
        ).rowcount
        conn.execute("UPDATE card SET tier = ? WHERE collection_id = ? AND tier = ?", (PARKED_TIER, collection_id, tier))
        del labels[tier]
        mapping = {t: (t - 1 if t > tier else t) for t in labels}
        _renumber(conn, collection_id, mapping)
        new_labels = {mapping[t]: label for t, label in labels.items()}
        _write(conn, collection_id, new_labels, max_unlocked - 1 if tier <= max_unlocked else max_unlocked)
        _tidy(conn, collection_id)
        return archived


def place_unarchived(conn: sqlite3.Connection, card_id: int) -> bool:
    """When a card comes back from the archive into a tier that no longer exists, put it in the last
    tier (adding tier 1 if the collection has none). Returns True if it was moved."""
    card = store.get_card(conn, card_id)
    with conn:
        labels, max_unlocked = _state(conn, card.collection_id)
        if card.tier in labels:
            return False
        if not labels:
            labels[1] = ""
            _write(conn, card.collection_id, labels, max_unlocked)
        conn.execute("UPDATE card SET tier = ? WHERE id = ?", (max(labels), card_id))
        return True
