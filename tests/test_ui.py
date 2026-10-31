"""UI hook registration without Qt."""

from __future__ import annotations

import anki_free_review
import anki_free_review.session
import anki_free_review.ui as ui


class _Hook:
    def __init__(self) -> None:
        self.callbacks: list = []

    def append(self, callback) -> None:
        self.callbacks.append(callback)


class _Hooks:
    def __init__(self) -> None:
        self.overview_will_render_bottom = _Hook()
        self.deck_browser_will_show_options_menu = _Hook()
        self.state_shortcuts_will_change = _Hook()
        self.state_did_change = _Hook()
        self.profile_did_open = _Hook()
        self.profile_will_close = _Hook()
        self.sync_will_start = _Hook()
        self.reviewer_did_answer_card = _Hook()
        self.reviewer_did_show_question = _Hook()
        self.state_did_undo = _Hook()


def test_package_imports_without_aqt() -> None:
    assert anki_free_review is not None
    assert anki_free_review.session.start_free_review is not None


def test_install_hooks_registers_each_entry_point() -> None:
    hooks = _Hooks()
    ui.install_hooks(hooks)
    assert hooks.overview_will_render_bottom.callbacks == [ui._on_overview_bottom]
    assert hooks.deck_browser_will_show_options_menu.callbacks == [
        ui._on_deck_options_menu
    ]
    assert hooks.state_shortcuts_will_change.callbacks == [ui._on_shortcuts]
    assert hooks.state_did_change.callbacks == [ui._on_state_change]
    assert hooks.profile_did_open.callbacks == [ui._on_profile_did_open]
    assert hooks.profile_will_close.callbacks == [ui._on_profile_will_close]
    assert hooks.sync_will_start.callbacks == [ui._on_sync_will_start]
    assert hooks.reviewer_did_answer_card.callbacks == [ui._on_reviewer_did_answer_card]
    assert hooks.reviewer_did_show_question.callbacks == [ui._on_reviewer_did_show_question]
    assert hooks.state_did_undo.callbacks == [ui._on_state_did_undo]


def test_overview_button_is_hidden_on_filtered_decks() -> None:
    links: list = [["O", "opts", "Options"]]
    ui.append_overview_button(links, deck_is_filtered=False)
    assert links[-1] == ["F", ui.FREE_REVIEW_URL, "Free Review"]

    filtered: list = []
    ui.append_overview_button(filtered, deck_is_filtered=True)
    assert filtered == []


def test_overview_url_dispatches_free_review() -> None:
    seen: list[str] = []

    def fallback(url: str) -> bool:
        seen.append(url)
        return False

    assert ui.handle_overview_url("freeReview", fallback, lambda: seen.append("go"))
    assert seen == ["go"]
    assert ui.handle_overview_url("study", fallback, lambda: None) is False
    assert seen[-1] == "study"


def test_dialog_cancel_and_scope_mapping() -> None:
    from anki_free_review.session import SCOPE_ALL_SEEN, SCOPE_OMIT_MATURE

    assert ui.free_review_scope_or_cancel(False, True) is None
    assert ui.free_review_scope_or_cancel(False, False) is None
    assert ui.free_review_scope_or_cancel(True, False) == SCOPE_ALL_SEEN
    assert ui.free_review_scope_or_cancel(True, True) == SCOPE_OMIT_MATURE


def test_dialog_tracking_is_independent_of_scope() -> None:
    from anki_free_review.session import SCOPE_ALL_SEEN, SCOPE_OMIT_MATURE

    omitted = ui.free_review_dialog_choice(True, True, True)
    assert omitted is not None
    assert omitted.scope == SCOPE_OMIT_MATURE
    assert omitted.track_struggle is True

    all_seen = ui.free_review_dialog_choice(True, False, False)
    assert all_seen is not None
    assert all_seen.scope == SCOPE_ALL_SEEN
    assert all_seen.track_struggle is False
    assert ui.free_review_dialog_choice(False, True, True) is None


def test_session_end_rules() -> None:
    assert ui.should_end_session("review", 5, 5) is False
    assert ui.should_end_session("review", 9, 5) is True
    assert ui.should_end_session("overview", 5, 5) is True
    assert ui.should_end_session("deckBrowser", 5, 5) is True
    assert ui.should_end_session("review", 5, None) is False


