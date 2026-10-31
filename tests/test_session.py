"""Collection tests for free review.

These run against the Anki 25.9.2 library (no Qt). They answer cards through
the real scheduler and compare scheduling fields with a snapshot taken
before the practice deck existed.
"""

from __future__ import annotations

import json
import os

import anki.consts as C
import pytest
from anki.cards import FSRSMemoryState
from anki.decks import FilteredDeckConfig
from anki.scheduler import FilteredDeckForUpdate

from anki.errors import UndoEmpty

from anki_free_review.session import (
    EMPTY_MESSAGE,
    FILTERED_SOURCE_MESSAGE,
    FREE_REVIEW_PREFIX,
    NO_STRUGGLE_CARDS_MESSAGE,
    PREVIEW_AGAIN_SECS,
    PREVIEW_GOOD_SECS,
    PREVIEW_HARD_SECS,
    SCOPE_ALL_SEEN,
    SCOPE_OMIT_MATURE,
    STRUGGLE_NAME_SUFFIX,
    V3_MESSAGE,
    cleanup_all_free_review_sessions,
    cleanup_free_review_deck,
    is_free_review_deck,
    seen_card_search,
    snapshot_path,
    start_free_review,
    start_from_dialog_choice,
    start_struggle_review,
    struggle_card_search,
    take_snapshot_warnings,
)
from anki_free_review.tracking import (
    accept_review_answer,
    begin_session_tracking,
    format_running_stats,
    format_summary_text,
    redo_review_answer,
    should_show_summary,
    struggle_browser_search,
    undo_review_answer,
)


def _note(col, front: str, deck_id: int):
    note = col.newNote()
    note["Front"] = front
    note["Back"] = "a"
    col.add_note(note, deck_id)
    return col.get_card(note.cards()[0].id)


def _write(col, card, **fields):
    memory = fields.pop("memory", None)
    for key, value in fields.items():
        setattr(card, key, value)
    if memory is not None:
        card.memory_state = memory
    col.update_cards([card])
    return col.get_card(card.id)


def _grab(col, cid: int) -> dict:
    card = col.get_card(cid)
    memory = card.memory_state
    return {
        "did": int(card.did),
        "type": int(card.type),
        "queue": int(card.queue),
        "due": int(card.due),
        "ivl": int(card.ivl),
        "factor": int(card.factor),
        "reps": int(card.reps),
        "lapses": int(card.lapses),
        "left": int(card.left),
        "odue": int(card.odue),
        "odid": int(card.odid),
        "custom_data": card.custom_data or "",
        "stability": None if memory is None else memory.stability,
        "difficulty": None if memory is None else memory.difficulty,
        "decay": card.decay,
        "desired_retention": card.desired_retention,
        "last_review_time": card.last_review_time,
    }


def _answer(col, cid: int, ease: int) -> None:
    card = col.get_card(cid)
    card.start_timer()
    col.sched.answerCard(card, ease)


def _next_states(col, cid: int):
    states = col._backend.get_scheduling_states(cid)
    packed = []
    for name in ("again", "hard", "good", "easy"):
        state = getattr(states, name)
        kind = state.WhichOneof("kind")
        if kind != "normal":
            packed.append((name, kind))
            continue
        normal = state.normal
        normal_kind = normal.WhichOneof("kind")
        if normal_kind == "review":
            review = normal.review
            memory = None
            if review.HasField("memory_state"):
                memory = (
                    round(review.memory_state.stability, 5),
                    round(review.memory_state.difficulty, 5),
                )
            packed.append(
                (
                    name,
                    normal_kind,
                    review.scheduled_days,
                    round(review.ease_factor, 5),
                    review.lapses,
                    memory,
                )
            )
        elif normal_kind == "relearning":
            relearn = normal.relearning
            packed.append(
                (
                    name,
                    normal_kind,
                    relearn.learning.scheduled_secs,
                    relearn.review.scheduled_days,
                    relearn.review.lapses,
                )
            )
        elif normal_kind == "learning":
            learning = normal.learning
            packed.append(
                (
                    name,
                    normal_kind,
                    learning.scheduled_secs,
                    learning.remaining_steps,
                )
            )
        else:
            packed.append((name, normal_kind))
    return tuple(packed)


def _home_counts(col) -> dict[str, int]:
    counts = {}
    for item in col.decks.all_names_and_ids():
        if col.decks.is_filtered(item.id):
            continue
        counts[item.name] = col.decks.card_count(item.id, include_subdecks=False)
    return counts


def _revlog(col, cid: int) -> list[tuple]:
    return col.db.all(
        "select ease, ivl, factor, type from revlog where cid=? order by id", cid
    )


def test_v3_scheduler_is_required() -> None:
    class _OldScheduler:
        def v3_scheduler(self) -> bool:
            return False

    result = start_free_review(_OldScheduler(), 1)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.deck_id is None
    assert result.message == V3_MESSAGE


