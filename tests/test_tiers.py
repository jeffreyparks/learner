"""The tier rule (DESIGN.md §12.5): tiers are 1…N, one label entry each, cards and labels change together."""

import re

import pytest
from fastapi.testclient import TestClient

from learner import engine, store, tiers
from learner.app import create_app
from learner.config import Config
from learner.models import CardIn


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.migrate(c)
    yield c
    c.close()


def collection(conn, labels=None, cards=(1, 2, 3)):
    cid = store.save_collection(conn, "C", "mysql", labels if labels is not None else {1: "A", 2: "B", 3: "C"})
    for tier in cards:
        store.save_card(conn, cid, CardIn(tier=tier, prompt=f"Q{tier}", reference_answer="x"))
    return cid


def layout(conn, cid):
    """(labels, {tier: [card prompts]}, max unlocked) — what the rule protects."""
    c = store.get_collection(conn, cid)
    by_tier = {}
    for card in store.list_cards(conn, cid, include_archived=True):
        by_tier.setdefault(card.tier, []).append(card.prompt + ("*" if card.archived else ""))
    return c.tier_labels, by_tier, c.max_unlocked_tier


def test_tidy_closes_gaps_and_adds_missing_labels(conn):
    cid = collection(conn, labels={1: "A", 4: "D"}, cards=(1, 3, 4))
    store.raise_unlocked_tier(conn, cid, 3)
    tiers.tidy(conn, cid)
    labels, by_tier, unlocked = layout(conn, cid)
    assert labels == {1: "A", 2: "", 3: "D"}  # tier 3 (no label) became 2; tier 4 became 3
    assert by_tier == {1: ["Q1"], 2: ["Q3"], 3: ["Q4"]}
    assert unlocked == 2  # what was unlocked (old 1–3) is still unlocked (new 1–2)


def test_swap_moves_cards_and_labels_together_and_keeps_unlocks(conn):
    cid = collection(conn)
    store.raise_unlocked_tier(conn, cid, 2)
    tiers.swap(conn, cid, 2, 3)
    labels, by_tier, unlocked = layout(conn, cid)
    assert labels == {1: "A", 2: "C", 3: "B"} and by_tier == {1: ["Q1"], 2: ["Q3"], 3: ["Q2"]}
    assert unlocked == 3  # B was unlocked and is now tier 3: still unlocked


def test_move_tier_cards_keeps_the_empty_tier(conn):
    cid = collection(conn)
    assert tiers.move_tier_cards(conn, cid, 3, 1) == 1
    labels, by_tier, _ = layout(conn, cid)
    assert labels == {1: "A", 2: "B", 3: "C"} and by_tier == {1: ["Q1", "Q3"], 2: ["Q2"]}


def test_delete_archives_its_cards_and_renumbers(conn):
    cid = collection(conn)
    store.raise_unlocked_tier(conn, cid, 3)
    assert tiers.delete(conn, cid, 2) == 1
    labels, by_tier, unlocked = layout(conn, cid)
    assert labels == {1: "A", 2: "C"} and by_tier[2] == ["Q3"] and unlocked == 2
    parked = [c for c in store.list_cards(conn, cid, include_archived=True) if c.archived]
    assert [c.prompt for c in parked] == ["Q2"] and parked[0].tier == tiers.PARKED_TIER
    # Unarchiving puts it in the last tier.
    store.set_card_archived(conn, parked[0].id, False)
    assert tiers.place_unarchived(conn, parked[0].id) and store.get_card(conn, parked[0].id).tier == 2
    assert store.get_collection(conn, cid).tier_labels == {1: "A", 2: "C"}  # no phantom tier appeared


def test_add_and_bulk_move_into_a_new_tier(conn):
    cid = collection(conn)
    assert tiers.add(conn, cid, "D") == 4
    first = store.list_cards(conn, cid)[0]
    tiers.move_cards(conn, [first.id], cid, 5)  # 5 = "+ New tier at the end"
    labels, by_tier, _ = layout(conn, cid)
    assert labels == {1: "A", 2: "B", 3: "C", 4: "D", 5: ""} and by_tier[5] == ["Q1"]
    with pytest.raises(tiers.TierError):
        tiers.move_cards(conn, [first.id], cid, 9)