class _Signal:
    def __init__(self) -> None:
        self.callbacks: list = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


class _FakeTimer:
    """QTimer stand-in: start, stop, and isActive, with no Qt."""

    def __init__(self) -> None:
        self.timeout = _Signal()
        self.destroyed = _Signal()
        self.interval: int | None = None
        self.active = False
        self.starts = 0
        self.stops = 0

    def setInterval(self, interval_ms: int) -> None:
        self.interval = interval_ms

    def isActive(self) -> bool:
        return self.active

    def start(self) -> None:
        self.active = True
        self.starts += 1

    def stop(self) -> None:
        if self.active:
            self.stops += 1
        self.active = False


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""
        self.visible = False
        self.destroyed = _Signal()

    def setText(self, text: str) -> None:
        self.text = text

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False


class _DeletedLabel(_FakeLabel):
    """A label whose C++ object is already gone."""

    def setText(self, text: str) -> None:
        raise RuntimeError("wrapped C/C++ object has been deleted")

    def hide(self) -> None:
        raise RuntimeError("wrapped C/C++ object has been deleted")


def _save_chrome():
    return (
        ui._stats_label,
        ui._elapsed_timer,
        ui._active.stats,
        ui._active.filtered_id,
        ui._active.source_id,
    )


def _restore_chrome(saved) -> None:
    (
        ui._stats_label,
        ui._elapsed_timer,
        ui._active.stats,
        ui._active.filtered_id,
        ui._active.source_id,
    ) = saved


def test_running_stats_tick_text_moves_the_clock_only() -> None:
    from anki_free_review.tracking import begin_session_tracking

    stats = begin_session_tracking(True, now=100)
    assert stats is not None
    stats.record_answer(7, 1)
    seen = set(stats.seen_card_ids)

    assert (
        ui.running_stats_tick_text(
            stats, now=100, reviewing=True, in_tracked_deck=True
        )
        == "Free Review: Answered 1 · Solid 0 · Struggle 1 · 0:00"
    )
    assert (
        ui.running_stats_tick_text(
            stats, now=161, reviewing=True, in_tracked_deck=True
        )
        == "Free Review: Answered 1 · Solid 0 · Struggle 1 · 1:01"
    )
    assert stats.answered == 1
    assert stats.solid == 0
    assert stats.struggle == 1
    assert stats.seen_card_ids == seen
    assert stats.struggle_card_ids == {7}

    assert (
        ui.running_stats_tick_text(None, now=161, reviewing=True, in_tracked_deck=True)
        is None
    )
    assert (
        ui.running_stats_tick_text(
            stats, now=161, reviewing=False, in_tracked_deck=True
        )
        is None
    )
    assert (
        ui.running_stats_tick_text(
            stats, now=161, reviewing=True, in_tracked_deck=False
        )
        is None
    )

    stats.freeze(now=130)
    assert (
        ui.running_stats_tick_text(
            stats, now=999, reviewing=True, in_tracked_deck=True
        )
        == "Free Review: Answered 1 · Solid 0 · Struggle 1 · 0:30"
    )


def test_elapsed_timer_starts_once_while_chrome_is_shown_and_stops_when_hidden() -> None:
    saved = _save_chrome()
    try:
        timer = _FakeTimer()
        label = _FakeLabel()
        ui._install_elapsed_timer(timer)
        ui._stats_label = label

        assert timer.interval == ui.ELAPSED_TICK_MS == 1000
        assert timer.timeout.callbacks == [ui._on_elapsed_tick]

        ui._show_stats_label("Free Review: Answered 0 · Solid 0 · Struggle 0 · 0:00")
        # A later card show refreshes counts but must not restart the interval.
        ui._show_stats_label("Free Review: Answered 1 · Solid 1 · Struggle 0 · 0:04")
        assert timer.starts == 1
        assert timer.active
        assert label.visible
        assert label.text.endswith("0:04")

        ui._hide_stats_label()
        assert timer.stops == 1
        assert not timer.active
        assert not label.visible
        assert label.text == ""

        ui._hide_stats_label()
        assert timer.stops == 1

        ui._show_stats_label("Free Review: Answered 1 · Solid 1 · Struggle 0 · 0:05")
        assert timer.starts == 2
        assert timer.active
    finally:
        _restore_chrome(saved)


