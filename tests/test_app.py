import io
import zipfile
import json
import re

import pytest
from fastapi.testclient import TestClient

from learner import store
from learner.app import create_app
from learner.config import Config
from learner.llm import FakeProvider


def make_client(tmp_path, provider=None, **settings):
    # provider="off" unless a FakeProvider is passed: tests must never reach the real API.
    cfg = Config(db_path=str(tmp_path / "app.db"), session_size=4, new_per_session=3, provider="off", **settings)
    return TestClient(create_app(cfg, provider), base_url="http://127.0.0.1")


@pytest.fixture
def client(tmp_path):
    return make_client(tmp_path)


def last_attempt(tmp_path):
    conn = store.connect(tmp_path / "app.db")
    try:
        return dict(conn.execute("SELECT * FROM attempt ORDER BY id DESC LIMIT 1").fetchone())
    finally:
        conn.close()


def start_session(client, collection_id=1, tier=1):
    response = client.get(f"/study/{collection_id}/{tier}", follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"]


def card_id_on(page_html):
    return int(re.search(r'name="card_id" value="(\d+)"', page_html)[1])


def test_first_run_loads_starter_collections(client):
    html = client.get("/").text
    assert "Python" in html and "SQL" in html and "Tier 1 · Foundations" in html


def test_session_runs_to_summary(client):
    url = start_session(client)
    seen = []
    for _ in range(10):
        page = client.get(url)
        if "Session complete" in page.text:
            break
        card_id = card_id_on(page.text)
        seen.append(card_id)
        client.post(url, data={"card_id": card_id, "result": "right"})
    assert "Session complete" in client.get(url + "/summary").text
    assert len(seen) == 3  # new_per_session caps the first session


def test_without_llm_every_card_is_self_marked(client):
    html = client.get(start_session(client)).text
    assert 'id="answer-input"' in html and 'data-llm="off"' in html
    assert 'id="hint-button"' not in html and "you mark your own answers" in html


def test_show_answer_is_offered_and_records_a_peek(client, tmp_path):
    url = start_session(client)
    html = client.get(url).text
    assert 'id="show-answer" class="bad"' in html and "no effect on your score" in html
    card_id = card_id_on(html)
    # What the Next button sends; a stray verdict is ignored, since nothing was graded.
    client.post(url, data={"card_id": card_id, "result": "revealed", "answer": "", "llm_verdict": "correct"})
    row = last_attempt(tmp_path)
    assert (row["outcome"], row["llm_verdict"], row["overridden"]) == ("revealed", None, 0)


def test_self_marked_attempt_records_no_verdict(client, tmp_path):
    url = start_session(client)
    card_id = card_id_on(client.get(url).text)
    client.post(url, data={"card_id": card_id, "result": "wrong", "answer": "no idea"})
    row = last_attempt(tmp_path)
    assert (row["outcome"], row["llm_verdict"], row["overridden"], row["answer"]) == ("incorrect", None, 0, "no idea")


def test_hint_turns_right_into_assisted_and_override_is_logged(tmp_path):
    client = make_client(tmp_path, FakeProvider())
    url = start_session(client)
    card_id = card_id_on(client.get(url).text)
    client.post(url, data={"card_id": card_id, "result": "right", "max_hint_level": "1",
                           "llm_verdict": "incorrect", "llm_feedback": "Missing the key point."})
    row = last_attempt(tmp_path)
    assert (row["outcome"], row["max_hint_level"], row["llm_verdict"], row["overridden"]) == ("assisted", 1, "incorrect", 1)


def test_grade_endpoint_uses_the_provider(tmp_path):
    provider = FakeProvider()
    client = make_client(tmp_path, provider)
    page = client.get(start_session(client)).text
    assert "Graded by AI" in page and "Fake" not in page  # the label never names a provider
    card_id = card_id_on(page)
    reference = client.get(f"/collections/1/export").json()["cards"][0]["reference_answer"]
    grade = client.post("/grade", json={"card_id": card_id, "answer": reference}).json()
    assert grade["verdict"] == "correct" and grade["right"] is True
    assert client.post("/grade", json={"card_id": card_id, "answer": "   "}).json() == {"error": "Nothing to grade."}
    assert len(provider.calls) == 1  # the blank answer never reached the LLM


def test_grade_failure_falls_back_with_a_message(tmp_path):
    client = make_client(tmp_path, FakeProvider(fail=True))
    card_id = card_id_on(client.get(start_session(client)).text)
    assert "error" in client.post("/grade", json={"card_id": card_id, "answer": "x"}).json()


def test_hint_endpoint_passes_level_and_earlier_hints(tmp_path):
    provider = FakeProvider()
    client = make_client(tmp_path, provider)
    card_id = card_id_on(client.get(start_session(client)).text)
    reply = client.post("/hint", json={"card_id": card_id, "answer": "", "level": 2, "earlier": ["first hint"]}).json()
    assert reply["level"] == 2 and "level 2" in reply["hint"]
    task, user = provider.calls[-1]
    assert task == "hint" and "1. first hint" in user and "(nothing written yet)" in user


def test_locked_tier_warns_before_starting(client):
    response = client.get("/study/1/2")
    assert "isn't unlocked yet" in response.text
    assert client.get("/study/1/2?confirm=1", follow_redirects=False).status_code == 303


def test_create_card_with_reference_and_errors(client):
    bad = client.post("/cards/new", data={"collection_id": "1", "tier": "1", "prompt": "Q", "reference_answer": "A", "references": "Docs | not-a-url"})
    assert "must be a web address" in bad.text
    good = client.post(
        "/cards/new",
        data={"collection_id": "1", "tier": "1", "answer_format": "text",
              "prompt": "Q?", "reference_answer": "A", "references": "Docs | https://example.com/docs", "tags": "x, y"},
    )
    assert "Saved." in good.text and "https://example.com/docs" in good.text


def test_export_then_import_round_trip(client):
    exported = client.get("/collections/1/export")
    assert exported.headers["content-disposition"].endswith('.json"')
    result = client.post("/import", data={"text": exported.text})
    assert re.search(r"0 added · 0 updated · \d+ unchanged", result.text)


def test_delete_collection_needs_archiving_first(client, tmp_path):
    start_session(client)  # gives collection 1 some study history
    assert client.post("/collections/1/delete", follow_redirects=False).status_code == 409
    assert client.get("/collections/1").status_code == 200
    assert "Delete permanently" not in client.get("/collections/1").text

    client.post("/collections/1/archive")
    assert "Delete permanently" in client.get("/collections/1").text
    assert client.post("/collections/1/delete", follow_redirects=False).status_code == 303
    assert client.get("/collections/1").status_code == 404
    conn = store.connect(tmp_path / "app.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM card WHERE collection_id = 1").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0] >= 1  # others untouched
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_browse_filters_by_status(client):
    html = client.get("/browse?status=new").text
    assert "new</span>" in html and "known</span>" not in html


