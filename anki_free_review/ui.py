"""Deck overview button and deck-browser menu for Free Review.

The Qt pieces are imported inside the callbacks so this module can be
imported under pytest without aqt.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from .session import (
    EMPTY_MESSAGE,
    SCOPE_ALL_SEEN,
    SCOPE_OMIT_MATURE,
    cleanup_all_free_review_sessions,
    cleanup_free_review_deck,
    start_from_dialog_choice,
    start_struggle_review,
    take_snapshot_warnings,
)
from .tracking import (
    SUMMARY_BROWSER,
    SUMMARY_REVIEW,
    SessionStats,
    accept_review_answer,
    begin_session_tracking,
    config_with_tracking,
    format_running_stats,
    format_summary_text,
    note_card_seen,
    redo_review_answer,
    should_show_summary,
    struggle_browser_search,
    summary_actions,
    tracking_enabled_from_config,
    undo_review_answer,
)

logger = logging.getLogger(__name__)

CHOICE_ALL_SEEN = "All seen cards"
CHOICE_OMIT_MATURE = "Omit mature cards"
TRACK_STRUGGLE_LABEL = "Track struggle and session stats"
TRACK_STRUGGLE_NOTE = (
    "Again and Hard count as struggle. Good and Easy count as solid. "
    "The reviewer shows a running total, and a summary appears when you finish."
)
SUMMARY_WINDOW_TITLE = "Free Review summary"
SUMMARY_REVIEW_BUTTON = "Review struggle cards"
SUMMARY_BROWSER_BUTTON = "Show in Browser"

FREE_REVIEW_URL = "freeReview"
FREE_REVIEW_LABEL = "Free Review"


@dataclass(frozen=True)
class FreeReviewDialogChoice:
    scope: str
    track_struggle: bool


class _ActiveSession:
    filtered_id: int | None = None
    source_id: int | None = None
    stats: SessionStats | None = None

    def clear(self) -> None:
        self.filtered_id = None
        self.source_id = None
        self.stats = None


_active = _ActiveSession()
_stats_label = None
# One repeating timer for the running line's clock. It is started when that
# line is shown and stopped when the line is hidden or destroyed. Show and
# answer hooks still refresh the counts; this tick only rewrites the label.
ELAPSED_TICK_MS = 1000
_elapsed_timer = None
# Redo steps captured on the collection thread, applied on the UI thread.
# Anki fires state_did_undo for Undo only. Redo has the same result shape
# but no hook, so Collection.redo stashes the counters and the next
# operation_did_execute drains them.
_pending_redos: list[tuple[int, int]] = []
_redo_lock = threading.Lock()
_redo_bridge_installed = False
# Answer-step counters captured inside the answer op, before that op
# returns. The reviewer success callback runs later on the UI thread, and
# an Undo queued during the answer can finish first and change last_step.
_pending_answer_steps: list[int | None] = []
_answer_lock = threading.Lock()


def append_overview_button(links: list, *, deck_is_filtered: bool) -> None:
    """Add the overview bottom-bar button. ``links`` items are [shortcut, url, label]."""

    if not deck_is_filtered:
        links.append(["F", FREE_REVIEW_URL, FREE_REVIEW_LABEL])


def handle_overview_url(url: str, fallback, on_free_review) -> bool:
    if url == FREE_REVIEW_URL:
        on_free_review()
        return True
    result = fallback(url)
    return bool(result)


def free_review_scope_or_cancel(accepted: bool, omit_mature: bool) -> str | None:
    """Map the dialog result to a session scope. Cancel returns None."""

    if not accepted:
        return None
    return SCOPE_OMIT_MATURE if omit_mature else SCOPE_ALL_SEEN


def free_review_dialog_choice(
    accepted: bool, omit_mature: bool, track_struggle: bool
) -> FreeReviewDialogChoice | None:
    """Scope plus the struggle-tracking checkbox. Cancel returns None."""

    scope = free_review_scope_or_cancel(accepted, omit_mature)
    if scope is None:
        return None
    return FreeReviewDialogChoice(scope=scope, track_struggle=bool(track_struggle))


def ask_free_review_dialog(parent, *, track_struggle: bool) -> FreeReviewDialogChoice | None:
    """Ask which seen cards to practice, and whether to track struggle.

    Cancel returns None and builds nothing. ``track_struggle`` is the last
    saved choice; the shipped default is off.
    """

    from aqt.qt import (
        QCheckBox,
        QDialog,
        QDialogButtonBox,
        QLabel,
        QRadioButton,
        Qt,
        QVBoxLayout,
    )
    from aqt.utils import qconnect

    dialog = QDialog(parent)
    dialog.setWindowTitle("Free Review")
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("Which cards do you want to practice?"))
    all_seen = QRadioButton(CHOICE_ALL_SEEN)
    omit = QRadioButton(CHOICE_OMIT_MATURE)
    all_seen.setChecked(True)
    note = QLabel(
        "Mature cards have an interval of 21 days or more. "
        "New, suspended, and buried cards are always skipped."
    )
    note.setWordWrap(True)
    layout.addWidget(all_seen)
    layout.addWidget(omit)
    layout.addWidget(note)
    track = QCheckBox(TRACK_STRUGGLE_LABEL)
    track.setChecked(bool(track_struggle))
    track_note = QLabel(TRACK_STRUGGLE_NOTE)
    track_note.setWordWrap(True)
    layout.addWidget(track)
    layout.addWidget(track_note)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    qconnect(buttons.accepted, dialog.accept)
    qconnect(buttons.rejected, dialog.reject)
    layout.addWidget(buttons)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    return free_review_dialog_choice(accepted, omit.isChecked(), track.isChecked())


def should_end_session(
    new_state: str, current_deck_id: int, active_filtered_id: int | None
) -> bool:
    """True when the user has left the practice reviewer.

    Staying in the reviewer of the practice deck does not end the session.
    Overview, the deck browser, and any other deck do.
    """

    if not active_filtered_id:
        return False
    if new_state == "review" and current_deck_id == active_filtered_id:
        return False
    return True


def install_hooks(gui_hooks) -> None:
    gui_hooks.overview_will_render_bottom.append(_on_overview_bottom)
    gui_hooks.deck_browser_will_show_options_menu.append(_on_deck_options_menu)
    gui_hooks.state_shortcuts_will_change.append(_on_shortcuts)
    gui_hooks.state_did_change.append(_on_state_change)
    gui_hooks.profile_did_open.append(_on_profile_did_open)
    gui_hooks.profile_will_close.append(_on_profile_will_close)
    gui_hooks.sync_will_start.append(_on_sync_will_start)
    gui_hooks.reviewer_did_answer_card.append(_on_reviewer_did_answer_card)
    gui_hooks.reviewer_did_show_question.append(_on_reviewer_did_show_question)
    gui_hooks.state_did_undo.append(_on_state_did_undo)
    _install_redo_bridge(gui_hooks)


def _on_overview_bottom(link_handler, links):
    from aqt import mw

    if not mw.col:
        return link_handler
    append_overview_button(
        links, deck_is_filtered=bool(mw.col.decks.current()["dyn"])
    )

    def handler(url: str) -> bool:
        return handle_overview_url(url, link_handler, _start_for_current_deck)

    return handler


def _on_shortcuts(state: str, shortcuts: list) -> None:
    if state != "overview":
        return
    from aqt import mw

    if not mw.col or mw.col.decks.current()["dyn"]:
        return
    shortcuts.append(("f", _start_for_current_deck))


def _on_deck_options_menu(menu, deck_id: int) -> None:
    from aqt import mw
    from aqt.utils import qconnect

    if not mw.col or mw.col.decks.is_filtered(deck_id):
        return
    action = menu.addAction(FREE_REVIEW_LABEL)

    def start(_checked: bool = False, did: int = deck_id) -> None:
        _start(did)

    qconnect(action.triggered, start)


def _on_state_change(new_state: str, _old_state: str) -> None:
    from aqt import mw

    if not mw.col:
        return
    current = int(mw.col.decks.get_current_id())
    if not should_end_session(new_state, current, _active.filtered_id):
        return
    filtered_id = _active.filtered_id
    stats, source_id = _finish_active_tracking()
    if filtered_id:
        # Synchronous on purpose: this must finish before a sync that the
        # state change might be heading into. Undo is collapsed inside
        # cleanup, so one Undo cannot bring the practice deck back by itself.
        cleanup_free_review_deck(mw.col, filtered_id)
    _show_snapshot_warnings()
    show = stats if should_show_summary(stats) else None
    _return_to_source_later(new_state, source_id, show)


def _return_to_source_later(
    new_state: str, source_id: int | None, stats: SessionStats | None = None
) -> None:
    from aqt import mw
    from aqt.qt import QTimer

    def go() -> None:
        if not mw.col:
            return
        if source_id and mw.col.decks.have(source_id):
            mw.col.decks.select(source_id)
        if new_state in ("overview", "review"):
            mw.moveToState("overview")
        elif mw.state == "deckBrowser":
            mw.deckBrowser.refresh()
        if stats is not None:
            _show_summary_dialog(stats, source_id)

    QTimer.singleShot(0, go)


def _on_profile_did_open() -> None:
    # Runs before auto-sync on startup, so a crashed session is folded
    # back into the home decks before AnkiWeb sees it.
    _sweep()


def _on_profile_will_close() -> None:
    # Runs before auto-sync on quit.
    _sweep()


def _on_sync_will_start() -> None:
    from aqt import mw

    if mw.state == "review" and _active.filtered_id:
        # Leaving review runs _on_state_change, which deletes the practice deck.
        # Drop the tally first so that path does not open a summary over the sync.
        # Hide the running line now so its one-second clock cannot outlive the tally.
        _active.stats = None
        _hide_stats_label()
        mw.moveToState("deckBrowser")
        return
    _sweep()


def _sweep() -> None:
    from aqt import mw

    if not mw.col:
        return
    _finish_active_tracking()
    cleanup_all_free_review_sessions(mw.col)
    _show_snapshot_warnings()


def _show_snapshot_warnings() -> None:
    from aqt import mw
    from aqt.utils import tooltip

    for message in take_snapshot_warnings():
        tooltip(message, parent=mw)


def _start_for_current_deck() -> None:
    from aqt import mw

    if not mw.col:
        return
    _start(int(mw.col.decks.get_current_id()))


def _start(deck_id: int) -> None:
    from aqt import mw

    if not mw.col:
        return
    choice = ask_free_review_dialog(mw, track_struggle=_load_track_struggle())
    if choice is None:
        return
    _save_track_struggle(choice.track_struggle)

    def op(col):
        return start_from_dialog_choice(col, deck_id, choice.scope)

    _run_start(deck_id, choice.track_struggle, op)


def _start_card_session(source_id: int, card_ids: list[int]) -> None:
    """One-click revisit. Tracking stays on, since the summary only opens then."""

    def op(col):
        return start_struggle_review(col, source_id, card_ids)

    _run_start(source_id, True, op)


def _run_start(source_id: int, track_struggle: bool, op) -> None:
    from aqt import mw
    from aqt.operations import CollectionOp
    from aqt.utils import tooltip

    def success(result) -> None:
        if result is None:
            return
        if not result.ok or not result.deck_id:
            tooltip(result.message or EMPTY_MESSAGE, parent=mw)
            return
        _active.filtered_id = int(result.deck_id)
        _active.source_id = int(source_id)
        _active.stats = begin_session_tracking(track_struggle)
        mw.col.decks.select(result.deck_id)
        mw.moveToState("review")

    def failure(exc: Exception) -> None:
        tooltip(str(exc), parent=mw)

    CollectionOp(parent=mw, op=op).success(success).failure(failure).run_in_background()


def _show_summary_dialog(stats: SessionStats, source_id: int | None) -> None:
    from aqt import mw
    from aqt.qt import (
        QDialog,
        QDialogButtonBox,
        QLabel,
        Qt,
        QVBoxLayout,
    )
    from aqt.utils import qconnect

    dialog = QDialog(mw)
    dialog.setWindowTitle(SUMMARY_WINDOW_TITLE)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumWidth(440)
    layout = QVBoxLayout(dialog)
    ended_at = stats.ended_at if stats.ended_at is not None else stats.started_at
    label = QLabel(format_summary_text(stats, ended_at=ended_at))
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    layout.addWidget(label)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    actions = summary_actions(stats, source_deck_id=source_id)
    search = struggle_browser_search(stats.struggle_card_ids)
    review_ids = sorted(stats.struggle_card_ids)
    review_clicked = {"go": False}

    if SUMMARY_REVIEW in actions:
        review = buttons.addButton(
            SUMMARY_REVIEW_BUTTON, QDialogButtonBox.ButtonRole.ActionRole
        )

        def start_review() -> None:
            review_clicked["go"] = True
            dialog.accept()

        qconnect(review.clicked, start_review)

    if SUMMARY_BROWSER in actions:
        browser_button = buttons.addButton(
            SUMMARY_BROWSER_BUTTON, QDialogButtonBox.ButtonRole.ActionRole
        )

        def show_browser() -> None:
            _open_browser(search)

        qconnect(browser_button.clicked, show_browser)

    qconnect(buttons.rejected, dialog.reject)
    layout.addWidget(buttons)
    dialog.exec()
    if review_clicked["go"]:
        _start_card_session(int(source_id), review_ids)


def _open_browser(search: str) -> None:
    from aqt import dialogs, mw
    from aqt.utils import tooltip

    if not search or not mw.col:
        return
    try:
        browser = dialogs.open("Browser", mw)
        browser.search_for(search)
    except Exception as exc:
        logger.exception("free-review could not open the browser")
        tooltip(str(exc), parent=mw)


def _on_reviewer_did_show_question(card) -> None:
    try:
        if card is None:
            return
        in_deck = _reviewing_active_deck()
        if note_card_seen(
            _active.stats, in_tracked_deck=in_deck, card_id=int(card.id)
        ):
            _refresh_stats_chrome(in_tracked_deck=in_deck)
    except Exception:
        logger.exception("free-review struggle tracking failed")


def _on_reviewer_did_answer_card(_reviewer, card, ease) -> None:
    # Take the step even when this hook bails out, so a captured counter
    # cannot be applied to a later grade.
    undo_step = _take_answer_step()
    try:
        if card is None:
            return
        in_deck = _reviewing_active_deck()
        kind = accept_review_answer(
            _active.stats,
            in_tracked_deck=in_deck,
            card_id=int(card.id),
            ease=int(ease),
            undo_step=undo_step,
        )
        if kind is not None:
            _refresh_stats_chrome(in_tracked_deck=in_deck)
    except Exception:
        logger.exception("free-review struggle tracking failed")


def answer_undo_step(
    undo_name: str, last_step: int, answer_op_name: str
) -> int | None:
    """Undo counter of a grade, or None when the top op is not that grade.

    Compared by Anki's own op name (``actions-answer-card``), which is
    translated. A later undo must not be stored in place of the grade.
    """

    if undo_name != answer_op_name:
        return None
    return int(last_step)


def _capture_answer_step(col) -> int | None:
    """Read the grade's undo counter. Called before the answer op returns."""

    try:
        status = col.undo_status()
        return answer_undo_step(
            status.undo, int(status.last_step), col.tr.actions_answer_card()
        )
    except Exception:
        logger.warning("free-review could not read the answer undo step", exc_info=True)
        return None


