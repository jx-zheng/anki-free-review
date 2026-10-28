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


def test_session_end_rules() -> None:
    assert ui.should_end_session("review", 5, 5) is False
    assert ui.should_end_session("review", 9, 5) is True
    assert ui.should_end_session("overview", 5, 5) is True
    assert ui.should_end_session("deckBrowser", 5, 5) is True
    assert ui.should_end_session("review", 5, None) is False