def test_empty_and_new_only_decks_do_not_start(col) -> None:
    empty_id = col.decks.id("Empty")
    result = start_free_review(col, empty_id)
    assert result.ok is False
    assert result.message == EMPTY_MESSAGE
    assert result.deck_id is None
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))
    assert not os.path.exists(snapshot_path(col))

    only_new = col.decks.id("OnlyNew")
    _note(col, "unseen", only_new)
    result = start_free_review(col, only_new)
    assert result.ok is False
    assert result.message == EMPTY_MESSAGE
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))


def test_filtered_source_is_rejected(col) -> None:
    user = _user_filtered_deck(col, "My Cram")
    rejected = start_free_review(col, user)
    assert rejected.ok is False
    assert rejected.message == FILTERED_SOURCE_MESSAGE
    assert rejected.deck_id is None
    assert col.decks.have(user)
    assert not os.path.exists(snapshot_path(col))


def test_seen_cards_are_practiced_without_rescheduling(col) -> None:
    col.set_config("fsrs", True)
    parent = col.decks.id("Languages::Japanese")
    child = col.decks.id("Languages::Japanese::Vocab")
    grandchild = col.decks.id("Languages::Japanese::Vocab::Extra")
    other = col.decks.id("Other")

    learning = _write(
        col,
        _note(col, "learning", parent),
        type=C.CARD_TYPE_LRN,
        queue=C.QUEUE_TYPE_DAY_LEARN_RELEARN,
        due=4,
        ivl=0,
        factor=2500,
        reps=2,
        lapses=0,
        left=1002,
    )
    relearn = _write(
        col,
        _note(col, "relearn", child),
        type=C.CARD_TYPE_RELEARNING,
        queue=C.QUEUE_TYPE_LRN,
        due=1_700_000_000,
        ivl=9,
        factor=1800,
        reps=4,
        lapses=2,
        left=1,
    )
    again_card = _write(
        col,
        _note(col, "again", child),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_REV,
        due=30,
        ivl=15,
        factor=2500,
        reps=6,
        lapses=1,
        memory=FSRSMemoryState(stability=15, difficulty=6),
    )
    again_card.decay = 0.5
    col.update_cards([again_card])
    again_card = col.get_card(again_card.id)

    hard_card = _write(
        col,
        _note(col, "hard", child),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_REV,
        due=18,
        ivl=7,
        factor=1600,
        reps=3,
        lapses=2,
        memory=FSRSMemoryState(stability=4, difficulty=8),
    )
    good_card = _write(
        col,
        _note(col, "good", grandchild),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_REV,
        due=40,
        ivl=21,
        factor=2500,
        reps=8,
        lapses=0,
        memory=FSRSMemoryState(stability=21, difficulty=5),
    )
    easy_card = _write(
        col,
        _note(col, "easy", child),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_REV,
        due=60,
        ivl=40,
        factor=2700,
        reps=10,
        lapses=0,
        memory=FSRSMemoryState(stability=40, difficulty=3),
    )
    due_zero = _write(
        col,
        _note(col, "due-zero", child),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_REV,
        due=0,
        ivl=3,
        factor=2200,
        reps=2,
        lapses=0,
        memory=FSRSMemoryState(stability=3, difficulty=7),
    )
    new_card = _note(col, "new", child)
    suspended = _write(
        col,
        _note(col, "suspended", child),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_SUSPENDED,
        due=5,
        ivl=30,
        factor=2300,
        reps=9,
    )
    buried = _write(
        col,
        _note(col, "buried", parent),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_SIBLING_BURIED,
        due=6,
        ivl=11,
        factor=2100,
        reps=3,
    )
    manual_buried = _write(
        col,
        _note(col, "manual-buried", parent),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_MANUALLY_BURIED,
        due=7,
        ivl=12,
        factor=2000,
        reps=3,
    )
    outsider = _write(
        col,
        _note(col, "outsider", other),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_REV,
        due=2,
        ivl=5,
        factor=2500,
        reps=1,
    )

    tracked = [
        learning.id,
        relearn.id,
        again_card.id,
        hard_card.id,
        good_card.id,
        easy_card.id,
        due_zero.id,
        new_card.id,
        suspended.id,
        buried.id,
        manual_buried.id,
        outsider.id,
    ]
    before = {cid: _grab(col, cid) for cid in tracked}
    states_before = {cid: _next_states(col, cid) for cid in tracked}
    counts_before = _home_counts(col)
    tree_before = _names(col)

    search = seen_card_search(col, "Languages::Japanese")
    assert "-is:new" in search and "-is:suspended" in search and "-is:buried" in search
    matched = set(col.find_cards(search))
    assert again_card.id in matched
    assert learning.id in matched
    assert good_card.id in matched  # grandchild
    assert new_card.id not in matched
    assert suspended.id not in matched
    assert buried.id not in matched
    assert manual_buried.id not in matched
    assert outsider.id not in matched

    result = start_free_review(col, parent)
    assert result.ok, result.message
    assert result.deck_id is not None
    practice_name = col.decks.name(result.deck_id)
    assert practice_name == "Free Review - Languages / Japanese"
    assert "::" not in practice_name

    config = col.sched.get_or_create_filtered_deck(result.deck_id).config
    assert config.reschedule is False
    assert config.preview_again_secs == PREVIEW_AGAIN_SECS
    assert config.preview_hard_secs == PREVIEW_HARD_SECS
    assert config.preview_good_secs == PREVIEW_GOOD_SECS
    assert config.search_terms[0].limit == len(matched)
    assert "-is:new" in config.search_terms[0].search

    gathered = set(col.find_cards(f"did:{result.deck_id}"))
    assert gathered == matched
    for cid in (new_card.id, suspended.id, buried.id, manual_buried.id, outsider.id):
        assert cid not in gathered

    preview = col._backend.get_scheduling_states(again_card.id)
    assert preview.current.WhichOneof("kind") == "filtered"
    assert preview.current.filtered.WhichOneof("kind") == "preview"

    # Again/Hard leave the card in the practice deck with a temporary due.
    _answer(col, again_card.id, 1)
    mid = col.get_card(again_card.id)
    assert int(mid.queue) == C.QUEUE_TYPE_PREVIEW
    assert int(mid.reps) == before[again_card.id]["reps"]
    assert int(mid.ivl) == before[again_card.id]["ivl"]
    assert int(mid.factor) == before[again_card.id]["factor"]
    assert int(mid.due) != before[again_card.id]["due"]

    _answer(col, hard_card.id, 2)
    _answer(col, due_zero.id, 1)
    assert int(col.get_card(due_zero.id).due) != 0

    _answer(col, good_card.id, 3)
    _answer(col, easy_card.id, 4)
    _answer(col, learning.id, 3)
    _answer(col, relearn.id, 4)

    for cid in gathered:
        rows = _revlog(col, cid)
        assert rows, cid
        assert {row[3] for row in rows} == {C.REVLOG_CRAM}

    # Shutdown / next-open sweep: delete the practice deck and restore.
    assert cleanup_all_free_review_sessions(col) == 1
    assert col.decks.id_for_name(practice_name) is None
    assert _names(col) == tree_before
    assert _home_counts(col) == counts_before
    assert not os.path.exists(snapshot_path(col))
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))

    for cid in tracked:
        assert _grab(col, cid) == before[cid], col.get_card(cid).note()["Front"]
        assert _next_states(col, cid) == states_before[cid]

    # The due-zero card must not keep the preview timestamp.
    assert col.get_card(due_zero.id).due == 0
    assert col.get_card(again_card.id).decay == 0.5


