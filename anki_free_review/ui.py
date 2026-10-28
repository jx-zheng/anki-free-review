"""Deck overview button and deck-browser menu for Free Review.

The Qt pieces are imported inside the callbacks so this module can be
imported under pytest without aqt.
"""

from __future__ import annotations

from .session import (
    EMPTY_MESSAGE,
    SCOPE_ALL_SEEN,
    SCOPE_OMIT_MATURE,
    cleanup_all_free_review_sessions,
    cleanup_free_review_deck,
    start_from_dialog_choice,
    take_snapshot_warnings,
)

CHOICE_ALL_SEEN = "All seen cards"
CHOICE_OMIT_MATURE = "Omit mature cards"

FREE_REVIEW_URL = "freeReview"
FREE_REVIEW_LABEL = "Free Review"


class _ActiveSession:
    filtered_id: int | None = None
    source_id: int | None = None

    def clear(self) -> None:
        self.filtered_id = None
        self.source_id = None


_active = _ActiveSession()


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


def ask_free_review_scope(parent) -> str | None:
    """Ask which seen cards to practice. Cancel returns None and builds nothing."""

    from aqt.qt import (
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
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    qconnect(buttons.accepted, dialog.accept)
    qconnect(buttons.rejected, dialog.reject)
    layout.addWidget(buttons)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    return free_review_scope_or_cancel(accepted, omit.isChecked())


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
    source_id = _active.source_id
    _active.clear()
    if filtered_id:
        # Synchronous on purpose: this must finish before a sync that the
        # state change might be heading into. Undo is collapsed inside
        # cleanup, so one Undo cannot bring the practice deck back by itself.
        cleanup_free_review_deck(mw.col, filtered_id)
    _show_snapshot_warnings()
    _return_to_source_later(new_state, source_id)


def _return_to_source_later(new_state: str, source_id: int | None) -> None:
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
        mw.moveToState("deckBrowser")
        return
    _sweep()


def _sweep() -> None:
    from aqt import mw

    if not mw.col:
        return
    cleanup_all_free_review_sessions(mw.col)
    _active.clear()
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
    from aqt.operations import CollectionOp
    from aqt.utils import tooltip

    scope = ask_free_review_scope(mw)
    if scope is None:
        return

    def op(col):
        return start_from_dialog_choice(col, deck_id, scope)

    def success(result) -> None:
        if result is None:
            return
        if not result.ok or not result.deck_id:
            tooltip(result.message or EMPTY_MESSAGE, parent=mw)
            return
        _active.filtered_id = int(result.deck_id)
        _active.source_id = int(deck_id)
        mw.col.decks.select(result.deck_id)
        mw.moveToState("review")

    def failure(exc: Exception) -> None:
        tooltip(str(exc), parent=mw)

    CollectionOp(parent=mw, op=op).success(success).failure(failure).run_in_background()
