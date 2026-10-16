import json
from pathlib import Path

import pytest

from learner import store
from learner.models import CardIn

STARTER = Path(__file__).parent.parent / "learner" / "starter"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.migrate(c)
    yield c
    c.close()


def sample(**card_overrides):
    card = {"tier": 1, "prompt": "What is 2 + 2?", "reference_answer": "4"} | card_overrides
    return {"collection": "Math", "tier_labels": {"1": "Basics"}, "cards": [card]}


def test_migrate_is_idempotent(conn):
    store.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(list(store.MIGRATIONS.glob("*.sql")))


def test_upgrading_a_version_1_database_keeps_its_data(tmp_path):
    old = store.connect(tmp_path / "old.db")
    old.executescript((store.MIGRATIONS / "001_init.sql").read_text() + "PRAGMA user_version = 1;")
    old.executescript("""
        INSERT INTO collection (id, name, created_at) VALUES (1, 'SQL', 't');
        INSERT INTO card (id, collection_id, tier, answer_format, default_mode, prompt, reference_answer, created_at, updated_at)
            VALUES (7, 1, 1, 'sql', 'practice', 'Q', 'A', 't', 't');
        INSERT INTO attempt (card_id, session_id, ts, mode, outcome, answer) VALUES (7, 's', 't', 'practice', 'correct', 'mine');
    """)
    store.migrate(old)
    assert store.get_card(old, 7).prompt == "Q"
    assert [a.outcome for a in store.list_attempts(old, 1)] == ["correct"]
    assert old.execute("SELECT answer FROM attempt").fetchone()[0] == "mine"
    old.close()


def test_import_creates_then_reimport_is_unchanged(conn):
    result = store.import_collection(conn, sample())
    assert not result.errors and result.created == 1
    exported = store.export_collection(conn, result.collection_id)
    again = store.import_collection(conn, exported)
    assert (again.created, again.updated, again.unchanged) == (0, 0, 1)
    assert store.get_collection(conn, result.collection_id).tier_labels == {1: "Basics"}


def test_import_reports_every_problem_with_position(conn):
    data = sample(tier=0, references=[{"title": "x", "url": "ftp://nope"}])
    data["cards"].append({"tier": 1, "prompt": "", "reference_answer": "a"})
    errors = store.import_collection(conn, data).errors
    assert any(e.startswith("Card 1, tier") for e in errors)
    assert any(e.startswith("Card 1, references 1 url") for e in errors)
    assert any(e.startswith("Card 2, prompt") for e in errors)
    assert store.list_collections(conn) == []


def test_import_rejects_id_from_another_collection(conn):
    first = store.import_collection(conn, sample())
    card_id = store.list_cards(conn, first.collection_id)[0].id
    other = sample(id=card_id)
    other["collection"] = "Other"
    assert "belongs to a different collection" in store.import_collection(conn, other).errors[0]


def test_updated_at_only_moves_when_content_changes(conn):
    cid = store.save_collection(conn, "C", "mysql", {})
    card_id, _ = store.save_card(conn, cid, CardIn(tier=1, prompt="p", reference_answer="a"))
    before = store.get_card(conn, card_id).updated_at
    _, changed = store.save_card(conn, cid, CardIn(tier=1, prompt="p", reference_answer="a"), card_id)
    assert not changed and store.get_card(conn, card_id).updated_at == before
    _, changed = store.save_card(conn, cid, CardIn(tier=1, prompt="p2", reference_answer="a"), card_id)
    assert changed and store.get_card(conn, card_id).updated_at > before


def test_unlock_tier_never_decreases(conn):
    cid = store.save_collection(conn, "C", "mysql", {})
    store.raise_unlocked_tier(conn, cid, 3)
    store.raise_unlocked_tier(conn, cid, 2)
    assert store.get_collection(conn, cid).max_unlocked_tier == 3


@pytest.mark.parametrize("path", sorted(STARTER.glob("*.json")), ids=lambda p: p.name)
def test_starter_collections_import_cleanly(conn, path):
    result = store.import_collection(conn, json.loads(path.read_text()))
    assert result.errors == [] and result.created >= 10