def _remember_answer_step(step: int | None) -> None:
    """Queue one answer-op result. Safe to call from the collection thread."""

    with _answer_lock:
        _pending_answer_steps.append(None if step is None else int(step))


def _take_answer_step() -> int | None:
    """Pop the oldest captured answer step. None when nothing is waiting."""

    with _answer_lock:
        if not _pending_answer_steps:
            return None
        return _pending_answer_steps.pop(0)


def _on_state_did_undo(changes) -> None:
    """Pop one answer when this Undo is that grade. Other undos stay put."""

    try:
        changed = undo_review_answer(
            _active.stats,
            undone_step=int(changes.counter),
            redo_step=int(changes.new_status.last_step),
        )
    except Exception:
        logger.exception("free-review undo stats failed")
        return
    if changed:
        _refresh_active_stats_line()


def _install_redo_bridge(gui_hooks) -> None:
    """Notice Redo, and capture each grade's undo counter inside the answer op.

    Redo does not emit state_did_undo. The answer counter cannot be read in
    the reviewer success callback: that callback is queued on the UI thread,
    and an Undo started while the answer op is still running can finish
    first and change ``last_step``. Skipped when aqt is missing (pytest).
    The wraps only record counters; the tally is updated on the UI thread.
    """

    global _redo_bridge_installed
    if _redo_bridge_installed:
        return
    try:
        import aqt
        from anki.collection import Collection
    except ImportError:
        return
    # Pytest imports this module without the desktop package.
    if not hasattr(aqt, "mw"):
        return

    original = Collection.redo

    def redo(self):
        out = original(self)
        try:
            redone_step = int(out.counter)
            undo_step = int(out.new_status.last_step)
        except Exception:
            logger.warning("free-review could not read the redo step", exc_info=True)
            return out
        _remember_redo(redone_step, undo_step)
        return out

    Collection.redo = redo

    from anki.scheduler.v3 import Scheduler as V3Scheduler

    original_answer = V3Scheduler.answer_card

    def answer_card(self, answer):
        out = original_answer(self, answer)
        # Still inside the answer op, so a queued Undo cannot have moved
        # last_step yet. Only a tracked session needs the counter.
        if _active.stats is not None:
            _remember_answer_step(_capture_answer_step(self.col))
        return out

    V3Scheduler.answer_card = answer_card
    gui_hooks.operation_did_execute.append(_on_operation_did_execute)
    _redo_bridge_installed = True