def test_sm2_fields_and_revlog_are_unchanged(col) -> None:
    col.set_config("fsrs", False)
    deck_id = col.decks.id("SM2")
    cards = []
    for index, ease in enumerate((1, 2, 3, 4), start=1):
        card = _write(
            col,
            _note(col, f"card-{ease}", deck_id),
            type=C.CARD_TYPE_REV,
            queue=C.QUEUE_TYPE_REV,
            due=10 + index,
            ivl=5 + index,
            factor=2500,
            reps=index,
            lapses=0,
        )
        cards.append((card, ease))

    before = {card.id: _grab(col, card.id) for card, _ease in cards}
    states = {card.id: _next_states(col, card.id) for card, _ease in cards}
    result = start_free_review(col, deck_id)
    assert result.ok
    for card, ease in cards:
        _answer(col, card.id, ease)
        if ease in (1, 2):
            # Finish the preview delay so the card returns, then cleanup
            # still has to deal with any sibling left in the deck.
            still_in = col.decks.is_filtered(col.get_card(card.id).did)
            if still_in:
                pass
    assert cleanup_free_review_deck(col, result.deck_id)
    assert not os.path.exists(snapshot_path(col))
    for card, _ease in cards:
        assert _grab(col, card.id) == before[card.id]
        assert _next_states(col, card.id) == states[card.id]
        assert {row[3] for row in _revlog(col, card.id)} == {C.REVLOG_CRAM}
        assert col.get_card(card.id).decay is None


def test_interrupt_mid_preview_then_startup_sweep(col) -> None:
    deck_id = col.decks.id("Interrupt")
    card = _write(
        col,
        _note(col, "q", deck_id),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_REV,
        due=0,
        ivl=4,
        factor=2500,
        reps=3,
        lapses=1,
    )
    before = _grab(col, card.id)
    result = start_free_review(col, deck_id)
    _answer(col, card.id, 1)
    assert int(col.get_card(card.id).queue) == C.QUEUE_TYPE_PREVIEW
    # Profile open / quit runs the full sweep, not the single-deck helper.
    cleanup_all_free_review_sessions(col)
    assert _grab(col, card.id) == before
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))


