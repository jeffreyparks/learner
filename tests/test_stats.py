from datetime import date

from learner import stats
from learner.engine import KNOWN, LEARNING, Progress
from learner.models import Analysis, AttemptRecord, Card


def card(id, fmt="text"):
    return Card(id=id, collection_id=1, tier=1, answer_format=fmt, prompt="p", reference_answer="a", rubric=[],
                references=[], tags=[], archived=False, progress_reset_at=None, created_at="", updated_at="")


def rec(id, card_id, outcome, ts="2026-10-10T12:00:00+00:00", verdict=None, overridden=False, hints=0, ms=None):
    return AttemptRecord(id, card_id, ts, outcome, None, hints, verdict, None, overridden, ms)


def test_totals():
    t = stats.totals([rec(1, 1, "correct", ms=4000), rec(2, 1, "assisted", hints=1, ms=10000), rec(3, 1, "incorrect", ms=6000)])
    assert (t.attempts, t.right, t.with_hints, t.median_seconds) == (3, 2, 1, 6.0)


def test_daily_includes_empty_days():
    days = stats.daily([rec(1, 1, "correct", ts="2026-10-10T12:00:00"), rec(2, 1, "incorrect", ts="2026-10-10T13:00:00")],
                       days=3, today=date(2026, 10, 11))
    assert [(d.day.day, d.attempts, d.right) for d in days] == [(9, 0, 0), (10, 2, 1), (11, 0, 0)]


def test_grader_agreement_by_format_and_direction():
    cards = {1: card(1, "sql"), 2: card(2, "text")}
    records = [
        rec(1, 1, "correct", verdict="correct"),
        rec(2, 1, "correct", verdict="incorrect", overridden=True),  # AI too harsh
        rec(3, 1, "incorrect", verdict="correct", overridden=True),  # AI too lenient
        rec(4, 2, "incorrect"),  # self-marked: not counted
    ]
    [row] = stats.grader_agreement(records, cards)
    assert (row.answer_format, row.graded, row.overridden, row.too_harsh, row.too_lenient) == ("sql", 3, 2, 1, 1)


def test_weakest_concepts_and_cards():
    cards = [card(1), card(2), card(3)]
    analyses = {1: Analysis(1, ["joins"], [], 3, False, ""), 2: Analysis(2, ["joins"], [], 3, False, ""),
                3: Analysis(3, ["ctes"], [], 3, False, "")}
    progress = {1: Progress(LEARNING, 0, 3, 0.33), 2: Progress(LEARNING, 0, 2, 0.5), 3: Progress(KNOWN, 2, 2, 1.0)}
    [only] = stats.weakest_concepts(cards, analyses, progress)  # "ctes" is at 100%: not weak
    assert only.name == "joins" and round(only.accuracy, 2) == 0.42
    assert [c.id for c in stats.weakest_cards(cards, progress)] == [1, 2]