def _remember_redo(redone_step: int, undo_step: int) -> None:
    """Queue one redo result. Safe to call from the collection thread."""

    with _redo_lock:
        _pending_redos.append((int(redone_step), int(undo_step)))


def _on_operation_did_execute(_changes, _handler) -> None:
    """Apply redo steps queued by Collection.redo. A no-op when none are waiting."""

    with _redo_lock:
        pending = list(_pending_redos)
        _pending_redos.clear()
    if not pending:
        return
    try:
        changed = False
        for redone_step, undo_step in pending:
            if redo_review_answer(
                _active.stats, redone_step=redone_step, undo_step=undo_step
            ):
                changed = True
        if changed:
            _refresh_active_stats_line()
    except Exception:
        logger.exception("free-review redo stats failed")


def _refresh_active_stats_line() -> None:
    """Rewrite the running line from the tally. The clock keeps moving."""

    try:
        _refresh_stats_chrome(in_tracked_deck=_reviewing_active_deck())
    except Exception:
        logger.debug("free-review stats line was not refreshed", exc_info=True)


def _reviewing_active_deck() -> bool:
    """True when the reviewer is still showing this practice deck."""

    from aqt import mw

    if not mw.col or not _active.filtered_id:
        return False
    try:
        current = int(mw.col.decks.get_current_id())
    except Exception:
        return False
    return current == _active.filtered_id