def test_empty_tier_never_blocks_unlocking(conn):
    cid = collection(conn, cards=(1, 3))
    c = store.get_collection(conn, cid)
    cards = store.list_cards(conn, cid)
    progress = {card.id: engine.Progress(engine.KNOWN if card.tier == 1 else engine.NEW) for card in cards}
    assert engine.unlocked_through(cards, progress, 1, 0.8, sorted(c.tier_labels)) == 3  # 1 known → 2 (empty) → 3
    summaries = engine.tier_summaries(cards, progress, 3, sorted(c.tier_labels))
    assert [s.total for s in summaries] == [1, 0, 1]
    assert engine.default_tier(summaries) == 3


# --- screens ---


def client_for(tmp_path):
    return TestClient(create_app(Config(db_path=str(tmp_path / "w.db"), provider="off")), base_url="http://127.0.0.1")


def test_tiers_panel_actions(tmp_path):
    client = client_for(tmp_path)
    page = client.get("/collections/1").text  # starter Python: Foundations, Intermediate
    assert 'name="label_1" value="Foundations"' in page and "tier_labels" not in page
    done = client.post("/collections/1/tiers", data={"label_1": "Basics", "label_2": "Intermediate", "op": "down:1"}).text
    assert "Moved tier 1 down." in done
    assert 'name="label_1" value="Intermediate"' in done and 'name="label_2" value="Basics"' in done  # rename + swap
    done = client.post("/collections/1/tiers", data={"label_1": "Intermediate", "label_2": "Basics", "op": "add", "new_label": "Advanced"}).text
    assert 'name="label_3" value="Advanced"' in done and "empty" in done
    done = client.post("/collections/1/tiers", data={"label_1": "Intermediate", "label_2": "Basics", "label_3": "Advanced", "op": "delete:1"}).text
    assert re.search(r"Deleted tier 1 and archived its \d+ cards", done)
    assert 'name="label_1" value="Basics"' in done


def test_card_form_offers_real_tiers_and_a_new_one(tmp_path):
    client = client_for(tmp_path)
    form = client.get("/cards/new?collection_id=1").text
    assert '<option value="2"' in form and "+ New tier at the end" in form
    base = {"collection_id": "1", "answer_format": "text", "prompt": "Q?", "reference_answer": "A"}
    assert "Choose one of the collection" in client.post("/cards/new", data=base | {"tier": "7"}).text
    client.post("/cards/new", data=base | {"tier": "new"})
    assert 'name="label_3"' in client.get("/collections/1").text  # the new tier exists, with a label slot


def test_bulk_move_on_the_cards_page(tmp_path):
    client = client_for(tmp_path)
    page = client.get("/browse?collection_id=1&tier=1").text
    ids = re.findall(r'name="card_ids" value="(\d+)"', page)[:2]
    moved = client.post("/cards/move", data={"to_collection": "1", "card_ids": ids, "to": "2", "back": "collection_id=1&tier=1"})
    assert "Moved 2 cards." in moved.text
    assert 'name="card_ids"' in client.get("/browse").text  # checkboxes work across all collections


def test_bulk_move_to_another_collection_and_tier(tmp_path):
    client = client_for(tmp_path)
    page = client.get("/browse?collection_id=1&tier=1").text
    ids = re.findall(r'name="card_ids" value="(\d+)"', page)[:2]
    moved = client.post("/cards/move", data={"to_collection": "2", "card_ids": ids, "to": "2", "back": "collection_id=1&tier=1"})
    assert "Moved 2 cards." in moved.text
    conn = store.connect(tmp_path / "w.db")
    assert {store.get_card(conn, int(i)).collection_id for i in ids} == {2}
    assert {store.get_card(conn, int(i)).tier for i in ids} == {2}
    assert store.get_collection(conn, 1).tier_labels == {1: "Foundations", 2: "Intermediate"}  # source keeps its tiers
    conn.close()


def test_reset_restores_starter_and_keeps_a_backup(tmp_path):
    client = client_for(tmp_path)
    conn = store.connect(tmp_path / "w.db")
    store.save_collection(conn, "Extra", "mysql", {1: "A"})
    store.add_attempt(conn, store.list_cards(conn, 1)[0].id, "s1", "correct", answer="x")
    conn.close()
    assert "Type reset" in client.post("/reset", data={"confirm": "no"}).text
    done = client.post("/reset", data={"confirm": "Reset"})
    assert "Back to the starter collections" in done.text
    conn = store.connect(tmp_path / "w.db")
    assert [c.name for c in store.list_collections(conn)] == ["Python", "SQL"]
    assert conn.execute("SELECT COUNT(*) FROM attempt").fetchone()[0] == 0
    conn.close()
    assert len(list((tmp_path / "backups").glob("learner-*.db"))) == 1