def test_stale_snapshot_does_not_overwrite_a_real_review(col) -> None:
    deck_id = col.decks.id("Real")
    card = _write(
        col,
        _note(col, "q", deck_id),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_REV,
        due=14,
        ivl=6,
        factor=2500,
        reps=2,
        lapses=0,
    )
    result = start_free_review(col, deck_id)
    assert result.ok
    _answer(col, card.id, 3)  # preview Good returns the card, reps unchanged
    reviewed = col.get_card(card.id)
    reviewed.reps = 9
    reviewed.ivl = 40
    reviewed.due = 80
    reviewed.factor = 2700
    col.update_cards([reviewed])
    cleanup_all_free_review_sessions(col)
    after = col.get_card(card.id)
    assert int(after.reps) == 9
    assert int(after.ivl) == 40
    assert int(after.due) == 80
    assert int(after.factor) == 2700


def test_normal_review_does_change_scheduling(col) -> None:
    """The assertions above would notice a real reschedule."""

    deck_id = col.decks.id("Control")
    card = _write(
        col,
        _note(col, "q", deck_id),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_REV,
        due=3,
        ivl=10,
        factor=2500,
        reps=1,
        lapses=0,
    )
    before = _grab(col, card.id)
    col.decks.select(deck_id)
    _answer(col, card.id, 3)
    after = _grab(col, card.id)
    assert after["reps"] == before["reps"] + 1
    assert after["ivl"] != before["ivl"] or after["due"] != before["due"]


def test_existing_filtered_deck_with_the_same_name_is_kept(col) -> None:
    source = col.decks.id("Languages::Japanese")
    _write(
        col,
        _note(col, "seen", source),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_REV,
        due=9,
        ivl=4,
        factor=2500,
        reps=1,
    )
    user_name = "Free Review - Languages / Japanese"
    user_id = _user_filtered_deck(col, user_name)
    before = _grab(col, col.find_cards(f"did:{source}")[0])

    result = start_free_review(col, source)
    assert result.ok
    assert col.decks.name(result.deck_id) == f"{user_name} (2)"
    assert col.decks.have(user_id)
    assert col.sched.get_or_create_filtered_deck(user_id).config.reschedule is True

    cleanup_free_review_deck(col, result.deck_id)
    assert col.decks.have(user_id)
    assert col.decks.id_for_name(f"{user_name} (2)") is None
    assert _grab(col, col.find_cards("deck:Languages::Japanese")[0]) == before


def test_quoted_deck_names_and_nested_search(col) -> None:
    deck_id = col.decks.id('Quotes "and" :: Child')
    card = _write(
        col,
        _note(col, "seen", deck_id),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_REV,
        due=11,
        ivl=2,
        factor=2500,
        reps=1,
    )
    before = _grab(col, card.id)
    result = start_free_review(col, deck_id)
    assert result.ok
    assert "::" not in col.decks.name(result.deck_id)
    assert card.id in set(col.find_cards(f"did:{result.deck_id}"))
    cleanup_free_review_deck(col, result.deck_id)
    assert _grab(col, card.id) == before


def test_suspend_bury_move_and_set_due_date_survive_cleanup(col) -> None:
    col.set_config("fsrs", True)
    home = col.decks.id("Home")
    other = col.decks.id("OtherDeck")
    suspended = _review(col, "suspend", home, due=20, ivl=8, reps=3)
    buried = _review(col, "bury", home, due=21, ivl=8, reps=3)
    moved = _review(col, "move", home, due=22, ivl=8, reps=3)
    redated = _review(col, "redate", home, due=23, ivl=8, reps=3)
    decayed = _review(col, "decay", home, due=0, ivl=4, reps=2)
    decayed.decay = 0.5
    col.update_cards([decayed])

    result = start_free_review(col, home)
    assert result.ok
    _answer(col, suspended.id, 3)
    _answer(col, buried.id, 3)
    _answer(col, moved.id, 3)
    _answer(col, redated.id, 3)
    _answer(col, decayed.id, 1)
    assert int(col.get_card(decayed.id).due) >= 1_000_000_000

    col.sched.suspend_cards([suspended.id])
    col.sched.bury_cards([buried.id], manual=True)
    moved_card = col.get_card(moved.id)
    moved_card.did = other
    col.update_cards([moved_card])
    col.sched.set_due_date([redated.id], "8")
    redated_due = int(col.get_card(redated.id).due)

    assert cleanup_free_review_deck(col, result.deck_id)
    assert int(col.get_card(suspended.id).queue) == C.QUEUE_TYPE_SUSPENDED
    assert int(col.get_card(buried.id).queue) == C.QUEUE_TYPE_MANUALLY_BURIED
    assert int(col.get_card(moved.id).did) == int(other)
    assert int(col.get_card(redated.id).due) == redated_due
    assert int(col.get_card(decayed.id).due) == 0
    assert col.get_card(decayed.id).decay == 0.5
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))


def test_undo_after_cleanup_does_not_resurrect_the_practice_deck(col) -> None:
    deck_id = col.decks.id("Undo")
    card = _review(col, "q", deck_id, due=0, ivl=4, reps=2)
    result = start_free_review(col, deck_id)
    _answer(col, card.id, 1)
    assert cleanup_free_review_deck(col, result.deck_id)
    assert int(col.get_card(card.id).due) == 0
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))

    col.undo()
    assert int(col.get_card(card.id).due) == 0
    assert int(col.get_card(card.id).reps) == 2
    assert int(col.get_card(card.id).ivl) == 4
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))
    try:
        col.undo()
    except UndoEmpty:
        pass


