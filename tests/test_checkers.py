from learner import checkers
from learner.models import ANSWER_FORMATS


def test_registry_is_the_source_of_answer_formats():
    assert ANSWER_FORMATS == tuple(checkers.FORMATS) == ("text", "python", "sql")


def test_text_and_blank_answers_are_never_checked():
    assert checkers.check("text", "def (") == []
    assert checkers.check("python", "   ") == []


def test_python_syntax_error_is_located():
    [first, *_] = checkers.check("python", "def f(:\n    pass")
    assert (first.line, first.col, first.severity) == (1, 7, "error")


def test_python_snippets_dont_warn_about_outside_names():
    # `df` is undefined, `os` unused, `x` unused: all normal in a snippet.
    assert checkers.check("python", "import os\nx = df.head()\nprint(df)") == []


def test_python_reports_selected_pyflakes_rules_as_warnings():
    [d] = checkers.check("python", 'print(f"hello")')
    assert d.severity == "warning" and "F541" in d.message


def test_sql_valid_query_is_clean():
    assert checkers.check("sql", "SELECT name FROM customers WHERE city = 'Boston' ORDER BY name;", "mysql") == []


def test_sql_error_points_at_the_bad_token():
    [d] = checkers.check("sql", "SELECT name FROM WHERE x = 1", "mysql")
    assert (d.line, d.col, d.end_col) == (1, 18, 23)
    assert d.message == "Expected table name but got “WHERE”"


def test_sql_unterminated_string():
    [d] = checkers.check("sql", "SELECT 'abc FROM t", "mysql")
    assert "quotes" in d.message


def test_starter_reference_answers_pass_their_own_checks():
    import json
    from pathlib import Path

    for path in (Path(__file__).parent.parent / "learner" / "starter").glob("*.json"):
        data = json.loads(path.read_text())
        for i, card in enumerate(data["cards"], start=1):
            problems = checkers.check(card.get("answer_format", "text"), card["reference_answer"], data.get("sql_dialect"))
            assert not [p for p in problems if p.severity == "error"], f"{path.name} card {i}: {problems}"
