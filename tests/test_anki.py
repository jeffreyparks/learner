"""Anki import, against small decks built here in both Anki formats (no real deck needed)."""

import io
import json
import sqlite3
import zipfile

import pytest
import zstandard
from fastapi.testclient import TestClient

from learner import anki, store
from learner.app import create_app
from learner.config import Config

PNG = b"\x89PNG\r\n\x1a\n" + b"tiny"
BASIC = ("{{Front}}", "{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}")
REVERSE = ("{{Back}}", "{{FrontSide}}\n\n<hr id=answer>\n\n{{Front}}")
CLOZE = ("{{cloze:Text}}", "{{cloze:Text}}<br>\n{{Back Extra}}")

NOTES = [  # (guid, note type, fields, tags, deck, card ords)
    ("g1", "Basic", ["What does <code>COUNT(*)</code> count?", "All rows, <b>including</b> NULLs."], " sql ", "SQL::Aggregates", [0]),
    ("g2", "Reversed", ["GIL", "Global Interpreter Lock"], "", "Python", [0, 1]),
    ("g3", "Cloze", ["The {{c1::GIL}} lets one thread run {{c2::bytecode::what?}} at a time.", "CPython"], "null", "Python::Concurrency", [0, 1]),
    ("g4", "Basic", ['Which chart is this?<img src="chart.png">', "A bar chart"], "", "SQL::Aggregates", [0]),
]
NOTE_TYPES = {"Basic": (1, ["Front", "Back"], [BASIC], False), "Reversed": (2, ["Front", "Back"], [BASIC, REVERSE], False),
              "Cloze": (3, ["Text", "Back Extra"], [CLOZE], True)}


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        out.append((n & 0x7F) | (0x80 if n > 0x7F else 0))
        n >>= 7
        if not n:
            return bytes(out)


def pb_bytes(number: int, data: bytes) -> bytes:
    return varint(number << 3 | 2) + varint(len(data)) + data