def test_user_preview_deck_with_the_same_shape_is_not_deleted(col) -> None:
    source = col.decks.id("Kept")
    _review(col, "seen", source, due=9, ivl=4, reps=1)
    user_search = "deck:Nobody -is:new -is:suspended -is:buried"
    user_id = _user_filtered_deck(col, "Free Review - Kept", search=user_search, reschedule=False)
    result = start_free_review(col, source)
    assert result.ok
    assert col.decks.name(result.deck_id) == "Free Review - Kept (2)"
    removed = cleanup_all_free_review_sessions(col)
    assert removed == 1
    assert col.decks.have(user_id)
    assert col.sched.get_or_create_filtered_deck(user_id).config.reschedule is False
    assert col.decks.id_for_name("Free Review - Kept (2)") is None


def test_corrupt_restore_file_is_kept_and_warned(col) -> None:
    path = snapshot_path(col)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    deck_id = col.decks.id("StillHere")
    card = _review(col, "q", deck_id, due=4, ivl=3, reps=1)
    assert cleanup_all_free_review_sessions(col) == 0
    assert os.path.exists(path)
    warnings = take_snapshot_warnings()
    assert warnings
    assert "restore file" in warnings[0]
    refused = start_free_review(col, deck_id)
    assert refused.ok is False
    assert "restore file" in refused.message
    assert int(col.get_card(card.id).due) == 4
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))


def test_omit_mature_search_and_session_leave_scheduling_unchanged(col) -> None:
    deck_id = col.decks.id("Young")
    learning = _review(col, "learn", deck_id, due=1, ivl=0, reps=1)
    learning.type = C.CARD_TYPE_LRN
    learning.queue = C.QUEUE_TYPE_DAY_LEARN_RELEARN
    learning.left = 1001
    col.update_cards([learning])
    young = _review(col, "young", deck_id, due=2, ivl=20, reps=2)
    boundary = _review(col, "mature", deck_id, due=3, ivl=21, reps=4)
    older = _review(col, "older", deck_id, due=4, ivl=40, reps=6)
    ids = [learning.id, young.id, boundary.id, older.id]
    before = {cid: _grab(col, cid) for cid in ids}

    all_seen = set(col.find_cards(seen_card_search(col, "Young")))
    omitted = set(
        col.find_cards(seen_card_search(col, "Young", omit_mature=True))
    )
    assert {learning.id, young.id, boundary.id, older.id} <= all_seen
    assert omitted == {learning.id, young.id}
    assert "-prop:ivl>=21" in seen_card_search(col, "Young", omit_mature=True)

    result = start_free_review(col, deck_id, scope=SCOPE_OMIT_MATURE)
    assert result.ok, result.message
    gathered = set(col.find_cards(f"did:{result.deck_id}"))
    assert gathered == {learning.id, young.id}
    _answer(col, learning.id, 3)
    _answer(col, young.id, 1)
    assert cleanup_free_review_deck(col, result.deck_id)
    for cid in ids:
        assert _grab(col, cid) == before[cid]
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))


def test_omit_mature_with_only_mature_cards_creates_nothing(col) -> None:
    deck_id = col.decks.id("MatureOnly")
    _review(col, "m", deck_id, due=8, ivl=21, reps=3)
    empty = start_free_review(col, deck_id, scope=SCOPE_OMIT_MATURE)
    assert empty.ok is False
    assert empty.message == EMPTY_MESSAGE
    assert empty.deck_id is None
    assert not os.path.exists(snapshot_path(col))
    assert col.decks.id_for_name("Free Review - MatureOnly") is None


def test_cancel_dialog_creates_nothing(col) -> None:
    deck_id = col.decks.id("Cancel")
    card = _review(col, "q", deck_id, due=5, ivl=30, reps=2)
    before = _grab(col, card.id)
    result = start_from_dialog_choice(col, deck_id, None)
    assert result is None
    assert _grab(col, card.id) == before
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))
    assert not os.path.exists(snapshot_path(col))
    assert start_from_dialog_choice(col, deck_id, SCOPE_ALL_SEEN).ok


def test_preview_answer_does_not_bury_sibling(col) -> None:
    deck_id = col.decks.id("Siblings")
    model = col.models.by_name("Basic (and reversed card)")
    note = col.new_note(model)
    note["Front"] = "front"
    note["Back"] = "back"
    col.add_note(note, deck_id)
    first, second = note.cards()
    first.type = C.CARD_TYPE_REV
    first.queue = C.QUEUE_TYPE_REV
    first.due = 6
    first.ivl = 5
    first.factor = 2500
    first.reps = 2
    col.update_cards([first])
    result = start_free_review(col, deck_id)
    assert second.id not in set(col.find_cards(f"did:{result.deck_id}"))
    _answer(col, first.id, 3)
    sibling = col.get_card(second.id)
    assert int(sibling.queue) == C.QUEUE_TYPE_NEW
    cleanup_free_review_deck(col, result.deck_id)
    assert int(col.get_card(second.id).queue) == C.QUEUE_TYPE_NEW