def test_elapsed_tick_rewrites_the_clock_and_stops_when_the_session_ends() -> None:
    from anki_free_review.tracking import begin_session_tracking

    saved = _save_chrome()
    try:
        stats = begin_session_tracking(True, now=0)
        assert stats is not None
        stats.record_answer(4, 3)
        ui._active.stats = stats
        label = _FakeLabel()
        label.show()
        timer = _FakeTimer()
        ui._install_elapsed_timer(timer)
        timer.start()
        ui._stats_label = label

        text = ui._apply_elapsed_tick(reviewing=True, in_tracked_deck=True, now=1)
        assert text == "Free Review: Answered 1 · Solid 1 · Struggle 0 · 0:01"
        text = ui._apply_elapsed_tick(reviewing=True, in_tracked_deck=True, now=2)
        assert text == "Free Review: Answered 1 · Solid 1 · Struggle 0 · 0:02"
        assert label.text == text
        assert stats.answered == 1
        assert stats.solid == 1
        assert stats.struggle == 0
        assert stats.seen_card_ids == {4}
        assert stats.struggle_card_ids == set()
        assert timer.active
        assert timer.starts == 1

        # Left the practice deck: hide the line and stop. The tally stays until
        # the session actually ends, so the next card show can bring it back.
        assert (
            ui._apply_elapsed_tick(reviewing=True, in_tracked_deck=False, now=6)
            is None
        )
        assert not timer.active
        assert label.text == ""
        assert not label.visible
        assert ui._active.stats is stats
        assert stats.answered == 1

        # Sync drops the tally, then hides, before the state change.
        label.show()
        label.setText("still up")
        timer.start()
        ui._active.stats = None
        ui._hide_stats_label()
        assert not timer.active
        assert label.text == ""

        # Leave, summary, and quit all go through finish, which freezes and stops.
        ui._active.stats = stats
        ui._active.filtered_id = 9
        ui._active.source_id = 3
        label.show()
        timer.start()
        finished, source_id = ui._finish_active_tracking()
        assert finished is stats
        assert source_id == 3
        assert stats.ended_at is not None
        assert ui._active.stats is None
        assert ui._active.filtered_id is None
        assert not timer.active
        assert label.text == ""
        assert not label.visible
    finally:
        _restore_chrome(saved)


class _UndoStatus:
    def __init__(self, last_step: int) -> None:
        self.last_step = last_step


class _UndoChanges:
    def __init__(self, counter: int, last_step: int) -> None:
        self.counter = counter
        self.new_status = _UndoStatus(last_step)


def test_undo_and_redo_hooks_follow_the_answer_stack() -> None:
    from anki_free_review.tracking import (
        accept_review_answer,
        begin_session_tracking,
        redo_review_answer,
    )

    saved = _save_chrome()
    ui._pending_redos.clear()
    try:
        stats = begin_session_tracking(True, now=0)
        assert stats is not None
        ui._active.stats = stats
        ui._active.filtered_id = 5
        assert (
            accept_review_answer(
                stats, in_tracked_deck=True, card_id=8, ease=1, undo_step=4
            )
            == "struggle"
        )
        assert (
            accept_review_answer(
                stats, in_tracked_deck=True, card_id=9, ease=3, undo_step=5
            )
            == "solid"
        )

        ui._on_state_did_undo(_UndoChanges(99, 100))
        assert stats.answered == 2
        ui._on_state_did_undo(_UndoChanges(5, 6))
        assert stats.answered == 1
        assert stats.solid == 0
        assert stats.struggle == 1
        assert stats.struggle_card_ids == {8}

        ui._remember_redo(100, 101)
        ui._on_operation_did_execute(None, None)
        assert stats.answered == 1
        ui._remember_redo(6, 7)
        ui._on_operation_did_execute(None, None)
        assert stats.answered == 2
        assert stats.solid == 1
        assert stats.struggle_card_ids == {8}

        # The redone Good is undone by the new counter, then redone again.
        ui._on_state_did_undo(_UndoChanges(7, 8))
        assert stats.solid == 0
        assert redo_review_answer(stats, redone_step=8, undo_step=9) is True
        assert stats.answered == 2
        assert stats.solid == 1
    finally:
        ui._pending_redos.clear()
        _restore_chrome(saved)