def running_stats_tick_text(
    stats: SessionStats | None,
    *,
    now: float,
    reviewing: bool,
    in_tracked_deck: bool,
) -> str | None:
    """Text for one elapsed tick, or None when the running line should come down.

    ``now`` only moves the clock. Answer counts are not changed here. None
    means the caller should hide the line and stop the timer.
    """

    if stats is None or not reviewing or not in_tracked_deck:
        return None
    return format_running_stats(stats, now=now)


def _refresh_stats_chrome(*, in_tracked_deck: bool) -> None:
    """Update counts on the running line when a card is shown or answered.

    The elapsed clock has its own one-second timer. This path still rewrites
    the whole line so solid and struggle change as soon as the answer lands.
    """

    from aqt import mw

    stats = _active.stats
    if stats is None or mw.state != "review" or not in_tracked_deck:
        _hide_stats_label()
        return
    _show_stats_label(format_running_stats(stats, now=time.monotonic()))


def _stats_label_widget():
    """A one-line strip between the card and the answer buttons.

    The reviewer bottom bar has a fixed height, so the totals live in the
    main window layout instead of inside that web view. A hidden label takes
    no space, which keeps untracked reviews unchanged.
    """

    global _stats_label
    if _stats_label is not None:
        return _stats_label
    from aqt import mw
    from aqt.qt import QLabel, Qt

    layout = getattr(mw, "mainLayout", None)
    bottom = getattr(mw, "bottomWeb", None)
    if layout is None or bottom is None:
        return None
    index = layout.indexOf(bottom)
    if index < 0:
        return None
    label = QLabel(mw)
    label.setObjectName("freeReviewStats")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    label.setContentsMargins(8, 4, 8, 4)
    label.setAccessibleName("Free Review session stats")
    layout.insertWidget(index, label)
    label.hide()
    _stats_label = label
    _watch_stats_label(label)
    return label