def test_tracked_answers_match_stats_and_leave_scheduling_unchanged(col) -> None:
    col.set_config("fsrs", True)
    deck_id = col.decks.id("Practice")
    again = _review(col, "again", deck_id, due=10, ivl=8, reps=3)
    again.decay = 0.5
    again.memory_state = FSRSMemoryState(stability=8, difficulty=6)
    col.update_cards([again])
    hard = _review(col, "hard", deck_id, due=11, ivl=6, reps=2)
    good = _review(col, "good", deck_id, due=12, ivl=9, reps=4)
    easy = _review(col, "easy", deck_id, due=13, ivl=14, reps=5)
    bounce = _review(col, "bounce", deck_id, due=14, ivl=4, reps=2)
    outsider = _review(col, "outsider", deck_id, due=15, ivl=3, reps=1)
    cards = [again, hard, good, easy, bounce, outsider]
    ids = [card.id for card in cards]
    before = {cid: _grab(col, cid) for cid in ids}
    states = {cid: _next_states(col, cid) for cid in ids}

    assert begin_session_tracking(False) is None
    assert should_show_summary(None) is False

    result = start_free_review(col, deck_id)
    assert result.ok, result.message
    stats = begin_session_tracking(True, now=1_000.0)
    assert stats is not None
    sequence = [
        (again, 1),
        (hard, 2),
        (good, 3),
        (easy, 4),
        (bounce, 1),
        (bounce, 3),
    ]
    for card, ease in sequence:
        stats.note_seen(card.id)
        _answer(col, card.id, ease)
        assert stats.record_answer(card.id, ease) in {"solid", "struggle"}
    stats.note_seen(outsider.id)

    assert stats.answered == 6
    assert stats.solid == 3
    assert stats.struggle == 3
    assert stats.struggle_card_ids == {again.id, hard.id, bounce.id}
    assert good.id not in stats.struggle_card_ids
    assert easy.id not in stats.struggle_card_ids
    assert outsider.id not in stats.struggle_card_ids
    assert stats.seen_card_ids == set(ids)
    stats.freeze(now=1_065.0)
    summary = format_summary_text(stats)
    assert "Time spent: 1:05" in summary
    assert "Cards seen: 6" in summary
    assert "Answered: 6" in summary
    assert "Solid (Good or Easy): 3" in summary
    assert "Struggle (Again or Hard): 3" in summary
    assert "Success rate: 50% (solid / answered)" in summary
    assert "Struggle cards: 3" in summary
    assert format_running_stats(stats, now=9_999) == (
        "Free Review: Answered 6 · Solid 3 · Struggle 3 · 1:05"
    )

    payload = json.loads(open(snapshot_path(col), encoding="utf-8").read())
    assert set(payload) == {"version", "sessions"}
    stored = next(iter(payload["sessions"].values()))
    assert set(stored) == {"source_deck_id", "filtered_deck_id", "undo_id", "cards"}
    assert "struggle" not in json.dumps(payload)

    assert cleanup_free_review_deck(col, result.deck_id)
    for cid in ids:
        assert _grab(col, cid) == before[cid]
        assert _next_states(col, cid) == states[cid]

    revisit = start_struggle_review(col, deck_id, sorted(stats.struggle_card_ids))
    assert revisit.ok, revisit.message
    assert col.decks.name(revisit.deck_id) == f"Free Review - Practice{STRUGGLE_NAME_SUFFIX}"
    config = col.sched.get_or_create_filtered_deck(revisit.deck_id).config
    assert config.reschedule is False
    assert is_free_review_deck(col, revisit.deck_id)
    search = config.search_terms[0].search
    assert "-is:new" in search and "-is:suspended" in search and "-is:buried" in search
    gathered = set(col.find_cards(f"did:{revisit.deck_id}"))
    assert gathered == {again.id, hard.id, bounce.id}
    for cid in gathered:
        card = col.get_card(cid)
        assert int(card.did) == int(revisit.deck_id)
        assert int(card.odid) == int(deck_id)
    _answer(col, again.id, 1)
    _answer(col, hard.id, 2)
    _answer(col, bounce.id, 3)
    assert cleanup_free_review_deck(col, revisit.deck_id)
    for cid in ids:
        assert _grab(col, cid) == before[cid], col.get_card(cid).note()["Front"]
        assert _next_states(col, cid) == states[cid]
        if cid != outsider.id:
            assert {row[3] for row in _revlog(col, cid)} == {C.REVLOG_CRAM}
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))
    browser = struggle_browser_search(stats.struggle_card_ids)
    assert browser == "cid:" + ",".join(
        str(cid) for cid in sorted(stats.struggle_card_ids)
    )
    assert set(col.find_cards(browser)) == stats.struggle_card_ids