def build(new_format: bool, notes=NOTES, media: dict[str, bytes] | None = None) -> bytes:
    media = {"chart.png": PNG} if media is None else media
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE notes (id INTEGER PRIMARY KEY, guid TEXT, mid INTEGER, flds TEXT, tags TEXT);
        CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER, ord INTEGER);
    """)
    decks = {name: i + 10 for i, name in enumerate(dict.fromkeys(n[4] for n in notes))}
    if new_format:
        db.executescript("""
            CREATE TABLE notetypes (id INTEGER PRIMARY KEY, name TEXT, config BLOB);
            CREATE TABLE fields (ntid INTEGER, ord INTEGER, name TEXT);
            CREATE TABLE templates (ntid INTEGER, ord INTEGER, name TEXT, config BLOB);
            CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT);
        """)
        for name, (ntid, fields, templates, cloze) in NOTE_TYPES.items():
            db.execute("INSERT INTO notetypes VALUES (?, ?, ?)", (ntid, name, (b"\x08\x01" if cloze else b"") + pb_bytes(3, b".card{}")))
            db.executemany("INSERT INTO fields VALUES (?, ?, ?)", [(ntid, i, f) for i, f in enumerate(fields)])
            db.executemany("INSERT INTO templates VALUES (?, ?, ?, ?)", [
                (ntid, i, f"Card {i + 1}", pb_bytes(1, q.encode()) + pb_bytes(2, a.encode())) for i, (q, a) in enumerate(templates)])
        db.executemany("INSERT INTO decks VALUES (?, ?)", [(did, name.replace("::", "\x1f")) for name, did in decks.items()])
    else:
        db.execute("CREATE TABLE col (models TEXT, decks TEXT)")
        models = {str(ntid): {"name": name, "type": 1 if cloze else 0,
                              "flds": [{"name": f, "ord": i} for i, f in enumerate(fields)],
                              "tmpls": [{"name": f"Card {i + 1}", "ord": i, "qfmt": q, "afmt": a} for i, (q, a) in enumerate(templates)]}
                  for name, (ntid, fields, templates, cloze) in NOTE_TYPES.items()}
        db.execute("INSERT INTO col VALUES (?, ?)", (json.dumps(models), json.dumps({str(d): {"name": n} for n, d in decks.items()})))
    card_id = 1
    for nid, (guid, nt, fields, tags, deck, ords) in enumerate(notes, start=1):
        db.execute("INSERT INTO notes VALUES (?, ?, ?, ?, ?)", (nid, guid, NOTE_TYPES[nt][0], "\x1f".join(fields), tags))
        for o in ords:
            db.execute("INSERT INTO cards VALUES (?, ?, ?, ?)", (card_id, nid, decks[deck], o))
            card_id += 1
    db.commit()
    raw = db.serialize()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("meta", b"\x08\x03")
        if new_format:
            compress = zstandard.ZstdCompressor().compress
            z.writestr("collection.anki21b", compress(raw))
            z.writestr("collection.anki2", b"placeholder")
            index = b"".join(pb_bytes(1, pb_bytes(1, name.encode())) for name in media)
            z.writestr("media", compress(index))
            for i, data in enumerate(media.values()):
                z.writestr(str(i), compress(data))
        else:
            z.writestr("collection.anki2", raw)
            z.writestr("media", json.dumps({str(i): name for i, name in enumerate(media)}))
            for i, data in enumerate(media.values()):
                z.writestr(str(i), data)
    return buf.getvalue()


@pytest.mark.parametrize("new_format", [True, False], ids=["anki-2.1.50+", "older-anki"])
def test_reads_both_formats_with_every_card_kind(new_format):
    deck = anki.read_apkg(build(new_format))
    assert deck.media == {"chart.png": PNG}
    by_ref = {c.source_ref: c for c in deck.cards}
    assert len(deck.cards) == 6  # basic, reversed ×2, cloze ×2, image card
    basic = by_ref["anki:g1:0"]
    assert basic.deck == ("SQL", "Aggregates") and basic.tags == ("sql",)
    assert anki.to_markdown(basic.prompt_html) == "What does `COUNT(*)` count?"
    assert anki.to_markdown(basic.answer_html) == "All rows, **including** NULLs."
    assert anki.to_markdown(by_ref["anki:g2:1"].prompt_html) == "Global Interpreter Lock"  # reversed
    c1, c2 = by_ref["anki:g3:0"], by_ref["anki:g3:1"]
    assert anki.to_markdown(c1.prompt_html) == "The […] lets one thread run bytecode at a time."
    assert anki.to_markdown(c2.prompt_html) == "The GIL lets one thread run [what?] at a time."
    assert "**GIL**" in anki.to_markdown(c1.answer_html) and "CPython" in anki.to_markdown(c1.answer_html)
    assert c1.tags == ()  # "null" isn't a real tag


def test_math_becomes_dollar_latex():
    md = anki.to_markdown(r"Is \(x_1 &lt; y^*\)? [$]a_b[/$]<br>Sum: \[\sum_{i=1}^n i<br>= n\] <b>bold_word</b>")
    assert md == "Is $x_1 < y^*$? $a_b$\nSum:\n\n$$\n\\sum_{i=1}^n i = n\n$$\n\n**bold\\_word**"


def test_collection_mapping():
    assert anki.collection_for(("SQL", "Joins")) == ("SQL", "Joins")
    assert anki.collection_for(("Python",)) == ("", "Python")
    assert anki.collection_for(("Data science", "Experiments", "A/B tests")) == ("Data science", "Experiments › A/B tests")


def test_rejects_bad_files_and_dangerous_media_names():
    with pytest.raises(anki.AnkiError, match="isn't an Anki deck"):
        anki.read_apkg(b"not a zip")
    deck = anki.read_apkg(build(False, media={"../../evil.png": PNG, "ok.png": PNG}))
    assert set(deck.media) == {"ok.png"}


def test_placeholder_only_export_explains_what_to_do():
    notes = [("p", "Basic", ["Please update to the latest Anki version, then import again.", "x"], "", "Default", [0])]
    with pytest.raises(anki.AnkiError, match="Support older Anki versions"):
        anki.read_apkg(build(False, notes=notes, media={}))


def test_empty_cards_are_skipped_with_a_note():
    notes = [("e", "Basic", ["A question with no answer", ""], "", "Default", [0])]
    assert anki.read_apkg(build(False, notes=notes, media={})).warnings == ["Skipped 1 card with an empty front or back."]


def test_unsupported_note_types_are_skipped_with_a_note():
    html = anki.render_template("{{#Header}}<div>{{Header}}</div>{{/Header}}{{^Extra}}none{{/Extra}}", {"Header": "Hi"}, None, False)
    assert html == "<div>Hi</div>none"
    assert anki.render_template("{{Front}}{{type:Back}}", {"Front": "Q", "Back": "A"}, None, False) == "Q"
    assert anki.render_template("{{tts en_US:Front}}{{Front}}", {"Front": "Q"}, None, False) == "Q"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "a.db")
    store.migrate(c)
    yield c
    c.close()


def as_collections(conn, deck):
    return [anki.DeckChoice("collections", c.collection, c.group, c.tiers) for c in anki.default_choices(conn, deck)]


def test_plan_then_import_then_reimport_keeps_local_edits(conn):
    deck = anki.read_apkg(build(True))
    choices = as_collections(conn, deck)
    plans = anki.plan_import(conn, deck, choices)
    assert [(p.display_name, p.new) for p in plans] == [("Python", 2), ("Python › Concurrency", 2), ("SQL › Aggregates", 2)]
    assert store.list_collections(conn) == []  # planning writes nothing

    results, warnings = anki.apply_import(conn, deck, choices, max_image_bytes=1_000_000)
    assert sum(r.new for r in results) == 6 and warnings == []
    image_card = store.find_card_by_source(conn, "anki:g4:0")
    assert "![chart](/media/" in image_card.prompt

    # You tweak a card locally; then the deck changes upstream and is imported again.
    from learner.models import CardIn
    basic = store.find_card_by_source(conn, "anki:g1:0")
    store.save_card(conn, basic.collection_id, CardIn(tier=2, answer_format="sql", prompt=basic.prompt,
                    reference_answer=basic.reference_answer, rubric=["counts NULLs"], tags=basic.tags), basic.id)
    changed = [("g1", "Basic", ["What does <code>COUNT(*)</code> count?", "Every row."], " sql ", "SQL::Aggregates", [0])] + NOTES[1:]
    results, _ = anki.apply_import(conn, anki.read_apkg(build(True, notes=changed)), choices, 1_000_000)
    assert sum(r.updated for r in results) == 1 and sum(r.new for r in results) == 0
    after = store.get_card(conn, basic.id)
    assert after.reference_answer == "Every row."  # Anki's content wins…
    assert (after.tier, after.answer_format, after.rubric) == (2, "sql", ["counts NULLs"])  # …your edits are kept


def test_import_screens(tmp_path):
    client = TestClient(create_app(Config(db_path=str(tmp_path / "w.db"), provider="off")), base_url="http://127.0.0.1")
    upload = client.post("/import", files={"file": ("My deck!.apkg", build(True), "application/octet-stream")}, follow_redirects=False)
    assert upload.headers["location"] == "/import/anki?file=My_deck_.apkg"
    assert (tmp_path / "imports" / "My_deck_.apkg").is_file()
    assert "My_deck_.apkg" in client.get("/import").text
    preview = client.get("/import/anki?file=My_deck_.apkg").text
    assert "Import 6 cards" in preview and "new collection" in preview
    assert 'name="mode_0" value="tiers" checked' in preview  # tiers by default
    # Choose: Python's subdecks as tiers (Concurrency first), SQL's as separate collections.
    form = {"mode_0": "tiers", "collection_0": "Python", "tier_0_0": "2", "tier_0_1": "1",
            "mode_1": "collections"}
    again = client.post("/import/anki/preview?file=My_deck_.apkg", data=form).text
    assert "Tier 1 · Concurrency" in again and "SQL › Aggregates" in again
    done = client.post("/import/anki?file=My_deck_.apkg", data=form).text
    assert "Imported" in done and "4 added" in done
    assert "Tier 1 · Concurrency" in client.get("/").text
    for bad in ("../w.db", "nope.apkg", "w.db"):
        assert client.get(f"/import/anki?file={bad}").status_code == 404



def test_subdecks_as_tiers_by_default_with_labels(conn):
    deck = anki.read_apkg(build(True))
    choices = anki.default_choices(conn, deck)
    py = next(c for c in choices if c.collection == "Python")
    assert py.mode == "tiers" and py.tiers == {anki.PARENT_ITSELF: 1, "Concurrency": 2}
    results, _ = anki.apply_import(conn, deck, choices, 1_000_000)
    python = store.find_collection(conn, "Python")
    assert {c.tier for c in store.list_cards(conn, python.id)} == {1, 2}
    assert python.tier_labels == {1: "General", 2: "Concurrency"}
    assert [p.display_name for p in results] == ["Python", "SQL"]


def test_existing_collection_with_other_cards_gets_a_separate_name(conn):
    from learner.models import CardIn
    cid = store.save_collection(conn, "SQL", "mysql", {})
    store.save_card(conn, cid, CardIn(tier=1, prompt="Starter card", reference_answer="x"))
    deck = anki.read_apkg(build(True))
    assert next(c for c in anki.default_choices(conn, deck) if c.collection.startswith("SQL")).collection == "SQL (Anki)"


def test_switching_to_tiers_on_reimport_moves_cards(conn):
    deck = anki.read_apkg(build(True))
    anki.apply_import(conn, deck, as_collections(conn, deck), 1_000_000)
    results, _ = anki.apply_import(conn, deck, anki.default_choices(conn, deck), 1_000_000)
    python = store.find_collection(conn, "Python")
    card = store.find_card_by_source(conn, "anki:g3:0")  # was in "Python › Concurrency"
    assert card.collection_id == python.id and card.tier == 2
    assert sum(p.new for p in results) == 0
    assert store.find_collection(conn, "Concurrency", "Python").archived  # emptied by the move, so archived


def test_ai_suggests_a_subdeck_order():
    from learner import tutor
    from learner.llm import FakeProvider
    order = tutor.order_subdecks(FakeProvider(), "SQL", [("Basics", ["What is SELECT?"]), ("Joins & keys", ["What is a join?"])])
    assert order == ["Joins & keys", "Basics"]  # the fake reverses; names with & survive the round trip
