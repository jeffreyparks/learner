import re

import pytest
from fastapi.testclient import TestClient

from learner import organize, store
from learner.app import create_app
from learner.config import Config
from learner.llm import FakeProvider
from learner.models import CardIn

NAMES = ["Causal Inference", "Causal Inference Fundamentals", "Causal Inference Directed Acyclic Graphs",
         "Causal Inference Potential Outcomes", "Experiments"]


def seed(conn):
    ids = {}
    for n, name in enumerate(NAMES):
        cid = store.save_collection(conn, name, "mysql", {}, group="Data science")
        for i in range(2):
            store.save_card(conn, cid, CardIn(tier=1, prompt=f"{name} question {i}", reference_answer="answer"))
        ids[name] = cid
    return ids


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "o.db")
    store.migrate(c)
    yield c
    c.close()


def test_suggested_name_and_short_labels(conn):
    ids = seed(conn)
    chosen = [store.get_collection(conn, ids[n]) for n in NAMES]
    assert organize.suggested_name(chosen) == "Causal Inference"  # "Experiments" breaks the shared prefix
    assert organize.suggested_name(chosen[1:4]) == "Causal Inference"
    rows = organize.rows_for(conn, list(ids.values()))
    mapping = organize.default_mapping(rows, "Causal Inference")
    assert [(m.tier, m.label) for m in mapping.values()] == [
        (1, "Basics"), (2, "Fundamentals"), (3, "Directed Acyclic Graphs"), (4, "Potential Outcomes"), (5, "Experiments")]


def test_combine_into_one_keeps_progress_and_archives_the_rest(conn):
    ids = seed(conn)
    card = store.list_cards(conn, ids["Experiments"])[0]
    store.add_attempt(conn, card.id, "s1", "correct", answer="mine")
    rows = organize.rows_for(conn, list(ids.values()))
    mapping = organize.default_mapping(rows, "Causal Inference")
    # Merge Fundamentals into Basics (tier 1), and make everything else follow.
    mapping[f"{ids['Causal Inference Fundamentals']}_1"].tier = 1
    for name, tier in (("Causal Inference Directed Acyclic Graphs", 2), ("Causal Inference Potential Outcomes", 3), ("Experiments", 4)):
        mapping[f"{ids[name]}_1"].tier = tier
    result = organize.apply(conn, rows, mapping, "Causal Inference", "Data science")

    target = store.get_collection(conn, result.collection_id)
    assert result.collection_id == ids["Causal Inference"]  # combined in place
    assert target.tier_labels == {1: "Basics + Fundamentals", 2: "Directed Acyclic Graphs", 3: "Potential Outcomes", 4: "Experiments"}
    assert len(store.list_cards(conn, target.id)) == 10 and result.moved == 8
    assert store.list_attempts(conn, target.id)[0].card_id == card.id  # history came along
    assert store.get_card(conn, card.id).tier == 4
    assert sorted(result.archived) == sorted(f"Data science › {n}" for n in NAMES[1:])
    assert [c.name for c in store.list_collections(conn)] == ["Causal Inference"]


def test_retier_a_single_collection(conn):
    cid = store.save_collection(conn, "SQL", "mysql", {1: "A", 2: "B", 3: "C"})
    for tier in (1, 2, 3):
        store.save_card(conn, cid, CardIn(tier=tier, prompt=f"Q{tier}", reference_answer="x"))
    rows = organize.rows_for(conn, [cid])
    mapping = organize.default_mapping(rows, "SQL")
    mapping[f"{cid}_3"].tier = 2  # fold C into B's tier
    organize.apply(conn, rows, mapping, "SQL", "")
    assert sorted(c.tier for c in store.list_cards(conn, cid)) == [1, 2, 2]
    assert store.get_collection(conn, cid).tier_labels == {1: "A", 2: "B + C"}


def test_examples_are_plain_text():
    assert organize.example("Which chart?\n\n![Bar chart](/media/abc) **bold** `code`") == "Which chart? bold code"


def test_refuses_to_mix_in_an_unrelated_collection(conn):
    ids = seed(conn)
    rows = organize.rows_for(conn, [ids["Experiments"]])
    with pytest.raises(organize.OrganizeError, match="already exists"):
        organize.apply(conn, rows, organize.default_mapping(rows, "X"), "Causal Inference", "Data science")


def test_combine_screens_with_ai_suggestion(tmp_path):
    client = TestClient(create_app(Config(db_path=str(tmp_path / "w.db"), provider="off"), FakeProvider()),
                        base_url="http://127.0.0.1")
    conn = store.connect(tmp_path / "w.db")
    ids = seed(conn)
    conn.close()
    assert "Pick the collections to combine" in client.get("/collections/organize").text
    query = "&".join(f"ids={ids[n]}" for n in NAMES)
    page = client.get(f"/collections/organize?{query}").text
    assert 'name="name" value="Causal Inference"' in page and 'value="Directed Acyclic Graphs"' in page

    form = {"ids": [str(ids[n]) for n in NAMES], "name": "Causal Inference", "group": "Data science", "suggest": "1"}
    suggested = client.post("/collections/organize/preview", data=form).text
    first_row = re.search(r'<tr>\s*<td><select name="tier_(\d+)_1"', suggested)[1]
    assert int(first_row) == ids["Experiments"]  # the fake AI reverses the order, so Experiments is now tier 1

    done = client.post("/collections/organize/apply", data={k: v for k, v in form.items() if k != "suggest"}).text
    assert "Data science › Causal Inference</strong> now has 5 tiers" in done and "Emptied and archived" in done