def test_tracking_off_writes_no_struggle_store(col) -> None:
    deck_id = col.decks.id("Quiet")
    card = _review(col, "q", deck_id, due=4, ivl=3, reps=1)
    before = _grab(col, card.id)
    result = start_free_review(col, deck_id)
    assert result.ok
    assert begin_session_tracking(False) is None
    payload = json.loads(open(snapshot_path(col), encoding="utf-8").read())
    assert set(payload) == {"version", "sessions"}
    stored = next(iter(payload["sessions"].values()))
    assert set(stored) == {"source_deck_id", "filtered_deck_id", "undo_id", "cards"}
    assert "struggle" not in json.dumps(payload)
    _answer(col, card.id, 3)
    assert cleanup_free_review_deck(col, result.deck_id)
    assert _grab(col, card.id) == before
    assert should_show_summary(None) is False
    assert not os.path.exists(snapshot_path(col))


def test_empty_struggle_review_creates_nothing(col) -> None:
    deck_id = col.decks.id("NoStruggle")
    card = _review(col, "q", deck_id, due=4, ivl=3, reps=1)
    before = _grab(col, card.id)
    result = start_struggle_review(col, deck_id, [])
    assert result.ok is False
    assert result.message == NO_STRUGGLE_CARDS_MESSAGE
    assert result.deck_id is None
    assert _grab(col, card.id) == before
    assert not os.path.exists(snapshot_path(col))
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))


def test_struggle_search_skips_new_and_suspended_cards(col) -> None:
    deck_id = col.decks.id("Skip")
    keep = _review(col, "keep", deck_id, due=6, ivl=5, reps=2)
    new = _note(col, "new", deck_id)
    suspended = _review(col, "suspended", deck_id, due=7, ivl=5, reps=2)
    col.sched.suspend_cards([suspended.id])
    search = struggle_card_search(col, [keep.id, new.id, suspended.id])
    assert "-is:new" in search and "-is:suspended" in search and "-is:buried" in search
    assert str(keep.id) in search
    assert set(col.find_cards(search)) == {keep.id}

    result = start_struggle_review(col, deck_id, [keep.id, new.id, suspended.id])
    assert result.ok, result.message
    assert set(col.find_cards(f"did:{result.deck_id}")) == {keep.id}
    assert int(col.get_card(keep.id).odid) == int(deck_id)
    browser = struggle_browser_search([suspended.id, keep.id])
    assert set(col.find_cards(browser)) == {keep.id, suspended.id}
    assert cleanup_free_review_deck(col, result.deck_id)
    assert int(col.get_card(suspended.id).queue) == C.QUEUE_TYPE_SUSPENDED
    assert int(col.get_card(keep.id).odid) == 0
    assert int(col.get_card(new.id).queue) == C.QUEUE_TYPE_NEW


def _schedule_identity(col, cid: int, snap: dict) -> None:
    card = col.get_card(cid)
    assert int(card.type) == snap["type"]
    assert int(card.ivl) == snap["ivl"]
    assert int(card.factor) == snap["factor"]
    assert int(card.reps) == snap["reps"]
    assert int(card.lapses) == snap["lapses"]


def _undo_tracked(col, stats) -> bool:
    out = col.undo()
    return undo_review_answer(
        stats,
        undone_step=int(out.counter),
        redo_step=int(out.new_status.last_step),
    )


def _redo_tracked(col, stats) -> bool:
    out = col.redo()
    return redo_review_answer(
        stats,
        redone_step=int(out.counter),
        undo_step=int(out.new_status.last_step),
    )


