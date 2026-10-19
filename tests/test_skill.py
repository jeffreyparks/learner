"""The learner-cards agent skill must describe the same format the importer accepts."""

import importlib.util
import json
from pathlib import Path

import pytest

from learner import store
from learner.models import ANSWER_FORMATS, SQL_DIALECTS

SKILL = Path(__file__).parent.parent / "skills" / "learner-cards"
STARTER = Path(__file__).parent.parent / "learner" / "starter"
EXAMPLES = sorted((SKILL / "examples").glob("*.json"))

spec = importlib.util.spec_from_file_location("validate", SKILL / "scripts" / "validate.py")
validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.migrate(c)
    yield c
    c.close()


def test_enums_match_the_app():
    schema = json.loads((SKILL / "schema.json").read_text())
    assert tuple(schema["$defs"]["card"]["properties"]["answer_format"]["enum"]) == ANSWER_FORMATS
    assert tuple(schema["properties"]["sql_dialect"]["enum"]) == SQL_DIALECTS
    assert validate.ANSWER_FORMATS == ANSWER_FORMATS
    assert validate.SQL_DIALECTS == SQL_DIALECTS


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_examples_validate_and_import(conn, path):
    data = json.loads(path.read_text())
    assert validate.validate(data) == ([], [])
    result = store.import_collection(conn, data)
    assert result.errors == []
    assert result.created == len(data["cards"])


@pytest.mark.parametrize("path", sorted(STARTER.glob("*.json")), ids=lambda p: p.name)
def test_starter_collections_have_no_errors(path):
    errors, _ = validate.validate(json.loads(path.read_text()))
    assert errors == []


@pytest.mark.parametrize("card_change", [
    {"concepts": ["x"]},
    {"answer_format": "javascript"},
    {"tier": 0},
    {"prompt": "  "},
    {"references": [{"title": "Docs", "url": "docs.python.org"}]},
])
def test_validator_rejects_what_the_importer_rejects(conn, card_change):
    card = {"tier": 1, "prompt": "What is 2 + 2?", "reference_answer": "4"} | card_change
    data = {"collection": "Math", "cards": [card]}
    errors, _ = validate.validate(data)
    assert errors
    assert store.import_collection(conn, data).errors
