"""SQLite access: migrations, collections, cards, attempts, and JSON import/export."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from . import media
from .models import Analysis, Attempt, AttemptRecord, Card, CardIn, Collection, CollectionIn, Reference, describe_errors

MIGRATIONS = Path(__file__).parent / "migrations"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """Apply numbered migrations newer than PRAGMA user_version, each in its own transaction."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    pending = [p for p in sorted(MIGRATIONS.glob("*.sql")) if int(p.name.split("_", 1)[0]) > current]
    if not pending:
        return
    # Some migrations rebuild a table, which SQLite only allows with foreign keys off (its documented
    # procedure). Turn them off around the migrations, then check nothing was left dangling.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for path in pending:
            version = int(path.name.split("_", 1)[0])
            conn.executescript(f"BEGIN;\n{path.read_text()}\nPRAGMA user_version = {version};\nCOMMIT;")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"Database migration left broken references: {[tuple(r) for r in problems][:5]}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def backup(conn: sqlite3.Connection, folder: str | Path) -> Path:
    """Copy the whole database to `folder/learner-<timestamp>.db` (a consistent snapshot, safe while in use)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"learner-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    with closing(sqlite3.connect(target)) as copy:
        conn.backup(copy)
    return target


def wipe(conn: sqlite3.Connection) -> None:
    """Delete every row of every table (the schema and its version stay). Callers re-seed afterwards."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            for table in tables:
                conn.execute(f'DELETE FROM "{table}"')
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
                conn.execute("DELETE FROM sqlite_sequence")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


# --- collections ---


def _collection(row: sqlite3.Row) -> Collection:
    return Collection(
        id=row["id"],
        name=row["name"],
        tier_labels={int(k): v for k, v in json.loads(row["tier_labels_json"]).items()},
        sql_dialect=row["sql_dialect"],
        max_unlocked_tier=row["max_unlocked_tier"],
        archived=bool(row["archived"]),
        group=row["group_name"],
        pace=row["pace"],
    )


def list_collections(conn: sqlite3.Connection, include_archived: bool = False) -> list[Collection]:
    sql = "SELECT * FROM collection" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY group_name, name"
    return [_collection(r) for r in conn.execute(sql)]


def get_collection(conn: sqlite3.Connection, collection_id: int) -> Collection | None:
    row = conn.execute("SELECT * FROM collection WHERE id = ?", (collection_id,)).fetchone()
    return _collection(row) if row else None


def find_collection(conn: sqlite3.Connection, name: str, group: str = "") -> Collection | None:
    row = conn.execute("SELECT * FROM collection WHERE name = ? AND group_name = ?", (name, group)).fetchone()
    return _collection(row) if row else None


def save_collection(
    conn: sqlite3.Connection,
    name: str,
    sql_dialect: str,
    tier_labels: dict[int, str],
    collection_id: int | None = None,
    group: str = "",
) -> int:
    """Create or update a collection. Raises sqlite3.IntegrityError if the name is taken in that group."""
    labels = json.dumps({str(k): v for k, v in sorted(tier_labels.items())})
    group = group.strip()
    with conn:
        if collection_id is None:
            cur = conn.execute(
                "INSERT INTO collection (group_name, name, sql_dialect, tier_labels_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (group, name, sql_dialect, labels, now()),
            )
            return cur.lastrowid
        conn.execute(
            "UPDATE collection SET group_name = ?, name = ?, sql_dialect = ?, tier_labels_json = ? WHERE id = ?",
            (group, name, sql_dialect, labels, collection_id),
        )
        return collection_id


def set_collection_pace(conn: sqlite3.Connection, collection_id: int, pace: str) -> None:
    with conn:
        conn.execute("UPDATE collection SET pace = ? WHERE id = ?", (pace, collection_id))