def test_undo_and_redo_roll_struggle_stats_without_rescheduling(col) -> None:
    """Answer, undo, and redo keep the tally on the same stack as the grades.

    Anki mints a new undo counter every time a step is undone or redone.
    The tally follows those counters. Scheduling columns stay put, and
    cleanup still collapses to one undo that does not bring the deck back.
    """

    deck_id = col.decks.id("StatsUndo")
    solid = _review(col, "solid", deck_id, due=5, ivl=4, reps=2)
    struggle = _review(col, "struggle", deck_id, due=0, ivl=4, reps=2)
    ids = [solid.id, struggle.id]
    before = {cid: _grab(col, cid) for cid in ids}

    result = start_free_review(col, deck_id)
    assert result.ok, result.message
    in_session = {cid: _grab(col, cid) for cid in ids}
    stats = begin_session_tracking(True, now=1_000.0)
    assert stats is not None

    def grade(card, ease: int) -> dict:
        _answer(col, card.id, ease)
        step = int(col.undo_status().last_step)
        kind = accept_review_answer(
            stats,
            in_tracked_deck=True,
            card_id=card.id,
            ease=ease,
            undo_step=step,
        )
        assert kind in {"solid", "struggle"}
        return _grab(col, card.id)

    after_solid = grade(solid, 3)
    after_again = grade(struggle, 1)
    after_hard = grade(struggle, 2)
    assert stats.answered == 3
    assert stats.solid == 1
    assert stats.struggle == 2
    assert stats.struggle_card_ids == {struggle.id}
    assert format_running_stats(stats, now=1_030.0) == (
        "Free Review: Answered 3 · Solid 1 · Struggle 2 · 0:30"
    )
    for cid in ids:
        _schedule_identity(col, cid, before[cid])

    edited = col.get_card(solid.id)
    edited.flags = 1
    col.update_card(edited)
    assert _undo_tracked(col, stats) is False
    assert stats.answered == 3
    assert int(col.get_card(solid.id).flags) == 0

    assert _undo_tracked(col, stats) is True
    assert stats.answered == 2
    assert stats.solid == 1
    assert stats.struggle == 1
    assert stats.struggle_card_ids == {struggle.id}
    assert _grab(col, struggle.id) == after_again

    assert _undo_tracked(col, stats) is True
    assert stats.answered == 1
    assert stats.struggle == 0
    assert stats.struggle_card_ids == set()
    assert _grab(col, struggle.id) == in_session[struggle.id]

    assert _undo_tracked(col, stats) is True
    assert stats.answered == 0
    assert stats.solid == 0
    assert stats.seen_card_ids == set()
    assert _grab(col, solid.id) == in_session[solid.id]
    for cid in ids:
        _schedule_identity(col, cid, before[cid])

    assert _redo_tracked(col, stats) is True
    assert stats.answered == 1
    assert stats.solid == 1
    assert stats.struggle == 0
    assert _grab(col, solid.id) == after_solid

    assert _redo_tracked(col, stats) is True
    assert stats.struggle_card_ids == {struggle.id}
    assert stats.struggle == 1
    assert _grab(col, struggle.id) == after_again

    assert _redo_tracked(col, stats) is True
    assert stats.answered == 3
    assert stats.solid == 1
    assert stats.struggle == 2
    assert _grab(col, struggle.id) == after_hard

    # The flag edit is still on Anki's redo queue. It is not a grade.
    assert _redo_tracked(col, stats) is False
    assert stats.answered == 3
    assert int(col.get_card(solid.id).flags) == 1

    assert _undo_tracked(col, stats) is False
    assert stats.answered == 3
    assert _undo_tracked(col, stats) is True
    assert stats.answered == 2
    assert stats.struggle == 1
    assert format_running_stats(stats, now=1_030.0) == (
        "Free Review: Answered 2 · Solid 1 · Struggle 1 · 0:30"
    )
    assert _redo_tracked(col, stats) is True
    assert stats.answered == 3
    assert stats.struggle == 2

    # Another grade clears Anki's redo queue, so the flag edit cannot return.
    grade(struggle, 3)
    assert stats.answered == 4
    assert stats.solid == 2
    assert stats.struggle == 2
    assert struggle.id in stats.struggle_card_ids
    with pytest.raises(UndoEmpty):
        col.redo()
    assert stats.answered == 4
    for cid in ids:
        _schedule_identity(col, cid, before[cid])

    assert cleanup_free_review_deck(col, result.deck_id)
    for cid in ids:
        assert _grab(col, cid) == before[cid]
    col.undo()
    for cid in ids:
        assert _grab(col, cid) == before[cid]
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))


def test_struggle_review_undo_does_not_resurrect_the_practice_deck(col) -> None:
    deck_id = col.decks.id("UndoStruggle")
    card = _review(col, "q", deck_id, due=0, ivl=4, reps=2)
    result = start_struggle_review(col, deck_id, [card.id])
    assert result.ok, result.message
    _answer(col, card.id, 1)
    assert int(col.get_card(card.id).due) != 0
    assert cleanup_free_review_deck(col, result.deck_id)
    assert int(col.get_card(card.id).due) == 0
    assert int(col.get_card(card.id).reps) == 2
    col.undo()
    assert int(col.get_card(card.id).due) == 0
    assert int(col.get_card(card.id).reps) == 2
    assert int(col.get_card(card.id).ivl) == 4
    assert not any(name.startswith(FREE_REVIEW_PREFIX) for name in _names(col))


def _review(col, front: str, deck_id: int, *, due: int, ivl: int, reps: int):
    return _write(
        col,
        _note(col, front, deck_id),
        type=C.CARD_TYPE_REV,
        queue=C.QUEUE_TYPE_REV,
        due=due,
        ivl=ivl,
        factor=2500,
        reps=reps,
        lapses=0,
    )


def _names(col) -> list[str]:
    return [item.name for item in col.decks.all_names_and_ids()]


def _user_filtered_deck(
    col, name: str, *, search: str = "tag:__free_review_test_none__", reschedule: bool = True
) -> int:
    out = col.sched.add_or_update_filtered_deck(
        FilteredDeckForUpdate(
            id=0,
            name=name,
            config=FilteredDeckConfig(
                reschedule=reschedule,
                search_terms=[
                    FilteredDeckConfig.SearchTerm(search=search, limit=5, order=0)
                ],
            ),
            allow_empty=True,
        )
    )
    return int(out.id)