def test_check_endpoint_returns_diagnostics(client):
    bad = client.post("/check", json={"format": "sql", "dialect": "mysql", "code": "SELECT FROM WHERE"}).json()
    assert bad["diagnostics"] and bad["diagnostics"][0]["severity"] == "error"
    good = client.post("/check", json={"format": "python", "code": "x = [n * n for n in nums]"}).json()
    assert good == {"diagnostics": []}
    assert client.post("/check", json={"format": "cobol", "code": "x"}).status_code == 400


def test_code_answers_get_an_editor(client):
    html = client.get("/cards/2").text  # starter Python card 2 is a practice code card
    assert 'id="reference-answer"' in html and 'data-dialect=' in html


def test_resubmitting_the_same_card_records_one_attempt(client, tmp_path):
    url = start_session(client)
    card_id = card_id_on(client.get(url).text)
    for _ in range(3):  # e.g. a held-down key, or the back button
        client.post(url, data={"card_id": card_id, "result": "right"})
    conn = store.connect(tmp_path / "app.db")
    assert conn.execute("SELECT COUNT(*) FROM attempt WHERE card_id = ?", (card_id,)).fetchone()[0] == 1
    conn.close()


def test_other_hosts_and_cross_site_writes_are_refused(client):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
    attack = client.post("/cards/1/archive", headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert attack.status_code == 403
    assert client.post("/cards/1/archive", headers={"origin": "http://127.0.0.1:8765"}, follow_redirects=False).status_code == 303


# --- card order (Phase 3) ---


def card_form(client, card_id):
    html = client.get(f"/cards/{card_id}").text
    value = lambda name: re.search(rf'name="{name}" value="([^"]*)"', html)[1]
    return html, value


def test_rebuild_orders_every_tier_and_shows_concepts(tmp_path):
    provider = FakeProvider()
    client = make_client(tmp_path, provider)
    assert "to order</span>" in client.get("/").text  # starter cards haven't been ordered yet
    page = client.post("/collections/2/order/rebuild").text
    assert "Order rebuilt." in page and "not ordered yet" not in page
    assert [t for t, _ in provider.calls] == ["sequence", "sequence"]  # SQL has two tiers
    assert "concepts_from_lower_tiers>\n(none)" in provider.calls[0][1]
    assert "(none)" not in provider.calls[1][1].split("<existing_concepts>")[0]  # tier 2 sees tier 1's concepts
    assert 'class="chip teach">joins' in page
    assert "to order</span>" not in client.get("/").text.split("SQL")[1]


def test_rebuild_failure_is_reported_and_keeps_going(tmp_path):
    client = make_client(tmp_path, FakeProvider(fail=True))
    page = client.post("/collections/2/order/rebuild").text
    assert "couldn't be ordered" in page and "Tier 1" in page and "Tier 2" in page


def test_without_ai_the_order_page_explains(client):
    page = client.get("/collections/2/order").text
    assert "Ordering cards needs AI" in page and "Rebuild order" not in page


def test_typed_concepts_are_kept_through_a_rebuild(tmp_path):
    client = make_client(tmp_path, FakeProvider())
    client.post("/collections/2/order/rebuild")
    html, value = card_form(client, 15)
    form = {"collection_id": "2", "tier": "1", "answer_format": "text", "prompt": "Q?", "reference_answer": "A",
            "concepts_introduced": "Where vs Having", "concepts_required": "filtering"}
    client.post("/cards/15", data=form)
    client.post("/collections/2/order/rebuild")
    html, value = card_form(client, 15)
    assert value("concepts_introduced") == "where-vs-having" and value("concepts_required") == "filtering"
    assert 'name="concepts_locked" value="1" checked' in html
    # Unticking "keep" hands the card back to the AI at the next rebuild.
    client.post("/cards/15", data=form | {"concepts_introduced": "where-vs-having"})
    client.post("/collections/2/order/rebuild")
    assert card_form(client, 15)[1]("concepts_introduced") != "where-vs-having"


def test_list_titles_drop_markdown_but_keep_identifiers():
    from learner.app import plain_title
    assert plain_title("## Table `orders(customer_id, created_at)`.\n\nMore") == "Table orders(customer_id, created_at)."
    assert plain_title("What is **`dict.get`**?") == "What is dict.get?"


def test_stats_page(client):
    assert "No answers yet" in client.get("/stats").text
    url = start_session(client)
    client.post(url, data={"card_id": card_id_on(client.get(url).text), "result": "right"})
    page = client.get("/stats").text
    assert "Answers per day" in page and ">1</div><div class=\"tile-label\">answers" in page
    assert "No AI-graded answers yet" in page
    assert client.get("/stats?collection_id=2").status_code == 200


# --- authoring assistant ---


def test_draft_then_save_only_ticked_cards_and_links(tmp_path):
    client = make_client(tmp_path, FakeProvider())
    page = client.post("/cards/draft", data={"collection_id": "2", "notes": "COALESCE returns the first non-null value. NULLIF does the reverse.", "count": "2"}).text
    assert "(Fake draft 1)" in page and "(Fake draft 2)" in page
    drafts = re.findall(r"name=\"(draft_\d)\" value='([^']*)'", page)
    assert len(drafts) == 2
    import html as html_lib
    data = {"collection_id": "2", "keep_0": "1", "refs_0": "1", "keep_1": "1"} | {k: html_lib.unescape(v) for k, v in drafts}
    saved = client.post("/cards/draft/save", data=data).text
    assert "Saved 2 cards" in saved
    export = client.get("/collections/2/export").json()["cards"]
    first, second = [c for c in export if c["prompt"].startswith("(Fake draft")]
    assert first["references"] and not second["references"]  # links only kept when ticked
    # The AI's concepts are saved, so new drafts are ordered without a rebuild.
    assert "draft-idea-1" in client.get("/collections/2/order").text


def test_missing_concept_links_to_a_draft(tmp_path):
    client = make_client(tmp_path, FakeProvider({"sequence": lambda user: {"cards": [
        {"card_id": int(cid), "concepts_introduced": [], "concepts_required": ["window-functions"], "difficulty": 2}
        for cid in re.findall(r'<card id="(\d+)"', user)]}}))
    client.post("/collections/2/order/rebuild")
    order = client.get("/collections/2/order").text
    assert "/cards/draft?collection_id=2&amp;concept=window-functions&amp;tier=1" in order
    page = client.get("/cards/draft?collection_id=2&concept=window-functions&tier=1").text
    assert "Write a card that teaches “window-functions”" in page


def test_drafting_without_ai(client):
    page = client.get("/cards/draft").text
    assert "Drafting cards needs AI" in page and 'id="draft-form"' not in page


def test_new_collection_from_the_card_form(client):
    base = {"tier": "1", "answer_format": "text", "prompt": "What is a tensor?", "reference_answer": "An n-dimensional array."}
    assert "Give the new collection a name" in client.post("/cards/new", data=base | {"collection_id": "new"}).text
    assert "already exists" in client.post("/cards/new", data=base | {"collection_id": "new", "new_collection": "SQL"}).text
    saved = client.post("/cards/new", data=base | {"collection_id": "new", "new_collection": "Machine Learning"})
    assert "Saved." in saved.text
    assert "Machine Learning" in client.get("/").text


def test_no_ai_messages_explain_the_fix(client, tmp_path, monkeypatch):
    # Switched off on purpose: point at the setting.
    assert 'provider = "off"' in client.get("/cards/draft").text
    # No key found: explain the .env file.
    monkeypatch.setattr("learner.llm.has_claude_credentials", lambda: False)
    no_key = TestClient(create_app(Config(db_path=str(tmp_path / "k.db"))), base_url="http://127.0.0.1")
    for url in ("/cards/draft", "/collections/2/order"):
        page = no_key.get(url).text
        assert "needs AI, and none is set up" in page and "ANTHROPIC_API_KEY=your-key" in page


def test_env_file_fills_in_missing_variables_only(tmp_path, monkeypatch):
    from learner.config import load_env_file
    env = tmp_path / ".env"
    env.write_text('# comment\nexport LEARNER_TEST_A="from-file"\nLEARNER_TEST_B=from-file\nnot a line\n')
    monkeypatch.delenv("LEARNER_TEST_A", raising=False)
    monkeypatch.setenv("LEARNER_TEST_B", "already-set")
    assert load_env_file(env) == ["LEARNER_TEST_A"]
    import os
    assert os.environ["LEARNER_TEST_A"] == "from-file" and os.environ["LEARNER_TEST_B"] == "already-set"
    monkeypatch.delenv("LEARNER_TEST_A")


# --- images and groups (Phase 5) ---

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image body"


def upload(client, data=PNG, alt="A bar chart"):
    return client.post("/media", files={"file": ("chart.png", data, "image/png")}, data={"alt": alt})


def test_upload_and_serve_an_image(client):
    reply = upload(client).json()
    assert reply["markdown"] == f"![A bar chart]({reply['url']})"
    served = client.get(reply["url"])
    assert served.content == PNG and served.headers["content-type"] == "image/png"
    assert "immutable" in served.headers["cache-control"] and served.headers["x-content-type-options"] == "nosniff"
    assert upload(client, b"<svg onload=alert(1)>").status_code == 400
    assert client.get("/media/" + "0" * 64).status_code == 404


def test_grader_and_hints_see_the_cards_images(tmp_path):
    provider = FakeProvider()
    client = make_client(tmp_path, provider)
    image = upload(client).json()
    form = {"collection_id": "1", "tier": "1", "answer_format": "text",
            "prompt": f"What kind of chart is this?\n\n{image['markdown']}", "reference_answer": "A bar chart"}
    card_id = int(client.post("/cards/new", data=form, follow_redirects=False).headers["location"].split("/")[2].split("?")[0])
    client.post("/grade", json={"card_id": card_id, "answer": "bar chart"})
    client.post("/hint", json={"card_id": card_id, "answer": ""})
    assert provider.images == [[("image/png", PNG)], [("image/png", PNG)]]


def test_claude_receives_images_before_the_text():
    from learner.llm import user_content
    blocks = user_content("Grade this", [("image/png", PNG)])
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[-1] == {"type": "text", "text": "Grade this"}
    assert user_content("Just text", []) == "Just text"


def test_export_import_carries_images_and_group(client, tmp_path):
    image = upload(client).json()
    client.post("/collections/new", data={"name": "Forecasting", "group": "Data science", "sql_dialect": "mysql"})
    cid = store.find_collection(store.connect(tmp_path / "app.db"), "Forecasting", "Data science").id
    client.post("/cards/new", data={"collection_id": str(cid), "tier": "1", "answer_format": "text",
                                    "prompt": f"Read the chart\n\n{image['markdown']}", "reference_answer": "Trend up"})
    exported = client.get(f"/collections/{cid}/export").json()
    assert exported["group"] == "Data science" and image["sha256"] in exported["media"]

    (tmp_path / "other").mkdir()
    fresh = make_client(tmp_path / "other")  # an empty database elsewhere
    result = fresh.post("/import", data={"text": json.dumps(exported)}).text
    assert "1 added" in result
    assert fresh.get(image["url"]).content == PNG
    exported["media"][image["sha256"]]["data"] = "aGVsbG8="  # tampered image data
    assert "match its fingerprint" in fresh.post("/import", data={"text": json.dumps(exported)}).text


def test_dashboard_groups_collections(client):
    client.post("/collections/new", data={"name": "Experiments", "group": "Data science", "sql_dialect": "mysql"})
    client.post("/collections/new", data={"name": "MMM", "group": "Data science", "sql_dialect": "mysql"})
    page = client.get("/").text
    assert page.count('class="group-heading"') == 1
    assert page.index("Data science</h2>") < page.index("Experiments") < page.index("MMM")
    assert page.index("Python") < page.index("Data science</h2>")  # ungrouped first


def test_new_collection_in_a_group_from_the_card_form(client):
    form = {"collection_id": "new", "new_collection": "Probability", "new_collection_group": "Data science",
            "tier": "1", "answer_format": "text", "prompt": "What is a prior?", "reference_answer": "Belief before data."}
    assert "Saved." in client.post("/cards/new", data=form).text
    assert "Data science › Probability" in client.get("/browse").text
