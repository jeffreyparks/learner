"""Combine collections and re-tier them (DESIGN.md §12.4).

Each tier of each chosen collection is a "row". You map every row to a tier of one target
collection (a new one, or one of those chosen), the same way Anki subdecks map to tiers on import.
Cards keep their ids, so progress, history and concept analyses all carry over.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from . import store, tiers
from .anki import plain
from .models import Collection


@dataclass(frozen=True)
class Row:
    collection: Collection
    tier: int
    count: int
    prompts: list[str]  # a few examples, as plain text

    @property
    def key(self) -> str:
        return f"{self.collection.id}_{self.tier}"

    @property
    def label(self) -> str:
        """What this row is called: the collection, plus its tier when the collection has several."""
        tier_label = self.collection.tier_labels.get(self.tier)
        return f"{self.collection.name} · {tier_label or f'Tier {self.tier}'}"


@dataclass
class Mapping:
    tier: int
    label: str  # label for the target tier


def example(prompt_markdown: str) -> str:
    """A prompt as one line of plain text: images dropped, Markdown markers removed."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", prompt_markdown or "")
    return " ".join(re.sub(r"[`*_#>|]+", "", plain(text)).split())[:160]


def rows_for(conn: sqlite3.Connection, collection_ids: list[int]) -> list[Row]:
    rows = []
    for cid in collection_ids:
        collection = store.get_collection(conn, cid)
        if collection is None:
            continue
        by_tier: dict[int, list] = defaultdict(list)
        for card in store.list_cards(conn, cid, include_archived=True):
            by_tier[card.tier].append(card)
        for tier in sorted(by_tier):
            cards = by_tier[tier]
            rows.append(Row(collection, tier, len(cards), [example(c.prompt) for c in cards[:3]]))
    return rows


def suggested_name(collections: list[Collection]) -> str:
    """The words the names share at the start ("Causal Inference …" → "Causal Inference"), else the first name."""
    if len(collections) == 1:
        return collections[0].name
    words = [c.name.split() for c in collections]
    shared = []
    for group in zip(*words):
        if len({w.casefold() for w in group}) != 1:
            break
        shared.append(group[0])
    common = " ".join(shared).strip(" -–—:·›")
    return common if len(common) >= 3 else sorted(collections, key=lambda c: c.name.casefold())[0].name


def default_label(row: Row, target_name: str, several_tiers: bool) -> str:
    """A short label for the target tier: drop the shared name ("Causal Inference DAGs" → "DAGs")."""
    name = row.collection.name
    if target_name and name.casefold().startswith(target_name.casefold()):
        name = name[len(target_name):].strip(" -–—:·›")
    tier_label = row.collection.tier_labels.get(row.tier)
    if several_tiers:
        name = f"{name} · {tier_label or f'Tier {row.tier}'}" if name else (tier_label or f"Tier {row.tier}")
    return name or "Basics"


def default_mapping(rows: list[Row], target_name: str, order: list[str] | None = None) -> dict[str, Mapping]:
    """One target tier per row, in `order` (row keys, e.g. the AI's suggestion) or the rows' own order."""
    tiers_per_collection = defaultdict(int)
    for row in rows:
        tiers_per_collection[row.collection.id] += 1
    keys = [k for k in (order or []) if k in {r.key for r in rows}]
    keys += [r.key for r in rows if r.key not in keys]
    by_key = {r.key: r for r in rows}
    return {
        key: Mapping(i, default_label(by_key[key], target_name, tiers_per_collection[by_key[key].collection.id] > 1))
        for i, key in enumerate(keys, start=1)
    }


def tier_labels(mapping: dict[str, Mapping]) -> dict[int, str]:
    labels: dict[int, list[str]] = defaultdict(list)
    for m in sorted(mapping.values(), key=lambda m: m.tier):
        if m.label.strip() and m.label.strip() not in labels[m.tier]:
            labels[m.tier].append(m.label.strip())
    return {tier: " + ".join(names) for tier, names in labels.items()}


class OrganizeError(ValueError):
    pass


@dataclass
class Result:
    collection_id: int
    moved: int
    archived: list[str]


def apply(
    conn: sqlite3.Connection, rows: list[Row], mapping: dict[str, Mapping], name: str, group: str
) -> Result:
    """Move every card of the chosen rows into the target collection at its mapped tier.

    The target is one of the chosen collections (if the name matches) or a new one. Chosen collections
    left empty are archived, never deleted. Raises OrganizeError for a target that would mix in an
    unrelated existing collection.
    """
    name, group = name.strip(), group.strip()
    if not name:
        raise OrganizeError("Give the combined collection a name.")
    if not rows:
        raise OrganizeError("Choose at least one collection with cards.")
    sources = {r.collection.id: r.collection for r in rows}
    existing = store.find_collection(conn, name, group)
    if existing and existing.id not in sources:
        raise OrganizeError(f"“{existing.display_name}” already exists. Pick a different name, or include it in the selection.")
    first = rows[0].collection
    if existing:
        target_id = existing.id
    else:
        target_id = store.save_collection(conn, name, first.sql_dialect, {}, group=group)

    moved = 0
    by_row = {(r.collection.id, r.tier): mapping[r.key].tier for r in rows if r.key in mapping}
    for cid in sources:
        for card in store.list_cards(conn, cid, include_archived=True):
            tier = by_row.get((cid, card.tier))
            if tier is not None and (card.collection_id, card.tier) != (target_id, tier):
                store.move_card(conn, card.id, target_id, tier)
                moved += 1

    target = store.get_collection(conn, target_id)
    store.save_collection(conn, target.name, target.sql_dialect, tier_labels(mapping), target_id, group=target.group)
    tiers.tidy(conn, target_id)  # e.g. tiers 1, 3, 4 (a skipped number) become 1, 2, 3
    archived = []
    for cid, collection in sources.items():
        if cid != target_id and not store.list_cards(conn, cid, include_archived=True):
            store.set_collection_archived(conn, cid, True)
            archived.append(collection.display_name)
    return Result(target_id, moved, archived)


def row_key_parts(key: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d+)_(\d+)", key)
    return (int(match[1]), int(match[2])) if match else None
