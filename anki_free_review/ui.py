"""Deck overview button and deck-browser menu for Free Review.

The Qt pieces are imported inside the callbacks so this module can be
imported under pytest without aqt.
"""

from __future__ import annotations

import logging
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
    should_show_summary,
    struggle_browser_search,
    summary_actions,
    tracking_enabled_from_config,
)

logger = logging.getLogger(__name__)

# Anki loads this folder as the add-on module and reads config.json from it.
ADDON_PACKAGE = "anki_free_review"

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
_stats_timer = None
_stats_label = None
_summary_suppressed = False


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


def tracking_to_persist(choice: FreeReviewDialogChoice | None) -> bool | None:
    """The checkbox value to save. None means the dialog was cancelled."""

    if choice is None:
        return None
    return bool(choice.track_struggle)


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
    show = stats if should_show_summary(stats) and not _summary_suppressed else None
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
        if stats is not None and should_show_summary(stats):
            _show_summary_dialog(stats, source_id)

    QTimer.singleShot(0, go)


def _on_profile_did_open() -> None:
    # Runs before auto-sync on startup, so a crashed session is folded
    # back into the home decks before AnkiWeb sees it.
    _sweep()


def _on_profile_will_close() -> None:
    # Runs before auto-sync on quit. A summary dialog here would block teardown.
    global _summary_suppressed
    _summary_suppressed = True
    try:
        _sweep()
    finally:
        _summary_suppressed = False


def _on_sync_will_start() -> None:
    from aqt import mw

    global _summary_suppressed
    # A modal summary would sit on top of the sync. End the session quietly.
    _summary_suppressed = True
    try:
        if mw.state == "review" and _active.filtered_id:
            # Leaving review runs _on_state_change, which deletes the practice deck.
            mw.moveToState("deckBrowser")
            return
        _sweep()
    finally:
        _summary_suppressed = False


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
    persist = tracking_to_persist(choice)
    if choice is None or persist is None:
        return
    _save_track_struggle(persist)

    def op(col):
        return start_from_dialog_choice(col, deck_id, choice.scope)

    _run_start(deck_id, persist, op)


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
        if _active.stats is not None:
            _start_stats_timer()
        else:
            _stop_stats_timer()
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

    if SUMMARY_REVIEW in actions and source_id:
        review = buttons.addButton(
            SUMMARY_REVIEW_BUTTON, QDialogButtonBox.ButtonRole.ActionRole
        )

        def start_review() -> None:
            review_clicked["go"] = True
            dialog.accept()

        qconnect(review.clicked, start_review)

    if SUMMARY_BROWSER in actions and search:
        browser_button = buttons.addButton(
            SUMMARY_BROWSER_BUTTON, QDialogButtonBox.ButtonRole.ActionRole
        )

        def show_browser() -> None:
            _open_browser(search)

        qconnect(browser_button.clicked, show_browser)

    qconnect(buttons.rejected, dialog.reject)
    layout.addWidget(buttons)
    dialog.exec()
    if review_clicked["go"] and source_id and review_ids:
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
        current = _reviewing_active_deck()
        if note_card_seen(
            _active.stats,
            current_deck_id=current,
            filtered_deck_id=_active.filtered_id,
            card_id=int(card.id),
        ):
            _refresh_stats_chrome()
    except Exception:
        logger.exception("free-review struggle tracking failed")


def _on_reviewer_did_answer_card(_reviewer, card, ease) -> None:
    # The current deck is read from the collection. Good/Easy move the card
    # home before this runs, so card.did is not the practice deck. The
    # reviewer is still showing that deck.
    try:
        if card is None:
            return
        current = _reviewing_active_deck()
        kind = accept_review_answer(
            _active.stats,
            current_deck_id=current,
            filtered_deck_id=_active.filtered_id,
            card_id=int(card.id),
            ease=int(ease),
        )
        if kind is not None:
            _refresh_stats_chrome()
    except Exception:
        logger.exception("free-review struggle tracking failed")


def _reviewing_active_deck() -> int | None:
    from aqt import mw

    if not mw.col or not _active.filtered_id:
        return None
    try:
        current = int(mw.col.decks.get_current_id())
    except Exception:
        return None
    if current != _active.filtered_id:
        return None
    return current


def _refresh_stats_chrome() -> None:
    stats = _active.stats
    if stats is None or not _active.filtered_id:
        _hide_stats_label()
        return
    from aqt import mw

    if not mw.col or mw.state != "review":
        _hide_stats_label()
        return
    try:
        if int(mw.col.decks.get_current_id()) != _active.filtered_id:
            _hide_stats_label()
            return
    except Exception:
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
    return label


def _show_stats_label(text: str) -> None:
    try:
        label = _stats_label_widget()
    except Exception:
        logger.debug("free-review stats chrome update failed", exc_info=True)
        return
    if label is None:
        return
    label.setText(text)
    label.show()


def _hide_stats_label() -> None:
    if _stats_label is None:
        return
    _stats_label.hide()
    _stats_label.setText("")


def _start_stats_timer() -> None:
    global _stats_timer
    from aqt import mw
    from aqt.qt import QTimer
    from aqt.utils import qconnect

    if _stats_timer is None:
        timer = QTimer(mw)
        timer.setInterval(1000)
        qconnect(timer.timeout, _refresh_stats_chrome)
        _stats_timer = timer
    if not _stats_timer.isActive():
        _stats_timer.start()


def _stop_stats_timer() -> None:
    if _stats_timer is not None and _stats_timer.isActive():
        _stats_timer.stop()


def _finish_active_tracking() -> tuple[SessionStats | None, int | None]:
    stats = _active.stats
    source_id = _active.source_id
    _active.clear()
    if stats is not None:
        stats.freeze(now=time.monotonic())
    _stop_stats_timer()
    _hide_stats_label()
    return stats, source_id


def _load_track_struggle() -> bool:
    try:
        from aqt import mw

        config = mw.addonManager.getConfig(ADDON_PACKAGE)
    except Exception:
        logger.debug("free-review config could not be read", exc_info=True)
        return False
    return tracking_enabled_from_config(config)


def _save_track_struggle(enabled: bool) -> None:
    try:
        from aqt import mw

        current = mw.addonManager.getConfig(ADDON_PACKAGE)
        mw.addonManager.writeConfig(ADDON_PACKAGE, config_with_tracking(current, enabled))
    except Exception:
        logger.warning(
            "free-review could not save the struggle-tracking choice", exc_info=True
        )
