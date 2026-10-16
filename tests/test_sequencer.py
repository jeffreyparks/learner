from learner import sequencer
from learner.models import Analysis, Card


def card(id, tier=1, updated="2026-01-01"):
    return Card(id=id, collection_id=1, tier=tier, answer_format="text", prompt="p", reference_answer="a",
                rubric=[], references=[], tags=[], archived=False, progress_reset_at=None,
                created_at=f"2026-01-01T00:00:{id:02d}", updated_at=updated)


def analysis(card_id, introduced=(), required=(), difficulty=3, manual=False, at="2026-02-01"):
    return Analysis(card_id, list(introduced), list(required), difficulty, manual, at)


def test_teaching_card_comes_before_the_cards_that_need_it():
    cards = [card(1), card(2), card(3)]
    analyses = {
        1: analysis(1, ["list-comprehension"], ["for-loop"], difficulty=1),
        2: analysis(2, ["for-loop"], difficulty=5),
        3: analysis(3, ["generator"], ["list-comprehension"], difficulty=1),
    }
    assert sequencer.build(cards, analyses).order[1] == [2, 1, 3]


def test_ties_break_by_difficulty_then_creation():
    cards = [card(1), card(2), card(3)]
    analyses = {1: analysis(1, difficulty=3), 2: analysis(2, difficulty=1), 3: analysis(3, difficulty=3)}
    assert sequencer.build(cards, analyses).order[1] == [2, 1, 3]


def test_edited_cards_go_last_until_rebuilt_but_manual_ones_stay():
    cards = [card(1, updated="2026-03-01"), card(2), card(3, updated="2026-03-01")]
    analyses = {1: analysis(1, difficulty=1), 2: analysis(2), 3: analysis(3, difficulty=1, manual=True)}
    seq = sequencer.build(cards, analyses)
    assert seq.unsequenced == {1}
    assert seq.order[1] == [3, 2, 1]


def test_cycle_is_broken_and_only_its_members_are_flagged():
    cards = [card(1), card(2), card(3)]
    analyses = {
        1: analysis(1, ["a"], ["b"]),
        2: analysis(2, ["b"], ["a"], difficulty=1),
        3: analysis(3, ["c"], ["a"]),  # downstream of the cycle, not part of it
    }
    seq = sequencer.build(cards, analyses)
    assert seq.order[1] == [2, 1, 3]
    [warning] = [w for w in seq.warnings if w.kind == "cycle"]
    assert warning.card_ids == (1, 2)


def test_missing_and_tier_mismatch_warnings():
    cards = [card(1, tier=1), card(2, tier=1), card(3, tier=2)]
    analyses = {
        1: analysis(1, [], ["window-functions"]),
        2: analysis(2, [], ["ctes"]),
        3: analysis(3, ["ctes"], ["window-functions"]),
    }
    kinds = {(w.kind, w.card_ids) for w in sequencer.build(cards, analyses).warnings}
    assert ("missing", (1, 3)) in kinds
    assert ("tier_mismatch", (2,)) in kinds


def test_concepts_from_lower_tiers_dont_create_edges():
    cards = [card(1, tier=1), card(2, tier=2), card(3, tier=2)]
    analyses = {1: analysis(1, ["joins"]), 2: analysis(2, [], ["joins"], difficulty=1), 3: analysis(3, ["joins"], difficulty=5)}
    assert sequencer.build(cards, analyses).order[2] == [2, 3]


def test_ready_cards_come_first():
    cards = [card(1), card(2), card(3)]
    analyses = {
        1: analysis(1, ["for-loop"], difficulty=1),
        2: analysis(2, ["comprehension"], ["for-loop"], difficulty=2),
        3: analysis(3, ["zip"], difficulty=3),
    }
    seq = sequencer.build(cards, analyses)
    assert sequencer.new_card_order(seq, 1, {2, 3}, known_ids=set()) == [3, 2]  # 2 waits for card 1
    assert sequencer.new_card_order(seq, 1, {2, 3}, known_ids={1}) == [2, 3]


def test_normalize_concept():
    assert sequencer.concept_list(["List Comprehension ", "list-comprehension", "C#", ""]) == ["list-comprehension", "c#"]
