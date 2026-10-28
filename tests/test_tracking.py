"""Struggle totals, the struggle-set rule, and the tracking-off path."""

from __future__ import annotations

import json
from pathlib import Path

from anki_free_review.tracking import (
    SUMMARY_BROWSER,
    SUMMARY_CLOSE,
    SUMMARY_REVIEW,
    STRUGGLE_SET_RULE,
    accept_review_answer,
    begin_session_tracking,
    classify_ease,
    config_with_tracking,
    format_elapsed,
    format_running_stats,
    format_success_rate,
    format_summary_text,
    note_card_seen,
    should_show_summary,
    struggle_browser_search,
    summary_actions,
    tracking_enabled_from_config,
)


def test_shipped_config_defaults_tracking_off() -> None:
    path = Path(__file__).resolve().parents[1] / "anki_free_review" / "config.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"track_struggle": False}


def test_saved_tracking_choice_defaults_off() -> None:
    assert tracking_enabled_from_config(None) is False
    assert tracking_enabled_from_config({}) is False
    assert tracking_enabled_from_config({"track_struggle": False}) is False
    assert tracking_enabled_from_config({"track_struggle": "true"}) is False
    assert tracking_enabled_from_config({"track_struggle": True}) is True
    assert config_with_tracking({"reviewer": 1}, False) == {
        "reviewer": 1,
        "track_struggle": False,
    }
    assert config_with_tracking(None, True)["track_struggle"] is True


def test_classify_good_easy_solid_and_again_hard_struggle() -> None:
    assert classify_ease(1) == "struggle"
    assert classify_ease(2) == "struggle"
    assert classify_ease(3) == "solid"
    assert classify_ease(4) == "solid"
    assert classify_ease(0) is None
    assert classify_ease(5) is None


def test_struggle_set_keeps_a_card_after_a_later_good() -> None:
    stats = begin_session_tracking(True, now=0)
    assert stats is not None

    # Good first does not put the card in the set. Again does, and Easy stays.
    assert stats.record_answer(1, 3) == "solid"
    assert 1 not in stats.struggle_card_ids
    assert stats.record_answer(1, 1) == "struggle"
    assert stats.record_answer(1, 4) == "solid"
    assert 1 in stats.struggle_card_ids

    stats.record_answer(2, 4)  # Easy only
    stats.record_answer(3, 2)  # Hard
    stats.record_answer(3, 2)  # Hard again: one card, two struggle answers

    assert stats.struggle_card_ids == {1, 3}
    assert 2 not in stats.struggle_card_ids
    assert stats.answered == 6
    assert stats.solid == 3
    assert stats.struggle == 3
    assert stats.seen_card_ids == {1, 2, 3}
    assert stats.record_answer(9, 0) is None
    assert stats.answered == 6
    assert 9 not in stats.seen_card_ids


def test_summary_matches_a_simulated_answer_sequence() -> None:
    stats = begin_session_tracking(True, now=10)
    assert stats is not None
    stats.note_seen(1)
    stats.record_answer(1, 1)
    stats.record_answer(1, 3)
    stats.freeze(now=70)
    assert format_elapsed(0) == "0:00"
    assert format_elapsed(59.9) == "0:59"
    assert format_elapsed(60) == "1:00"
    assert format_elapsed(3661) == "1:01:01"
    assert format_elapsed(-4) == "0:00"
    assert format_success_rate(0, 0) == "n/a"
    assert format_success_rate(1, 2) == "50%"
    assert format_success_rate(2, 3) == "66.7%"
    assert format_success_rate(4, 7) == "57.1%"
    assert format_summary_text(stats) == (
        "Time spent: 1:00\n"
        "Cards seen: 1\n"
        "Answered: 2\n"
        "Solid (Good or Easy): 1\n"
        "Struggle (Again or Hard): 1\n"
        "Success rate: 50% (solid / answered)\n"
        "Struggle cards: 1\n"
        "\n" + STRUGGLE_SET_RULE
    )
    # The clock is frozen when the session ends, so a later read does not move.
    assert format_running_stats(stats, now=999) == (
        "Free Review: Answered 2 · Solid 1 · Struggle 1 · 1:00"
    )
    fresh = begin_session_tracking(True, now=0)
    assert fresh is not None
    assert format_running_stats(fresh, now=0) == (
        "Free Review: Answered 0 · Solid 0 · Struggle 0 · 0:00"
    )
    assert "Success rate: n/a (solid / answered)" in format_summary_text(fresh)


def test_summary_actions_and_browser_search() -> None:
    stats = begin_session_tracking(True, now=0)
    assert stats is not None
    stats.record_answer(1, 3)
    assert summary_actions(stats, source_deck_id=5) == (SUMMARY_CLOSE,)
    assert struggle_browser_search(stats.struggle_card_ids) == ""

    stats.record_answer(30, 1)
    stats.record_answer(10, 2)
    stats.record_answer(30, 3)  # stays in the set
    assert summary_actions(stats, source_deck_id=5) == (
        SUMMARY_REVIEW,
        SUMMARY_BROWSER,
        SUMMARY_CLOSE,
    )
    assert summary_actions(stats, source_deck_id=None) == (
        SUMMARY_BROWSER,
        SUMMARY_CLOSE,
    )
    assert struggle_browser_search([30, 10, 30]) == "cid:10,30"
    assert struggle_browser_search([]) == ""


def test_tracking_off_does_not_record_or_require_a_summary() -> None:
    assert begin_session_tracking(False) is None
    assert begin_session_tracking(False, now=5) is None
    assert should_show_summary(None) is False
    assert (
        accept_review_answer(None, in_tracked_deck=True, card_id=4, ease=1) is None
    )
    assert note_card_seen(None, in_tracked_deck=True, card_id=4) is False

    stats = begin_session_tracking(True, now=0)
    assert (
        accept_review_answer(stats, in_tracked_deck=False, card_id=4, ease=1) is None
    )
    assert stats is not None
    assert stats.answered == 0
    assert stats.struggle_card_ids == set()
    assert note_card_seen(stats, in_tracked_deck=False, card_id=4) is False
    assert stats.seen_card_ids == set()
    assert (
        accept_review_answer(stats, in_tracked_deck=True, card_id=4, ease=2)
        == "struggle"
    )
    assert stats.struggle_card_ids == {4}
    assert should_show_summary(stats) is True