def test_retier_starts_afresh_and_drops_empty_tiers(conn):
    cid = collection(conn, labels={1: "Old A", 2: "Old B", 3: "Old C"}, cards=(1, 2, 3, 3))
    store.raise_unlocked_tier(conn, cid, 3)
    ids = [c.id for c in store.list_cards(conn, cid)]
    shelved = store.save_card(conn, cid, CardIn(tier=3, prompt="Shelved", reference_answer="x", archived=True))
    # Five proposed names, but nothing is assigned to tiers 2 and 4: they disappear and the rest close up.
    changed = tiers.retier(conn, cid, ["Fundamentals", "Basics", "Intermediate", "Unused", "Advanced"],
                           {ids[0]: 3, ids[1]: 1, ids[2]: 1, ids[3]: 5})
    labels, _, unlocked = layout(conn, cid)
    assert labels == {1: "Fundamentals", 2: "Intermediate", 3: "Advanced"}
    assert unlocked == 1  # unlocks re-open from progress, not from the old layout
    assert [store.get_card(conn, i).tier for i in ids] == [2, 1, 1, 3]
    assert store.get_card(conn, shelved).tier == 3  # archived cards stay inside the new range
    assert changed == 3


def test_retier_rejects_a_card_without_a_listed_tier(conn):
    cid = collection(conn)
    first = store.list_cards(conn, cid)[0].id
    with pytest.raises(tiers.TierError):
        tiers.retier(conn, cid, ["Fundamentals", "Advanced"], {first: 3})
    with pytest.raises(tiers.TierError):
        tiers.retier(conn, cid, [], {first: 1})
    assert layout(conn, cid)[0] == {1: "A", 2: "B", 3: "C"}  # nothing changed


def test_ai_tiers_are_clamped_and_skipped_cards_keep_their_place(conn):
    from learner import tutor
    from learner.llm import FakeProvider
    cid = collection(conn, cards=(1, 2, 3, 4))
    cards = store.list_cards(conn, cid)
    reply = {"tiers": ["Fundamentals", " ", "Advanced", "Fundamentals"],
             "cards": [{"card_id": cards[0].id, "tier": 0}, {"card_id": cards[1].id, "tier": 9},
                       {"card_id": 999, "tier": 1}]}  # unknown id ignored; cards 3 and 4 skipped
    result = tutor.suggest_tiers(FakeProvider({"sequence": reply}), "C", cards)
    assert result.labels == ["Fundamentals", "Advanced"]  # blanks and repeats dropped
    assert result.tiers == {cards[0].id: 1, cards[1].id: 2, cards[2].id: 2, cards[3].id: 2}


def test_retier_screen_previews_then_applies(tmp_path):
    from learner.llm import FakeProvider
    client = TestClient(create_app(Config(db_path=str(tmp_path / "r.db"), provider="off"), FakeProvider()),
                        base_url="http://127.0.0.1")
    c = store.connect(tmp_path / "r.db")
    cid = collection(c, labels={1: "Old"}, cards=(1,) * 6)
    preview = client.post(f"/collections/{cid}/retier").text
    assert 'value="Fundamentals"' in preview and 'value="Advanced"' in preview and "Nothing changes until you apply" in preview
    assert store.get_collection(c, cid).tier_labels == {1: "Old"}  # the preview changes nothing
    form = {"label_1": "Fundamentals", "label_2": "Intermediate", "label_3": "Advanced"}
    for n, card in enumerate(store.list_cards(c, cid)):
        form[f"tier_{card.id}"] = str(1 + n // 2)
    client.post(f"/collections/{cid}/retier/apply", data=form)
    assert store.get_collection(c, cid).tier_labels == {1: "Fundamentals", 2: "Intermediate", 3: "Advanced"}
    assert [x.tier for x in store.list_cards(c, cid)] == [1, 1, 2, 2, 3, 3]
    c.close()