def _show_stats_label(text: str) -> None:
    try:
        label = _stats_label_widget()
    except Exception:
        logger.debug("free-review stats chrome update failed", exc_info=True)
        return
    if label is None:
        return
    try:
        label.setText(text)
        label.show()
    except RuntimeError:
        _forget_stats_label(label)
        return
    try:
        _start_elapsed_timer()
    except Exception:
        logger.debug("free-review elapsed timer could not start", exc_info=True)


def _hide_stats_label() -> None:
    """Hide the running line and stop its clock. Safe to call more than once."""

    _stop_elapsed_timer()
    label = _stats_label
    if label is None:
        return
    try:
        label.hide()
        label.setText("")
    except RuntimeError:
        _forget_stats_label(label)


def _watch_stats_label(label) -> None:
    """Stop the clock if Anki destroys the label while a session is open."""

    def dropped(*_args, watched=label) -> None:
        _forget_stats_label(watched)

    destroyed = getattr(label, "destroyed", None)
    if destroyed is None or not hasattr(destroyed, "connect"):
        return
    try:
        destroyed.connect(dropped)
    except Exception:
        logger.debug("free-review could not watch the stats label", exc_info=True)


def _forget_stats_label(label) -> None:
    """Drop a label that is gone, and do not leave its clock running."""

    global _stats_label
    if _stats_label is label:
        _stats_label = None
    _stop_elapsed_timer()