def test_answer_step_is_only_an_answer_card_and_is_taken_in_order() -> None:
    from anki_free_review.ui import answer_undo_step

    assert answer_undo_step("Answer Card", 6, "Answer Card") == 6
    assert answer_undo_step("Update Card", 7, "Answer Card") is None
    assert answer_undo_step("", 7, "Answer Card") is None
    # The name Anki reports is translated; a different translation does not match.
    assert answer_undo_step("Antworten", 8, "Answer Card") is None
    assert answer_undo_step("Antworten", 8, "Antworten") == 8

    ui._pending_answer_steps.clear()
    try:
        assert ui._take_answer_step() is None
        ui._remember_answer_step(answer_undo_step("Answer Card", 6, "Answer Card"))
        ui._remember_answer_step(answer_undo_step("Undo", 7, "Answer Card"))
        # A later undo has moved last_step to 7. The grade keeps the captured 6.
        assert ui._take_answer_step() == 6
        assert ui._take_answer_step() is None
        assert ui._take_answer_step() is None
    finally:
        ui._pending_answer_steps.clear()


def test_running_line_follows_undo_and_redo() -> None:
    from anki_free_review.tracking import (
        accept_review_answer,
        begin_session_tracking,
        redo_review_answer,
        undo_review_answer,
    )

    saved = _save_chrome()
    try:
        stats = begin_session_tracking(True, now=100)
        assert stats is not None
        ui._active.stats = stats
        assert (
            accept_review_answer(
                stats, in_tracked_deck=True, card_id=4, ease=3, undo_step=7
            )
            == "solid"
        )
        label = _FakeLabel()
        label.show()
        timer = _FakeTimer()
        ui._install_elapsed_timer(timer)
        timer.start()
        ui._stats_label = label

        shown = ui._apply_elapsed_tick(reviewing=True, in_tracked_deck=True, now=104)
        assert shown == "Free Review: Answered 1 · Solid 1 · Struggle 0 · 0:04"
        assert label.text == shown

        assert undo_review_answer(stats, undone_step=7, redo_step=8) is True
        shown = ui._apply_elapsed_tick(reviewing=True, in_tracked_deck=True, now=110)
        assert shown == "Free Review: Answered 0 · Solid 0 · Struggle 0 · 0:10"
        assert label.text == shown
        assert timer.active

        assert redo_review_answer(stats, redone_step=8, undo_step=9) is True
        shown = ui._apply_elapsed_tick(reviewing=True, in_tracked_deck=True, now=111)
        assert shown == "Free Review: Answered 1 · Solid 1 · Struggle 0 · 0:11"
        assert label.text == shown
    finally:
        _restore_chrome(saved)


def test_tearing_down_the_stats_label_stops_the_timer() -> None:
    saved = _save_chrome()
    try:
        label = _FakeLabel()
        timer = _FakeTimer()
        ui._stats_label = label
        ui._install_elapsed_timer(timer)
        timer.start()
        ui._watch_stats_label(label)

        assert len(label.destroyed.callbacks) == 1
        label.destroyed.callbacks[0]()
        assert ui._stats_label is None
        assert not timer.active
        assert timer.stops == 1

        # A tick that finds the label already deleted drops it and stops.
        dead = _DeletedLabel()
        dead.show()
        stats_timer = _FakeTimer()
        ui._install_elapsed_timer(stats_timer)
        stats_timer.start()
        ui._stats_label = dead
        from anki_free_review.tracking import begin_session_tracking

        ui._active.stats = begin_session_tracking(True, now=0)
        assert (
            ui._apply_elapsed_tick(reviewing=True, in_tracked_deck=True, now=3)
            is None
        )
        assert ui._stats_label is None
        assert not stats_timer.active
        assert stats_timer.stops == 1
    finally:
        _restore_chrome(saved)
