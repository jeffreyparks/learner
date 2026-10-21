from itertools import count

from learner import engine
from learner.config import Config
from learner.engine import CORRECT, INCORRECT, ASSISTED, REVEALED, KNOWN, LEARNING, NEW
from learner.models import Attempt, Card

_ids = count(1)


def card(id, tier=1, reset_at=None):
    return Card(
        id=id, collection_id=1, tier=tier, answer_format="text",
        prompt="p", reference_answer="a", rubric=[], references=[], tags=[], archived=False,
        progress_reset_at=reset_at, created_at=f"2026-01-01T00:00:{id:02d}", updated_at="",
    )


def attempt(card_id, session, outcome, ts=None, llm_verdict=None):
    n = next(_ids)
    return Attempt(id=n, card_id=card_id, session_id=session, ts=ts or f"2026-02-01T00:{n:05d}", outcome=outcome,
                   llm_verdict=llm_verdict)


def status(c, attempts, known_streak=2):
    return engine.card_progress(c, attempts, known_streak)


# --- progress ---


def test_new_card_has_no_attempts():
    assert status(card(1), []).status == NEW


def test_known_needs_correct_answers_in_separate_sessions():
    c = card(1)
    same_session = [attempt(1, "s1", CORRECT), attempt(1, "s1", CORRECT)]
    assert status(c, same_session).streak == 1
    assert status(c, same_session).status == LEARNING
    two_sessions = [attempt(1, "s1", CORRECT), attempt(1, "s2", CORRECT)]
    assert status(c, two_sessions).status == KNOWN


def test_incorrect_resets_and_assisted_holds():
    c = card(1)
    assert status(c, [attempt(1, "s1", CORRECT), attempt(1, "s2", INCORRECT)]).streak == 0
    assert status(c, [attempt(1, "s1", CORRECT), attempt(1, "s2", ASSISTED)]).streak == 1


def test_miss_then_correct_in_same_session_does_not_count():
    c = card(1)
    p = status(c, [attempt(1, "s1", INCORRECT), attempt(1, "s1", CORRECT)])
    assert p.streak == 0 and p.status == LEARNING


def test_peek_has_no_effect_on_the_score():
    c = card(1)
    assert status(c, [attempt(1, "s1", REVEALED)]).status == NEW  # only peeked: still new
    known = [attempt(1, "s1", CORRECT), attempt(1, "s2", CORRECT)]
    after_peek = status(c, known + [attempt(1, "s3", REVEALED)])
    assert (after_peek.status, after_peek.streak, after_peek.attempts, after_peek.recent_accuracy) == (KNOWN, 2, 2, 1.0)


def test_right_after_a_peek_in_the_same_session_does_not_count():
    c = card(1)
    assert status(c, [attempt(1, "s1", REVEALED), attempt(1, "s1", CORRECT)]).streak == 0


def test_reset_ignores_earlier_attempts():
    attempts = [attempt(1, "s1", CORRECT, ts="2026-01-01"), attempt(1, "s2", CORRECT, ts="2026-01-02")]
    assert status(card(1), attempts).status == KNOWN
    assert status(card(1, reset_at="2026-01-03"), attempts).status == NEW


# --- tiers ---


def test_unlock_chains_and_is_sticky():
    cards = [card(1, tier=1), card(2, tier=2), card(3, tier=3)]
    known = engine.Progress(KNOWN)
    progress = {1: known, 2: known, 3: engine.Progress(NEW)}
    assert engine.unlocked_through(cards, progress, stored_max=1, unlock_pct=0.8) == 3
    # Tier 1 slipped below the threshold, but the stored unlock holds.
    progress = {1: engine.Progress(LEARNING), 2: engine.Progress(NEW), 3: engine.Progress(NEW)}
    assert engine.unlocked_through(cards, progress, stored_max=2, unlock_pct=0.8) == 2


def test_lowest_tier_is_always_unlocked():
    cards = [card(1, tier=2)]
    assert engine.unlocked_through(cards, {1: engine.Progress(NEW)}, stored_max=1, unlock_pct=0.8) == 2


# --- sessions ---


def test_plan_takes_learning_then_new_then_refresher():
    cfg = Config(session_size=10, new_per_session=2, refresher_pct=0.1)
    cards = [card(i) for i in range(1, 8)]
    start = {i: engine.Progress(NEW) for i in range(1, 8)}
    start[1] = engine.Progress(LEARNING, recent_accuracy=0.8)
    start[2] = engine.Progress(LEARNING, recent_accuracy=0.2)
    start[3] = engine.Progress(KNOWN, last_seen="2026-03-01")
    start[4] = engine.Progress(KNOWN, last_seen="2026-02-01")
    assert engine.session_plan(cards, start, 1, cfg) == [2, 1, 5, 6, 4]


def test_complete_tier_gives_short_refresher_pass():
    cfg = Config(session_size=20)
    cards = [card(i) for i in range(1, 11)]
    start = {i: engine.Progress(KNOWN, last_seen=f"2026-01-{i:02d}") for i in range(1, 11)}
    assert engine.session_plan(cards, start, 1, cfg) == [1, 2, 3, 4, 5]


def test_missed_card_comes_back_after_gap():
    cfg = Config(session_size=10, new_per_session=5, requeue_gap=2)
    cards = [card(i) for i in range(1, 6)]
    attempts = [attempt(1, "s", INCORRECT)]
    assert engine.session_state(cards, attempts, "s", 1, cfg).next_card_id == 2
    attempts += [attempt(2, "s", CORRECT), attempt(3, "s", CORRECT)]
    state = engine.session_state(cards, attempts, "s", 1, cfg)
    assert state.next_card_id == 1
    assert state.answered == 3 and state.remaining == 3  # cards 4, 5, and the miss