def delete_collection(conn: sqlite3.Connection, collection_id: int) -> int:
    """Permanently delete an archived collection with its cards, analyses and attempts. Returns the card count.

    Only an archived collection can be deleted, so it takes two deliberate steps.
    """
    collection = get_collection(conn, collection_id)
    if collection is None:
        raise ValueError("Collection not found")
    if not collection.archived:
        raise ValueError("Archive the collection before deleting it")
    cards = "SELECT id FROM card WHERE collection_id = ?"
    with conn:
        conn.execute(f"DELETE FROM attempt WHERE card_id IN ({cards})", (collection_id,))
        conn.execute(f"DELETE FROM card_analysis WHERE card_id IN ({cards})", (collection_id,))
        count = conn.execute("DELETE FROM card WHERE collection_id = ?", (collection_id,)).rowcount
        conn.execute("DELETE FROM collection WHERE id = ?", (collection_id,))
    return count


def list_groups(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT group_name FROM collection WHERE group_name != '' ORDER BY 1")]


def set_collection_archived(conn: sqlite3.Connection, collection_id: int, archived: bool) -> None:
    with conn:
        conn.execute("UPDATE collection SET archived = ? WHERE id = ?", (int(archived), collection_id))


def raise_unlocked_tier(conn: sqlite3.Connection, collection_id: int, tier: int) -> None:
    """Sticky unlock: max_unlocked_tier only ever goes up."""
    with conn:
        conn.execute(
            "UPDATE collection SET max_unlocked_tier = MAX(max_unlocked_tier, ?) WHERE id = ?",
            (tier, collection_id),
        )


# --- cards ---


def _card(row: sqlite3.Row) -> Card:
    return Card(
        id=row["id"],
        collection_id=row["collection_id"],
        tier=row["tier"],
        answer_format=row["answer_format"],
        prompt=row["prompt"],
        reference_answer=row["reference_answer"],
        rubric=json.loads(row["rubric_json"]),
        references=[Reference(**r) for r in json.loads(row["references_json"])],
        tags=json.loads(row["tags_json"]),
        archived=bool(row["archived"]),
        progress_reset_at=row["progress_reset_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        source_ref=row["source_ref"],
    )


def list_cards(
    conn: sqlite3.Connection, collection_id: int | None = None, include_archived: bool = False
) -> list[Card]:
    where, params = [], []
    if collection_id is not None:
        where.append("collection_id = ?")
        params.append(collection_id)
    if not include_archived:
        where.append("archived = 0")
    sql = "SELECT * FROM card" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY tier, created_at, id"
    return [_card(r) for r in conn.execute(sql, params)]


def get_card(conn: sqlite3.Connection, card_id: int) -> Card | None:
    row = conn.execute("SELECT * FROM card WHERE id = ?", (card_id,)).fetchone()
    return _card(row) if row else None


def _card_columns(collection_id: int, card: CardIn) -> dict[str, object]:
    return {
        "collection_id": collection_id,
        "tier": card.tier,
        "answer_format": card.answer_format,
        "prompt": card.prompt,
        "reference_answer": card.reference_answer,
        "rubric_json": json.dumps(card.rubric),
        "references_json": json.dumps([r.model_dump() for r in card.references]),
        "tags_json": json.dumps(card.tags),
        "archived": int(card.archived),
    }


def save_card(conn: sqlite3.Connection, collection_id: int, card: CardIn, card_id: int | None = None) -> tuple[int, bool]:
    """Insert or update a card. Returns (id, changed). `updated_at` only moves when content changes.

    For inserts, `card.id` is used as the new row's id when given (so re-importing an export is stable).
    """
    columns = _card_columns(collection_id, card)
    with conn:
        if card_id is not None:
            row = conn.execute("SELECT * FROM card WHERE id = ?", (card_id,)).fetchone()
            if all(row[k] == v for k, v in columns.items()):
                return card_id, False
            sets = ", ".join(f"{k} = ?" for k in columns)
            conn.execute(f"UPDATE card SET {sets}, updated_at = ? WHERE id = ?", (*columns.values(), now(), card_id))
            return card_id, True
        ts = now()
        columns = {"id": card.id, **columns, "created_at": ts, "updated_at": ts}
        names = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        cur = conn.execute(f"INSERT INTO card ({names}) VALUES ({marks})", tuple(columns.values()))
        return cur.lastrowid, True


def find_card_by_source(conn: sqlite3.Connection, source_ref: str) -> Card | None:
    row = conn.execute("SELECT * FROM card WHERE source_ref = ? ORDER BY id LIMIT 1", (source_ref,)).fetchone()
    return _card(row) if row else None


def set_card_source(conn: sqlite3.Connection, card_id: int, source_ref: str) -> None:
    with conn:
        conn.execute("UPDATE card SET source_ref = ? WHERE id = ?", (source_ref, card_id))


def move_card(conn: sqlite3.Connection, card_id: int, collection_id: int, tier: int) -> None:
    """Move a card to another collection/tier. Its content is unchanged, so `updated_at` (and with it
    the card's concept analysis) stays as is."""
    with conn:
        conn.execute("UPDATE card SET collection_id = ?, tier = ? WHERE id = ?", (collection_id, tier, card_id))


def set_card_archived(conn: sqlite3.Connection, card_id: int, archived: bool) -> None:
    with conn:
        conn.execute("UPDATE card SET archived = ? WHERE id = ?", (int(archived), card_id))


def reset_card_progress(conn: sqlite3.Connection, card_id: int) -> None:
    """Attempts before this moment stop counting toward progress. History is kept."""
    with conn:
        conn.execute("UPDATE card SET progress_reset_at = ? WHERE id = ?", (now(), card_id))


# --- concept analysis (sequencing) ---


def list_analyses(conn: sqlite3.Connection, collection_id: int) -> dict[int, Analysis]:
    rows = conn.execute(
        """SELECT a.* FROM card_analysis a JOIN card c ON c.id = a.card_id WHERE c.collection_id = ?""",
        (collection_id,),
    )
    return {
        r["card_id"]: Analysis(
            card_id=r["card_id"],
            introduced=json.loads(r["concepts_introduced_json"]),
            required=json.loads(r["concepts_required_json"]),
            difficulty=r["difficulty"],
            manual=bool(r["manual"]),
            analyzed_at=r["analyzed_at"],
        )
        for r in rows
    }


def save_analysis(
    conn: sqlite3.Connection, card_id: int, introduced: list[str], required: list[str],
    difficulty: int = 3, manual: bool = False,
) -> None:
    with conn:
        conn.execute(
            """INSERT INTO card_analysis (card_id, concepts_introduced_json, concepts_required_json, difficulty, manual, analyzed_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (card_id) DO UPDATE SET
                 concepts_introduced_json = excluded.concepts_introduced_json,
                 concepts_required_json = excluded.concepts_required_json,
                 difficulty = excluded.difficulty, manual = excluded.manual, analyzed_at = excluded.analyzed_at""",
            (card_id, json.dumps(introduced), json.dumps(required), max(1, min(difficulty, 5)), int(manual), now()),
        )


def set_analysis_manual(conn: sqlite3.Connection, card_id: int, manual: bool) -> None:
    with conn:
        conn.execute("UPDATE card_analysis SET manual = ? WHERE card_id = ?", (int(manual), card_id))


# --- attempts ---


def list_attempts(conn: sqlite3.Connection, collection_id: int) -> list[Attempt]:
    rows = conn.execute(
        """SELECT a.id, a.card_id, a.session_id, a.ts, a.outcome
           FROM attempt a JOIN card c ON c.id = a.card_id
           WHERE c.collection_id = ? ORDER BY a.id""",
        (collection_id,),
    )
    return [Attempt(**dict(r)) for r in rows]


def list_attempt_records(conn: sqlite3.Connection, collection_id: int | None = None) -> list[AttemptRecord]:
    """Full attempt rows (archived cards included), oldest first."""
    sql = """SELECT a.id, a.card_id, a.ts, a.outcome, a.answer, a.max_hint_level, a.llm_verdict,
                    a.llm_feedback, a.overridden, a.duration_ms
             FROM attempt a JOIN card c ON c.id = a.card_id"""
    params: tuple = ()
    if collection_id is not None:
        sql += " WHERE c.collection_id = ?"
        params = (collection_id,)
    return [AttemptRecord(**{**dict(r), "overridden": bool(r["overridden"])}) for r in conn.execute(sql + " ORDER BY a.id", params)]


def add_attempt(
    conn: sqlite3.Connection,
    card_id: int,
    session_id: str,
    outcome: str,
    answer: str | None = None,
    duration_ms: int | None = None,
    max_hint_level: int = 0,
    llm_verdict: str | None = None,
    llm_feedback: str | None = None,
    overridden: bool = False,
) -> int:
    with conn:
        cur = conn.execute(
            """INSERT INTO attempt (card_id, session_id, ts, outcome, answer, duration_ms,
                                    max_hint_level, llm_verdict, llm_feedback, overridden)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (card_id, session_id, now(), outcome, answer, duration_ms,
             max_hint_level, llm_verdict, llm_feedback, int(overridden)),
        )
        return cur.lastrowid


# --- JSON import/export ---


def export_collection(conn: sqlite3.Connection, collection_id: int) -> dict:
    collection = get_collection(conn, collection_id)
    cards = list_cards(conn, collection_id, include_archived=True)
    images = media.images_for(conn, *(t for c in cards for t in (c.prompt, c.reference_answer)))
    return {
        "collection": collection.name,
        **({"group": collection.group} if collection.group else {}),
        "sql_dialect": collection.sql_dialect,
        "tier_labels": {str(k): v for k, v in sorted(collection.tier_labels.items())},
        "cards": [
            {
                "id": c.id,
                "tier": c.tier,
                "answer_format": c.answer_format,
                "prompt": c.prompt,
                "reference_answer": c.reference_answer,
                "rubric": c.rubric,
                "references": [{"title": r.title, "url": r.url} for r in c.references],
                "tags": c.tags,
                "archived": c.archived,
            }
            for c in cards
        ],
        **({"media": {img.sha256: media.to_export(img) for img in images}} if images else {}),
    }


@dataclass
class ImportResult:
    collection_id: int | None = None
    collection_name: str = ""
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)


def import_collection(conn: sqlite3.Connection, data: object) -> ImportResult:
    """Validate the whole file first; only write if there are no errors.

    The collection is matched by group and name (created if new). Cards are matched by `id` when
    present. Embedded images are checked against their fingerprints before anything is written.
    """
    try:
        parsed = CollectionIn.model_validate(data)
    except ValidationError as exc:
        return ImportResult(errors=describe_errors(exc))

    existing = find_collection(conn, parsed.collection, parsed.group)
    errors = []
    images: dict[str, bytes] = {}
    for sha, item in parsed.media.items():
        try:
            images[sha] = media.decode_export(sha, item.model_dump())
        except media.MediaError as exc:
            errors.append(str(exc))
    seen_ids: set[int] = set()
    for i, card in enumerate(parsed.cards, start=1):
        if card.id is None:
            continue
        if card.id in seen_ids:
            errors.append(f"Card {i}, id: {card.id} appears more than once in this file")
        seen_ids.add(card.id)
        current = get_card(conn, card.id)
        if current and (existing is None or current.collection_id != existing.id):
            errors.append(f"Card {i}, id: {card.id} belongs to a different collection")
    if errors:
        return ImportResult(errors=errors)

    result = ImportResult(collection_name=parsed.collection)
    for sha, data in images.items():
        item = parsed.media[sha]
        media.save_image(conn, data, item.filename, item.alt, max_bytes=len(data))
    result.collection_id = save_collection(
        conn,
        parsed.collection,
        parsed.sql_dialect,
        parsed.tier_labels,
        collection_id=existing.id if existing else None,
        group=parsed.group,
    )
    for card in parsed.cards:
        exists = card.id is not None and get_card(conn, card.id) is not None
        _, changed = save_card(conn, result.collection_id, card, card_id=card.id if exists else None)
        if not exists:
            result.created += 1
        elif changed:
            result.updated += 1
        else:
            result.unchanged += 1
    return result
