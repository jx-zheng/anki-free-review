"""In-memory struggle tracking for one Free Review run.

Nothing in this module writes the collection or the restore file. Scheduling
stays on the filtered-deck preview path.

Struggle-set rule: the first Again (1) or Hard (2) puts that card id in
``struggle_card_ids`` for the rest of the session. A later Good (3) or Easy
(4) does not remove it. ``solid`` and ``struggle`` count answers, not unique
cards. The success rate is solid answers divided by answers. ``seen_card_ids``
is every card shown or answered.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field

EASE_AGAIN = 1
EASE_HARD = 2
EASE_GOOD = 3
EASE_EASY = 4
SOLID_EASES = frozenset({EASE_GOOD, EASE_EASY})
STRUGGLE_EASES = frozenset({EASE_AGAIN, EASE_HARD})

TRACK_STRUGGLE_CONFIG_KEY = "track_struggle"

SUMMARY_REVIEW = "review"
SUMMARY_BROWSER = "browser"
SUMMARY_CLOSE = "close"

STRUGGLE_SET_RULE = (
    "A card stays on the struggle list after Again or Hard, "
    "even if a later answer is Good or Easy."
)


def classify_ease(ease: int) -> str | None:
    """Return ``solid``, ``struggle``, or None when ``ease`` is not an answer."""

    try:
        value = int(ease)
    except (TypeError, ValueError):
        return None
    if value in SOLID_EASES:
        return "solid"
    if value in STRUGGLE_EASES:
        return "struggle"
    return None


@dataclass
class SessionStats:
    """Answer totals for a single tracked Free Review run."""

    started_at: float
    ended_at: float | None = None
    answered: int = 0
    solid: int = 0
    struggle: int = 0
    seen_card_ids: set[int] = field(default_factory=set)
    struggle_card_ids: set[int] = field(default_factory=set)

    def note_seen(self, card_id: int) -> None:
        self.seen_card_ids.add(int(card_id))

    def record_answer(self, card_id: int, ease: int) -> str | None:
        """Count one answer. Again/Hard joins the struggle set and stays there."""

        kind = classify_ease(ease)
        if kind is None:
            return None
        card_id = int(card_id)
        self.seen_card_ids.add(card_id)
        self.answered += 1
        if kind == "solid":
            self.solid += 1
            return kind
        self.struggle += 1
        self.struggle_card_ids.add(card_id)
        return kind

    def freeze(self, *, now: float) -> None:
        """Stop the elapsed clock at the moment the session ends."""

        if self.ended_at is None:
            self.ended_at = now

    def elapsed_seconds(self, *, now: float) -> float:
        end = self.ended_at if self.ended_at is not None else now
        return max(0.0, end - self.started_at)


def begin_session_tracking(enabled: bool, *, now: float | None = None) -> SessionStats | None:
    """Start totals when the user opted in. Off returns None and stores nothing."""

    if not enabled:
        return None
    if now is None:
        now = time.monotonic()
    return SessionStats(started_at=now)


def _in_tracked_deck(
    stats: SessionStats | None,
    *,
    current_deck_id: int | None,
    filtered_deck_id: int | None,
) -> bool:
    if stats is None or filtered_deck_id is None or current_deck_id is None:
        return False
    return int(current_deck_id) == int(filtered_deck_id)


def note_card_seen(
    stats: SessionStats | None,
    *,
    current_deck_id: int | None,
    filtered_deck_id: int | None,
    card_id: int,
) -> bool:
    """Record a shown card. False when tracking is off or the deck is not ours."""

    if not _in_tracked_deck(
        stats, current_deck_id=current_deck_id, filtered_deck_id=filtered_deck_id
    ):
        return False
    assert stats is not None
    stats.note_seen(card_id)
    return True


def accept_review_answer(
    stats: SessionStats | None,
    *,
    current_deck_id: int | None,
    filtered_deck_id: int | None,
    card_id: int,
    ease: int,
) -> str | None:
    """Count an answer given in the practice deck. Other decks are ignored.

    Gate on the deck being reviewed, not ``card.did``. Good and Easy return
    the card to its home deck before the reviewer hook runs.
    """

    if not _in_tracked_deck(
        stats, current_deck_id=current_deck_id, filtered_deck_id=filtered_deck_id
    ):
        return None
    assert stats is not None
    return stats.record_answer(card_id, ease)


def should_show_summary(stats: SessionStats | None) -> bool:
    """A summary exists only for a run that opted in. Off keeps the old ending."""

    return stats is not None


def tracking_enabled_from_config(config: dict | None) -> bool:
    """Default off unless the saved add-on config says true."""

    if not isinstance(config, dict):
        return False
    return config.get(TRACK_STRUGGLE_CONFIG_KEY, False) is True


def config_with_tracking(config: dict | None, enabled: bool) -> dict:
    """Copy ``config`` and store the checkbox value. Other keys are kept."""

    updated = dict(config or {})
    updated[TRACK_STRUGGLE_CONFIG_KEY] = bool(enabled)
    return updated


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_success_rate(solid: int, answered: int) -> str:
    """Solid answers / answers, as a percent. No answers is ``n/a``."""

    if answered <= 0:
        return "n/a"
    percent = round(100.0 * solid / answered, 1)
    if percent == int(percent):
        return f"{int(percent)}%"
    return f"{percent:.1f}%"


def format_running_stats(stats: SessionStats, *, now: float) -> str:
    """One line for the reviewer bottom bar."""

    elapsed = format_elapsed(stats.elapsed_seconds(now=now))
    return (
        f"Free Review: Answered {stats.answered} · "
        f"Solid {stats.solid} · Struggle {stats.struggle} · {elapsed}"
    )


def format_summary_text(stats: SessionStats, *, ended_at: float | None = None) -> str:
    """Body of the end-of-session dialog."""

    if ended_at is None:
        ended_at = stats.ended_at if stats.ended_at is not None else stats.started_at
    rate = format_success_rate(stats.solid, stats.answered)
    lines = [
        f"Time spent: {format_elapsed(ended_at - stats.started_at)}",
        f"Cards seen: {len(stats.seen_card_ids)}",
        f"Answered: {stats.answered}",
        f"Solid (Good or Easy): {stats.solid}",
        f"Struggle (Again or Hard): {stats.struggle}",
        f"Success rate: {rate} (solid / answered)",
        f"Struggle cards: {len(stats.struggle_card_ids)}",
        "",
        STRUGGLE_SET_RULE,
    ]
    return "\n".join(lines)


def struggle_browser_search(card_ids: Iterable[int]) -> str:
    """Browser search for the struggle set. Empty when there is nothing to show."""

    ids = sorted({int(cid) for cid in card_ids})
    if not ids:
        return ""
    return "cid:" + ",".join(str(cid) for cid in ids)


def summary_actions(stats: SessionStats, *, source_deck_id: int | None) -> tuple[str, ...]:
    """Buttons for the summary. Close is always available.

    Review needs a source deck so the new filtered deck has somewhere to
    return cards. Browser only needs the card ids.
    """

    actions: list[str] = []
    if stats.struggle_card_ids and source_deck_id:
        actions.append(SUMMARY_REVIEW)
    if stats.struggle_card_ids:
        actions.append(SUMMARY_BROWSER)
    actions.append(SUMMARY_CLOSE)
    return tuple(actions)