def test_peeked_card_comes_back_after_gap():
    cfg = Config(session_size=10, new_per_session=5, requeue_gap=1)
    cards = [card(i) for i in range(1, 4)]
    attempts = [attempt(1, "s", REVEALED), attempt(2, "s", CORRECT)]
    assert engine.session_state(cards, attempts, "s", 1, cfg).next_card_id == 1


def test_session_ends_when_plan_and_misses_are_done():
    cfg = Config(session_size=10, new_per_session=2, max_attempts_per_card=2)
    cards = [card(1), card(2)]
    attempts = [attempt(1, "s", INCORRECT), attempt(2, "s", CORRECT), attempt(1, "s", INCORRECT)]
    state = engine.session_state(cards, attempts, "s", 1, cfg)
    assert state.next_card_id is None and state.remaining == 0


def test_session_summary_counts_newly_known():
    cards = [card(1), card(2)]
    attempts = [attempt(1, "old", CORRECT), attempt(1, "s", CORRECT), attempt(2, "s", INCORRECT)]
    summary = engine.session_summary(cards, attempts, "s", known_streak=2)
    assert (summary.answered, summary.right, summary.wrong, summary.newly_known) == (2, 1, 1, 1)


def test_session_summary_counts_peeks_separately():
    cards = [card(1), card(2)]
    attempts = [attempt(1, "s", REVEALED), attempt(2, "s", CORRECT)]
    summary = engine.session_summary(cards, attempts, "s", known_streak=2)
    assert (summary.right, summary.wrong, summary.revealed) == (1, 0, 1)


def test_new_cards_follow_the_concept_order():
    from learner import sequencer
    from learner.models import Analysis
    cfg = Config(session_size=10, new_per_session=2)
    cards = [card(1), card(2), card(3)]
    analyses = {  # card 3 teaches what 1 needs; card 2 is independent but hard
        1: Analysis(1, ["comprehension"], ["for-loop"], 1, False, "2027"),
        2: Analysis(2, ["zip"], [], 5, False, "2027"),
        3: Analysis(3, ["for-loop"], [], 2, False, "2027"),
    }
    start = {i: engine.Progress(NEW) for i in (1, 2, 3)}
    order = sequencer.build(cards, analyses)
    assert engine.session_plan(cards, start, 1, cfg) == [1, 2]  # without an order: creation order
    assert engine.session_plan(cards, start, 1, cfg, order) == [3, 2]  # card 1 waits for card 3


def test_assisted_credit_builds_half_a_step():
    c = card(1)
    attempts = [attempt(1, "s1", "assisted"), attempt(1, "s2", "assisted")]
    assert engine.card_progress(c, attempts, 2).status == engine.LEARNING
    p = engine.card_progress(c, attempts, 2, assisted_credit=0.5)
    assert p.streak == 1 and p.status == engine.LEARNING
    p = engine.card_progress(c, attempts + [attempt(1, "s3", "assisted"), attempt(1, "s4", "assisted")], 2, assisted_credit=0.5)
    assert p.status == engine.KNOWN


def test_right_retry_after_a_miss_earns_assisted_credit_once_per_session():
    c = card(1)
    miss_then_retry = [attempt(1, "s1", "incorrect"), attempt(1, "s1", "correct"), attempt(1, "s1", "correct")]
    assert engine.card_progress(c, miss_then_retry, 2).streak == 0  # no credit configured (slow)
    p = engine.card_progress(c, miss_then_retry, 2, assisted_credit=0.5)
    assert p.streak == 0.5  # credited once, not twice
    p = engine.card_progress(c, miss_then_retry + [attempt(1, "s2", "correct"), attempt(1, "s3", "correct")], 2, assisted_credit=0.5)
    assert p.status == engine.KNOWN  # 0.5 + 1 + 1


def test_retry_credit_needs_a_miss_not_a_peek():
    c = card(1)
    peek_then_right = [attempt(1, "s1", "revealed"), attempt(1, "s1", "correct")]
    assert engine.card_progress(c, peek_then_right, 2, assisted_credit=0.5).streak == 0
    first_try_then_again = [attempt(1, "s1", "correct"), attempt(1, "s1", "correct")]
    assert engine.card_progress(c, first_try_then_again, 2, assisted_credit=0.5).streak == 1


def test_partially_correct_holds_the_streak_instead_of_resetting_it():
    c = card(1)
    earned = [attempt(1, "s1", "correct")]
    partial = attempt(1, "s2", "incorrect", llm_verdict="partially_correct")
    assert engine.card_progress(c, earned + [partial], 2).streak == 1  # held, not reset
    assert engine.card_progress(c, earned + [partial], 2, assisted_credit=0.5).streak == 1.5
    plain_miss = attempt(1, "s2", "incorrect", llm_verdict="incorrect")
    assert engine.card_progress(c, earned + [plain_miss], 2).streak == 0


def test_partial_then_right_retry_in_the_same_session_is_not_a_second_credit():
    c = card(1)
    attempts = [attempt(1, "s1", "incorrect", llm_verdict="partially_correct"), attempt(1, "s1", "correct")]
    assert engine.card_progress(c, attempts, 2, assisted_credit=0.5).streak == 0.5
