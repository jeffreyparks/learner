from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from learner import tutor
from learner.config import Config
from learner.llm import ClaudeProvider, FakeProvider, LLMError
from learner.models import Card


def card(**overrides):
    fields = dict(
        id=1, collection_id=1, tier=1, answer_format="sql", prompt="List Boston customers.",
        reference_answer="SELECT name FROM customers WHERE city = 'Boston';", rubric=["Filters on city"],
        references=[], tags=[], archived=False, progress_reset_at=None, created_at="", updated_at="",
    )
    return Card(**(fields | overrides))


def test_grade_prompt_includes_card_answer_and_syntax_check():
    provider = FakeProvider()
    tutor.grade(provider, card(), "SELECT name FROM WHERE", "mysql")
    task, user = provider.calls[0]
    assert task == "grade"
    assert "SQL (mysql dialect)" in user and "- Filters on city" in user
    assert "<learner_answer>\nSELECT name FROM WHERE\n</learner_answer>" in user
    assert "Expected table name" in user  # sqlglot's diagnostic reached the grader


def test_answers_with_dollar_signs_survive_templating():
    provider = FakeProvider()
    tutor.grade(provider, card(answer_format="text"), "costs $5 or ${x}", "mysql")
    assert "costs $5 or ${x}" in provider.calls[0][1]


def test_partially_correct_is_not_right():
    provider = FakeProvider({"grade": {"verdict": "partially_correct", "feedback": "Close.", "rubric_hits": [],
                                       "rubric_misses": ["Filters on city"], "equivalent_approach": False}})
    grade = tutor.grade(provider, card(), "SELECT name FROM customers", "mysql")
    assert not grade.right and grade.rubric_misses == ["Filters on city"]


def test_malformed_grade_raises():
    provider = FakeProvider({"grade": {"verdict": "great"}})
    with pytest.raises(LLMError):
        tutor.grade(provider, card(), "x", "mysql")


def test_hint_level_is_clamped():
    provider = FakeProvider()
    tutor.hint(provider, card(), "", level=9, earlier=[], dialect="mysql")
    assert "<hint_level>3</hint_level>" in provider.calls[0][1]


# --- ClaudeProvider, against a stub client (no network) ---


class StubMessages:
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def claude_with(result):
    messages = StubMessages(result)
    return ClaudeProvider(Config(provider="claude"), client=SimpleNamespace(messages=messages)), messages


def response(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def test_claude_request_uses_task_model_effort_and_schema():
    provider, messages = claude_with(response('{"hint": "Look at JOINs."}'))
    assert provider.complete("hint", "sys", "user", tutor.HINT_SCHEMA) == {"hint": "Look at JOINs."}
    assert messages.kwargs["model"] == "claude-haiku-5-5"
    assert messages.kwargs["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": tutor.HINT_SCHEMA}}
    assert "thinking" not in messages.kwargs


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_claude_early_stop_raises(stop_reason):
    provider, _ = claude_with(response("{}", stop_reason))
    with pytest.raises(LLMError, match=stop_reason):
        provider.complete("grade", "s", "u", tutor.GRADE_SCHEMA)


def test_claude_api_errors_become_llm_errors():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    provider, _ = claude_with(anthropic.APIConnectionError(request=request))
    with pytest.raises(LLMError, match="Couldn't reach"):
        provider.complete("grade", "s", "u", tutor.GRADE_SCHEMA)


# --- sequencing ---


def test_analyze_tier_skips_fixed_cards_and_normalizes_names():
    from learner.models import Analysis
    cards = [card(id=1, tags=["joins"]), card(id=2, tags=["joins", "Group By"])]
    fixed = {2: Analysis(2, ["group-by"], ["joins"], 2, True, "t")}
    provider = FakeProvider({"sequence": {"cards": [
        {"card_id": 1, "concepts_introduced": ["Left Join"], "concepts_required": [], "difficulty": 2},
        {"card_id": 2, "concepts_introduced": ["ignored"], "concepts_required": [], "difficulty": 9},
        {"card_id": 99, "concepts_introduced": ["unknown card"], "concepts_required": [], "difficulty": 1},
    ]}})
    result = tutor.analyze_tier(provider, "SQL", 1, cards, fixed, ["select"], ["joins"])
    assert result == {1: tutor.CardConcepts(["left-join"], [], 2)}
    user = provider.calls[0][1]
    assert '<fixed_concepts introduced="group-by" required="joins"/>' in user
    assert "select" in user and 'tags="joins, Group By"' in user


def test_fake_sequencer_builds_a_graph_from_tags():
    cards = [card(id=1, tags=["joins"]), card(id=2, tags=["joins", "aggregation"])]
    result = tutor.analyze_tier(FakeProvider(), "SQL", 1, cards, {}, [], [])
    assert result[1].introduced == ["joins"] and result[2].required == ["joins"]


class StubStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def test_claude_sequencing_streams_on_sonnet_with_fallback():
    calls = {}

    def stream(**kwargs):
        calls.update(kwargs)
        return StubStream(response('{"cards": []}'))

    client = SimpleNamespace(messages=StubMessages(None), beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    provider = ClaudeProvider(Config(provider="claude"), client=client)
    assert provider.complete("sequence", "s", "u", tutor.SEQUENCE_SCHEMA) == {"cards": []}
    assert calls["model"] == "claude-sonnet-5-5" and calls["fallbacks"] == "default"
    assert calls["betas"] == ["server-side-fallback-2026-07-01"]
    assert calls["output_config"]["effort"] == "medium"


def test_claude_drafting_uses_the_draft_model():
    calls = {}

    def stream(**kwargs):
        calls.update(kwargs)
        return StubStream(response('{"cards": []}'))

    client = SimpleNamespace(messages=StubMessages(None), beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    provider = ClaudeProvider(Config(provider="claude", draft_model="claude-opus-5-5", draft_effort="high"), client=client)
    provider.complete("draft", "s", "u", {})
    assert calls["model"] == "claude-opus-5-5" and calls["output_config"]["effort"] == "high"


def test_ask_sends_phase_and_conversation_and_limits_it():
    provider = FakeProvider()
    chat = [("learner", "what is a CTE?"), ("tutor", "A named subquery."), ("learner", "show me one")]
    reply = tutor.ask(provider, card(), "SELECT 1", chat, answer_shown=False, dialect="postgres")
    assert "Fake tutor" in reply
    _, user = provider.calls[-1]
    assert "<phase>attempting</phase>" in user and 'visible_to_learner="no"' in user
    assert "<learner>\nwhat is a CTE?\n</learner>" in user and "<tutor>\nA named subquery.\n</tutor>" in user
    tutor.ask(provider, card(), "", chat, answer_shown=True, dialect="postgres")
    assert "<phase>answer_shown</phase>" in provider.calls[-1][1]
    many = [("learner", f"q{i}") for i in range(30)]
    tutor.ask(provider, card(), "", many, answer_shown=True, dialect="postgres")
    user = provider.calls[-1][1]
    assert "q29" in user and "q17" not in user and "q18" in user


def test_ask_needs_a_learner_question_last():
    with pytest.raises(LLMError):
        tutor.ask(FakeProvider(), card(), "", [("tutor", "hi")], False, "postgres")
    with pytest.raises(LLMError):
        tutor.ask(FakeProvider({"ask": {"reply": "  "}}), card(), "", [("learner", "hi")], False, "postgres")