def _install_elapsed_timer(timer) -> None:
    """Use ``timer`` as the one-second clock. A test double can stand in for Qt."""

    global _elapsed_timer
    timer.setInterval(ELAPSED_TICK_MS)
    timer.timeout.connect(_on_elapsed_tick)
    _elapsed_timer = timer


def _create_elapsed_timer():
    from aqt import mw
    from aqt.qt import QTimer

    timer = QTimer(mw)
    _install_elapsed_timer(timer)

    def drop(*_args, watched=timer) -> None:
        _clear_elapsed_timer(watched)

    timer.destroyed.connect(drop)
    return timer


def _clear_elapsed_timer(timer) -> None:
    global _elapsed_timer
    if _elapsed_timer is timer:
        _elapsed_timer = None


def _start_elapsed_timer() -> None:
    """Start the clock if it is not already running.

    Calling start on a live QTimer restarts its interval. Card shows happen
    faster than once a second, so an active timer is left on its own cadence.
    """

    timer = _elapsed_timer
    if timer is None:
        timer = _create_elapsed_timer()
    if timer is None:
        return
    try:
        if timer.isActive():
            return
        timer.start()
    except RuntimeError:
        _clear_elapsed_timer(timer)


def _stop_elapsed_timer() -> None:
    """Stop the clock. A second stop, or a timer Qt already deleted, is a no-op."""

    timer = _elapsed_timer
    if timer is None:
        return
    try:
        if not timer.isActive():
            return
        timer.stop()
    except RuntimeError:
        _clear_elapsed_timer(timer)


def _on_elapsed_tick() -> None:
    """Rewrite the running line's clock.

    Does not record a shown card or an answer, and does not touch scheduling.
    """

    try:
        reviewing, in_deck = _elapsed_tick_context()
        _apply_elapsed_tick(reviewing=reviewing, in_tracked_deck=in_deck)
    except Exception:
        logger.exception("free-review elapsed tick failed")
        _stop_elapsed_timer()


def _elapsed_tick_context() -> tuple[bool, bool]:
    from aqt import mw

    return mw.state == "review", _reviewing_active_deck()


def _apply_elapsed_tick(
    *, reviewing: bool, in_tracked_deck: bool, now: float | None = None
) -> str | None:
    """Set the running line from ``now``. Returns that text, or None if it came down.

    Counts stay as they are. None hides the line and stops the timer, so a tick
    that finds the session no longer on screen does not keep the clock running.
    """

    if now is None:
        now = time.monotonic()
    text = running_stats_tick_text(
        _active.stats,
        now=now,
        reviewing=reviewing,
        in_tracked_deck=in_tracked_deck,
    )
    label = _stats_label
    if text is None or label is None:
        _hide_stats_label()
        return None
    try:
        label.setText(text)
    except RuntimeError:
        _forget_stats_label(label)
        return None
    return text


def _finish_active_tracking() -> tuple[SessionStats | None, int | None]:
    stats = _active.stats
    source_id = _active.source_id
    _active.clear()
    if stats is not None:
        stats.freeze(now=time.monotonic())
    _hide_stats_label()
    return stats, source_id


def _addon_package() -> str:
    """Folder Anki actually loaded, including a numeric AnkiWeb id."""

    from aqt import mw

    return mw.addonManager.addonFromModule(__name__)


def _load_track_struggle() -> bool:
    try:
        from aqt import mw

        config = mw.addonManager.getConfig(_addon_package())
    except Exception:
        logger.debug("free-review config could not be read", exc_info=True)
        return False
    return tracking_enabled_from_config(config)


def _save_track_struggle(enabled: bool) -> None:
    try:
        from aqt import mw

        package = _addon_package()
        current = mw.addonManager.getConfig(package)
        mw.addonManager.writeConfig(package, config_with_tracking(current, enabled))
    except Exception:
        logger.warning(
            "free-review could not save the struggle-tracking choice", exc_info=True
        )
