import io

import pytest

from learner import evals, store
from learner.llm import FakeProvider
from learner.models import CardIn


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "e.db")
    store.migrate(c)
    cid = store.save_collection(c, "SQL", "postgres", {})
    store.save_card(c, cid, CardIn(tier=1, answer_format="sql", prompt="Q", reference_answer="SELECT 1", rubric=["one"]))
    store.save_card(c, cid, CardIn(tier=1, prompt="Why?", reference_answer="Because of nulls"))
    yield c
    c.close()


def attempts(conn):
    # card 1: AI said right, you said wrong (override); card 2: AI right, you agreed; one self-marked.
    store.add_attempt(conn, 1, "s", "incorrect", answer="SELECT 2", llm_verdict="correct", overridden=True)
    store.add_attempt(conn, 2, "s", "correct", answer="nulls", llm_verdict="correct")
    store.add_attempt(conn, 2, "t", "correct", answer="nulls")


def test_export_adds_overrides_and_matching_agreed_cases_once(conn, tmp_path):
    attempts(conn)
    path = tmp_path / "cases.jsonl"
    assert evals.export_cases(conn, path) == (1, 1)
    assert evals.export_cases(conn, path) == (0, 0)  # nothing new
    cases = evals.load_cases(path)
    override = next(c for c in cases if c["kind"] == "override")
    assert override["expected"] == "wrong" and override["dialect"] == "postgres" and override["card"]["rubric"] == ["one"]


def test_no_overrides_means_nothing_to_export(conn, tmp_path):
    store.add_attempt(conn, 2, "s", "correct", answer="nulls", llm_verdict="correct")
    assert evals.export_cases(conn, tmp_path / "cases.jsonl") == (0, 0)


def test_run_reports_agreement_by_kind_and_format(conn, tmp_path):
    attempts(conn)
    path = tmp_path / "cases.jsonl"
    evals.export_cases(conn, path)
    out = io.StringIO()
    # The fake grader marks by word overlap: "SELECT 2" vs "SELECT 1" → partly right → wrong (agrees with you);
    # "nulls" vs "Because of nulls" → under 60% overlap → wrong (disagrees with you).
    tally = evals.run_cases(FakeProvider(), evals.load_cases(path), out)
    report = out.getvalue()
    assert tally[("override", "sql", "agree")] == 1 and tally[("agreed", "text", "disagree")] == 1
    assert "Your past overrides (were wrong)" in report and "1/1" in report
    assert "Still disagreeing:" in report and "you said right" in report


def test_cli_export(conn, tmp_path, capsys, monkeypatch):
    attempts(conn)
    toml = tmp_path / "learner.toml"
    toml.write_text(f'[app]\ndb_path = "{tmp_path / "e.db"}"\n')
    evals.main(["--config", str(toml), "--cases", str(tmp_path / "c.jsonl"), "export"])
    assert "Added 1 override case(s) and 1 agreed case(s)" in capsys.readouterr().out
