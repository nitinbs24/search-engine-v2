import pytest

from sentences import best_sentences, split_sentences


def test_splits_on_sentence_end_before_a_capital_or_digit():
    assert split_sentences("First one. Second one! Is it third? 4 is last.") == [
        "First one.", "Second one!", "Is it third?", "4 is last."]


def test_decimals_and_lowercase_abbreviations_do_not_split():
    assert split_sentences("Version 3.5 is out, e.g. the new one. Done.") == ["Version 3.5 is out, e.g. the new one.", "Done."]


def test_one_sentence_and_empty_passage():
    assert split_sentences("  just one  ") == ["just one"]
    assert split_sentences("") == [] and split_sentences("   ") == []


def by_length(query, sentences):
    return [float(len(s)) for s in sentences]


def test_best_sentence_is_returned_for_the_top_passages_only():
    passages = ["Short. A much longer sentence here.", "Aa. Bbbbbbbbbbb.", "Xx. Yyyyyyyyyyyyyyyyyyyy."]
    out = best_sentences(by_length, "q", passages, [5.0, 9.0, 1.0], top=2)
    assert out == ["A much longer sentence here.", "Bbbbbbbbbbb.", ""]


def test_one_sentence_passage_is_its_own_highlight_and_is_not_scored():
    def boom(*_):
        raise AssertionError("scored")
    assert best_sentences(boom, "q", ["Only one.", ""], [2.0, 1.0], top=2) == ["Only one.", ""]


def test_top_zero_scores_nothing():
    assert best_sentences(by_length, "q", ["A. B."], [1.0], top=0) == [""]
